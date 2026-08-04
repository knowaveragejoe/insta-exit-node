"""Minimal Tailscale API client (stdlib only).

Operator-side only. This module handles the OAuth client secret, which must never
leave the operator's machine — it is not copied to the VM, not written into
cloud-init user-data, and not logged. Only the short-lived auth key it mints
travels to the target host.

Endpoint details verified against Tailscale's published OpenAPI spec, except the
OAuth token exchange, which that spec does not describe (it documents only
bearer-authenticated calls). See https://tailscale.com/kb/1623/ for that flow.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.tailscale.com/api/v2"
OAUTH_TOKEN_URL = f"{API_BASE}/oauth/token"

DEFAULT_TAILNET = "-"
DEFAULT_KEY_EXPIRY = 600
DESCRIPTION_MAX = 50


class TailscaleAPIError(Exception):
    """An API call failed. Message is safe to print — never contains credentials."""


def _error_detail(exc):
    """Pull a human-readable reason out of an HTTPError body.

    Tailscale returns {"message": ...}; the OAuth endpoint follows RFC 6749 and
    returns {"error": ..., "error_description": ...} instead.
    """
    try:
        body = exc.read().decode("utf-8", "replace")
    except OSError:
        return f"HTTP {exc.code}"
    detail = None
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        detail = parsed.get("message") or parsed.get("error_description") or parsed.get("error")
    return f"HTTP {exc.code}: {detail or body.strip() or exc.reason}"


def _request(url, *, token=None, data=None, content_type=None, accept=None, timeout=30):
    req = urllib.request.Request(url, data=data)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if content_type:
        req.add_header("Content-Type", content_type)
    if accept:
        req.add_header("Accept", accept)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise TailscaleAPIError(_error_detail(exc)) from None
    except urllib.error.URLError as exc:
        raise TailscaleAPIError(f"could not reach {url}: {exc.reason}") from None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise TailscaleAPIError(f"invalid JSON in response from {url}") from None


def exchange_oauth_token(client_id, client_secret, timeout=30):
    """Exchange OAuth client credentials for a short-lived API access token."""
    data = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        }
    ).encode()
    payload = _request(
        OAUTH_TOKEN_URL,
        data=data,
        content_type="application/x-www-form-urlencoded",
        timeout=timeout,
    )
    token = payload.get("access_token")
    if not token:
        raise TailscaleAPIError("OAuth token response contained no access_token")
    return token


def sanitize_description(text):
    """Fit a key description to the API's limits.

    The API accepts at most 50 characters: alphanumerics, hyphens and spaces.
    Hostnames are user-supplied, so anything else is replaced rather than sent.
    """
    cleaned = "".join(
        c if ((c.isascii() and c.isalnum()) or c in "- ") else "-" for c in text
    )
    return cleaned[:DESCRIPTION_MAX].strip() or "insta-exit-node"


def create_auth_key(
    token,
    *,
    tags,
    hostname=None,
    expiry_seconds=DEFAULT_KEY_EXPIRY,
    tailnet=DEFAULT_TAILNET,
    ephemeral=True,
    reusable=False,
    timeout=30,
):
    """Mint an auth key and return its secret material.

    Defaults are deliberately narrow: single-use, ephemeral, pre-authorized, and
    short-lived. A key recovered later (e.g. from cloud-init user-data via the
    metadata service) is then both consumed and expired.
    """
    if not tags:
        raise TailscaleAPIError(
            "at least one tag is required: keys minted through the API are owned "
            "by the tailnet, and Tailscale requires those keys to be tagged"
        )
    body = {
        # Explicit even though "auth" is the default: this endpoint also mints
        # OAuth clients and federated identities, and leaving the type implicit
        # is what produces the opaque "exactly one capability scope must be
        # populated" error. See tailscale/tailscale#11914.
        "keyType": "auth",
        "description": sanitize_description(
            f"insta-exit-node {hostname}" if hostname else "insta-exit-node"
        ),
        # Tags belong here, NOT at the top level of the body. A top-level "tags"
        # applies only to OAuth clients and federated identities and would be
        # ignored for an auth key, yielding an untagged key that the tailnet
        # then rejects.
        "capabilities": {
            "devices": {
                "create": {
                    "reusable": reusable,
                    "ephemeral": ephemeral,
                    "preauthorized": True,
                    "tags": list(tags),
                }
            }
        },
    }
    # Never send 0 — omitting the field means "use the tailnet default" whereas
    # 0 is rejected.
    if expiry_seconds:
        body["expirySeconds"] = int(expiry_seconds)

    url = f"{API_BASE}/tailnet/{urllib.parse.quote(tailnet, safe='')}/keys"
    payload = _request(
        url,
        token=token,
        data=json.dumps(body).encode(),
        content_type="application/json",
        timeout=timeout,
    )
    key = payload.get("key")
    if not key:
        raise TailscaleAPIError("key creation response contained no key material")
    return key


def get_policy_file(token, tailnet=DEFAULT_TAILNET, timeout=30):
    """Fetch the tailnet policy file as JSON.

    Requested as JSON rather than the default HuJSON so the stdlib can parse it.
    This is read-only by design: the policy file is HuJSON, and writing it back
    from parsed JSON would strip every comment in it.
    """
    url = f"{API_BASE}/tailnet/{urllib.parse.quote(tailnet, safe='')}/acl"
    return _request(url, token=token, accept="application/json", timeout=timeout)


def check_acl(policy, tags):
    """Return a list of policy problems that would break this exit node.

    An empty list means the tailnet is configured correctly for these tags.
    """
    policy = policy or {}
    tag_owners = policy.get("tagOwners") or {}
    exit_approvers = (policy.get("autoApprovers") or {}).get("exitNode") or []

    problems = []
    for tag in tags:
        if tag not in tag_owners:
            problems.append(
                f'{tag} is not declared in tagOwners, so creating a key with it will fail.\n'
                f'  Add to your policy file:\n'
                f'      "tagOwners": {{ "{tag}": ["autogroup:admin"] }}'
            )
        if tag not in exit_approvers and "*" not in exit_approvers:
            problems.append(
                f'{tag} is not auto-approved as an exit node, so no device could route through it.\n'
                f'  Add to your policy file:\n'
                f'      "autoApprovers": {{ "exitNode": ["{tag}"] }}'
            )
    return problems
