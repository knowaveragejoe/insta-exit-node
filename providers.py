"""Thin REST drivers for the VM lifecycle.

Each provider needs three calls: one POST to create, one GET to look up, one
DELETE to remove. That is small enough to write directly against the HTTP API,
so this module has no dependencies and no SDKs. Adding a provider means adding
one subclass and one registry entry — not a new abstraction layer.

Operator-side only, like tsapi: provider tokens never reach the VM.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

# Applied as a DigitalOcean tag / Hetzner label so our own VMs are identifiable.
MANAGED_TAG = "insta-exit-node"


class ProviderError(Exception):
    """A provider API call failed. Message is safe to print — never a token."""


class Provider:
    """Base driver. Subclasses map their API onto create/get/find/delete."""

    name = ""
    token_env = ()
    base = ""
    defaults = {}
    ssh_key_help = "SSH key registered with the provider"

    def __init__(self, token):
        self.token = token

    # --- subclass hooks -------------------------------------------------

    @staticmethod
    def _message(payload, fallback):
        """Pull the human-readable error out of a provider's error body."""
        return fallback

    @staticmethod
    def _shape(raw):
        """Normalise a provider's server object to {id, name, ip}."""
        raise NotImplementedError

    # --- shared plumbing ------------------------------------------------

    def _http_error(self, exc):
        try:
            payload = json.loads(exc.read().decode("utf-8", "replace"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return f"HTTP {exc.code}: {self._message(payload, exc.reason)}"

    def _req(self, path, *, method="GET", body=None, timeout=30):
        url = f"{self.base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise ProviderError(self._http_error(exc)) from None
        except urllib.error.URLError as exc:
            raise ProviderError(f"could not reach {url}: {exc.reason}") from None
        if not raw.strip():
            return {}  # DELETE commonly returns an empty body
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise ProviderError(f"invalid JSON in response from {url}") from None

    def wait_for_ip(self, server_id, timeout=180, interval=3, sleep=time.sleep):
        """Poll until the server has a public IPv4 address."""
        deadline = time.monotonic() + timeout
        while True:
            server = self.get(server_id)
            if server["ip"]:
                return server
            if time.monotonic() >= deadline:
                raise ProviderError(
                    f"timed out after {timeout}s waiting for a public IP on {server_id}"
                )
            sleep(interval)


def _id_or_text(value):
    """DigitalOcean accepts an SSH key as a numeric ID or a fingerprint string."""
    text = str(value)
    return int(text) if text.isdigit() else text


class DigitalOcean(Provider):
    name = "digitalocean"
    token_env = ("DIGITALOCEAN_TOKEN", "DO_API_TOKEN")
    base = "https://api.digitalocean.com/v2"
    defaults = {"region": "nyc3", "size": "s-1vcpu-512mb-10gb", "image": "ubuntu-24-04-x64"}
    ssh_key_help = "DigitalOcean SSH key ID or fingerprint (doctl compute ssh-key list)"

    @staticmethod
    def _message(payload, fallback):
        return payload.get("message") or fallback

    @staticmethod
    def _shape(raw):
        ip = ""
        for net in (raw.get("networks") or {}).get("v4") or []:
            if net.get("type") == "public":
                ip = net.get("ip_address") or ""
                break
        return {"id": str(raw.get("id")), "name": raw.get("name") or "", "ip": ip}

    def create(self, *, name, region, size, image, ssh_keys, user_data):
        body = {
            "name": name,
            "region": region,
            "size": size,
            "image": image,
            "ssh_keys": [_id_or_text(k) for k in ssh_keys],
            "user_data": user_data,
            "tags": [MANAGED_TAG],
            "ipv6": True,
        }
        # 202 Accepted: the droplet exists but has no IP yet, hence wait_for_ip.
        return self._shape(self._req("/droplets", method="POST", body=body)["droplet"])

    def get(self, server_id):
        return self._shape(self._req(f"/droplets/{server_id}")["droplet"])

    def find(self, name):
        query = urllib.parse.urlencode({"name": name})
        payload = self._req(f"/droplets?{query}")
        return [self._shape(d) for d in payload.get("droplets") or []]

    def delete(self, server_id):
        self._req(f"/droplets/{server_id}", method="DELETE")


class Hetzner(Provider):
    name = "hetzner"
    token_env = ("HCLOUD_TOKEN",)
    base = "https://api.hetzner.cloud/v1"
    defaults = {"region": "nbg1", "size": "cx22", "image": "ubuntu-24.04"}
    ssh_key_help = "Hetzner SSH key name or ID (hcloud ssh-key list)"

    @staticmethod
    def _message(payload, fallback):
        return (payload.get("error") or {}).get("message") or fallback

    @staticmethod
    def _shape(raw):
        ipv4 = (raw.get("public_net") or {}).get("ipv4") or {}
        return {
            "id": str(raw.get("id")),
            "name": raw.get("name") or "",
            "ip": ipv4.get("ip") or "",
        }

    def _ssh_key_ids(self, ssh_keys):
        """Hetzner takes numeric SSH key IDs only, so resolve any names first."""
        ids = []
        for key in ssh_keys:
            text = str(key)
            if text.isdigit():
                ids.append(int(text))
                continue
            query = urllib.parse.urlencode({"name": text})
            found = self._req(f"/ssh_keys?{query}").get("ssh_keys") or []
            if not found:
                raise ProviderError(f"no Hetzner SSH key named {text!r}")
            ids.append(found[0]["id"])
        return ids

    def create(self, *, name, region, size, image, ssh_keys, user_data):
        body = {
            "name": name,
            "server_type": size,
            "image": image,
            "location": region,
            "user_data": user_data,
            "labels": {MANAGED_TAG: "true"},
        }
        if ssh_keys:
            body["ssh_keys"] = self._ssh_key_ids(ssh_keys)
        return self._shape(self._req("/servers", method="POST", body=body)["server"])

    def get(self, server_id):
        return self._shape(self._req(f"/servers/{server_id}")["server"])

    def find(self, name):
        query = urllib.parse.urlencode({"name": name})
        payload = self._req(f"/servers?{query}")
        return [self._shape(s) for s in payload.get("servers") or []]

    def delete(self, server_id):
        self._req(f"/servers/{server_id}", method="DELETE")


PROVIDERS = {cls.name: cls for cls in (DigitalOcean, Hetzner)}


def get_provider(name, token=None, env=None):
    """Resolve a provider driver and its API token."""
    env = os.environ if env is None else env
    try:
        cls = PROVIDERS[name]
    except KeyError:
        raise ProviderError(
            f"unknown provider {name!r}; choose from {', '.join(sorted(PROVIDERS))}"
        ) from None
    if not token:
        for var in cls.token_env:
            token = env.get(var)
            if token:
                break
    if not token:
        raise ProviderError(
            f"no API token for {name}: set "
            f"{' or '.join(cls.token_env)}, or pass --provider-token"
        )
    return cls(token)
