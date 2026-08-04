"""Tests for the Tailscale API client.

Runs against a local stub HTTP server, so no credentials or network are needed:
    python3 -m unittest discover -s tests -v

These assert the shape of what we send, which is where the sharp edges are — the
API rejects subtly-wrong payloads with unhelpful messages.
"""
import http.server
import json
import os
import subprocess
import sys
import threading
import unittest
import urllib.parse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import tsapi  # noqa: E402


class _StubHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # keep test output clean

    def _handle(self, body=b""):
        self.server.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": dict(self.headers),
                "body": body,
            }
        )
        code, payload = self.server.responses.get(
            self.path.split("?")[0], (200, {})
        )
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self._handle()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self._handle(self.rfile.read(length))


class StubServerTest(unittest.TestCase):
    def setUp(self):
        os.environ["no_proxy"] = "*"  # never route localhost through a proxy
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _StubHandler)
        self.server.requests = []
        self.server.responses = {}
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{self.server.server_port}/api/v2"
        self._saved = (tsapi.API_BASE, tsapi.OAUTH_TOKEN_URL)
        tsapi.API_BASE = base
        tsapi.OAUTH_TOKEN_URL = f"{base}/oauth/token"

    def tearDown(self):
        tsapi.API_BASE, tsapi.OAUTH_TOKEN_URL = self._saved
        self.server.shutdown()
        self.server.server_close()

    def respond(self, path, code, payload):
        self.server.responses[path] = (code, payload)

    @property
    def last(self):
        return self.server.requests[-1]

    def last_json(self):
        return json.loads(self.last["body"])


class TestOAuthExchange(StubServerTest):
    def test_posts_client_credentials_form(self):
        self.respond("/api/v2/oauth/token", 200, {"access_token": "tskey-api-abc"})
        token = tsapi.exchange_oauth_token("kID", "tskey-client-secret")

        self.assertEqual(token, "tskey-api-abc")
        self.assertEqual(self.last["method"], "POST")
        self.assertEqual(
            self.last["headers"]["Content-Type"], "application/x-www-form-urlencoded"
        )
        form = urllib.parse.parse_qs(self.last["body"].decode())
        self.assertEqual(form["grant_type"], ["client_credentials"])
        self.assertEqual(form["client_id"], ["kID"])
        self.assertEqual(form["client_secret"], ["tskey-client-secret"])

    def test_missing_token_is_an_error(self):
        self.respond("/api/v2/oauth/token", 200, {"token_type": "Bearer"})
        with self.assertRaises(tsapi.TailscaleAPIError):
            tsapi.exchange_oauth_token("kID", "secret")

    def test_oauth_error_body_is_surfaced(self):
        self.respond(
            "/api/v2/oauth/token",
            401,
            {"error": "invalid_client", "error_description": "bad secret"},
        )
        with self.assertRaises(tsapi.TailscaleAPIError) as ctx:
            tsapi.exchange_oauth_token("kID", "secret")
        self.assertIn("bad secret", str(ctx.exception))


class TestCreateAuthKey(StubServerTest):
    KEYS_PATH = "/api/v2/tailnet/-/keys"

    def mint(self, **kwargs):
        self.respond(self.KEYS_PATH, 200, {"key": "tskey-auth-minted"})
        kwargs.setdefault("tags", ["tag:exit"])
        return tsapi.create_auth_key("tok", **kwargs)

    def test_returns_key_and_authenticates_as_bearer(self):
        self.assertEqual(self.mint(), "tskey-auth-minted")
        self.assertEqual(self.last["headers"]["Authorization"], "Bearer tok")
        self.assertEqual(self.last["headers"]["Content-Type"], "application/json")

    def test_key_type_is_explicit(self):
        # The endpoint also mints OAuth clients and federated identities; leaving
        # keyType implicit is what produces the opaque capability-scope error.
        self.mint()
        self.assertEqual(self.last_json()["keyType"], "auth")

    def test_tags_are_nested_not_top_level(self):
        # A top-level "tags" applies only to OAuth clients and would be silently
        # ignored here, producing an untagged key the tailnet then rejects.
        self.mint(tags=["tag:exit", "tag:extra"])
        body = self.last_json()
        self.assertNotIn("tags", body)
        self.assertEqual(
            body["capabilities"]["devices"]["create"]["tags"],
            ["tag:exit", "tag:extra"],
        )

    def test_key_defaults_are_narrow(self):
        self.mint()
        create = self.last_json()["capabilities"]["devices"]["create"]
        self.assertFalse(create["reusable"])
        self.assertTrue(create["ephemeral"])
        self.assertTrue(create["preauthorized"])

    def test_expiry_is_sent(self):
        self.mint(expiry_seconds=600)
        self.assertEqual(self.last_json()["expirySeconds"], 600)

    def test_zero_expiry_is_omitted_not_sent(self):
        # Sending 0 is rejected; omitting means "tailnet default".
        self.mint(expiry_seconds=0)
        self.assertNotIn("expirySeconds", self.last_json())

    def test_description_is_sanitized_and_truncated(self):
        self.mint(hostname='evil"; drop $(x) ' + "z" * 80)
        description = self.last_json()["description"]
        self.assertLessEqual(len(description), tsapi.DESCRIPTION_MAX)
        for char in '";$()':
            self.assertNotIn(char, description)

    def test_tailnet_is_url_encoded(self):
        self.respond("/api/v2/tailnet/my.org/keys", 200, {"key": "k"})
        tsapi.create_auth_key("tok", tags=["tag:exit"], tailnet="my.org")
        self.assertEqual(self.last["path"], "/api/v2/tailnet/my.org/keys")

    def test_untagged_request_is_refused_locally(self):
        with self.assertRaises(tsapi.TailscaleAPIError):
            tsapi.create_auth_key("tok", tags=[])
        self.assertEqual(self.server.requests, [])  # never hit the network

    def test_missing_key_material_is_an_error(self):
        self.respond(self.KEYS_PATH, 200, {"id": "k123"})
        with self.assertRaises(tsapi.TailscaleAPIError):
            tsapi.create_auth_key("tok", tags=["tag:exit"])

    def test_api_error_message_is_surfaced(self):
        self.respond(self.KEYS_PATH, 400, {"message": "tag not in policy file"})
        with self.assertRaises(tsapi.TailscaleAPIError) as ctx:
            tsapi.create_auth_key("tok", tags=["tag:exit"])
        self.assertIn("tag not in policy file", str(ctx.exception))


class TestPolicyFile(StubServerTest):
    def test_requests_json_rather_than_hujson(self):
        self.respond("/api/v2/tailnet/-/acl", 200, {"tagOwners": {}})
        tsapi.get_policy_file("tok")
        self.assertEqual(self.last["headers"]["Accept"], "application/json")
        self.assertEqual(self.last["method"], "GET")


class TestCheckAcl(unittest.TestCase):
    GOOD = {
        "tagOwners": {"tag:exit": ["autogroup:admin"]},
        "autoApprovers": {"exitNode": ["tag:exit"]},
    }

    def test_correct_policy_has_no_problems(self):
        self.assertEqual(tsapi.check_acl(self.GOOD, ["tag:exit"]), [])

    def test_missing_tag_owner_is_reported(self):
        policy = {"autoApprovers": {"exitNode": ["tag:exit"]}}
        problems = tsapi.check_acl(policy, ["tag:exit"])
        self.assertEqual(len(problems), 1)
        self.assertIn("tagOwners", problems[0])

    def test_missing_auto_approver_is_reported(self):
        policy = {"tagOwners": {"tag:exit": []}}
        problems = tsapi.check_acl(policy, ["tag:exit"])
        self.assertEqual(len(problems), 1)
        self.assertIn("autoApprovers", problems[0])

    def test_empty_policy_reports_both(self):
        self.assertEqual(len(tsapi.check_acl({}, ["tag:exit"])), 2)

    def test_none_policy_does_not_crash(self):
        self.assertEqual(len(tsapi.check_acl(None, ["tag:exit"])), 2)

    def test_wildcard_approver_is_accepted(self):
        policy = {"tagOwners": {"tag:exit": []}, "autoApprovers": {"exitNode": ["*"]}}
        self.assertEqual(tsapi.check_acl(policy, ["tag:exit"]), [])

    def test_each_tag_checked_independently(self):
        problems = tsapi.check_acl(self.GOOD, ["tag:exit", "tag:other"])
        self.assertEqual(len(problems), 2)
        self.assertTrue(all("tag:other" in p for p in problems))


class TestSanitizeDescription(unittest.TestCase):
    def test_keeps_allowed_characters(self):
        self.assertEqual(tsapi.sanitize_description("insta exit-node 1"), "insta exit-node 1")

    def test_replaces_disallowed_characters(self):
        self.assertEqual(tsapi.sanitize_description("a:b/c"), "a-b-c")

    def test_non_ascii_is_replaced(self):
        # str.isalnum() accepts non-ASCII letters; the API does not.
        self.assertEqual(tsapi.sanitize_description("uber"), "uber")
        self.assertNotIn("ü", tsapi.sanitize_description("über"))

    def test_never_returns_empty(self):
        self.assertTrue(tsapi.sanitize_description("!!!"))
        self.assertTrue(tsapi.sanitize_description(""))

    def test_respects_length_limit(self):
        self.assertLessEqual(len(tsapi.sanitize_description("x" * 200)), tsapi.DESCRIPTION_MAX)


class TestCliOutputStreams(unittest.TestCase):
    """cloud-init output is redirected to a file, so stdout must stay clean."""

    def test_cloud_init_stdout_has_no_log_lines(self):
        env = dict(os.environ, TS_AUTHKEY="tskey-auth-test")
        result = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "bin" / "insta-exit-node"),
                "cloud-init",
                "--hostname", "insta-exit-1",
                "--tag", "tag:exit",
            ],
            capture_output=True, text=True, env=env, check=True,
        )
        self.assertTrue(result.stdout.startswith("#cloud-config"))
        self.assertNotIn("[insta-exit-node]", result.stdout)


def _load_cli():
    """Import bin/insta-exit-node as a module (it has no .py extension)."""
    from importlib.machinery import SourceFileLoader
    import importlib.util

    loader = SourceFileLoader("insta_exit_node", str(REPO_ROOT / "bin" / "insta-exit-node"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class TestKeyResolutionOrder(unittest.TestCase):
    """An explicit key must always win, so existing setups keep working."""

    def setUp(self):
        self.cli = _load_cli()
        self.minted = []
        self.cli.mint_authkey = lambda args: self.minted.append(args) or "tskey-auth-minted"
        for var in ("TS_AUTHKEY", "TS_OAUTH_CLIENT_ID", "TS_OAUTH_CLIENT_SECRET", "TS_API_KEY"):
            os.environ.pop(var, None)

    def args(self, **kwargs):
        import argparse

        defaults = dict(authkey=None, authkey_file=None, tags=["tag:exit"], hostname="h")
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    def test_explicit_flag_wins_over_minting(self):
        key = self.cli.resolve_authkey(self.args(authkey="tskey-auth-explicit"))
        self.assertEqual(key, "tskey-auth-explicit")
        self.assertEqual(self.minted, [])

    def test_env_var_wins_over_minting(self):
        os.environ["TS_AUTHKEY"] = "tskey-auth-fromenv"
        self.addCleanup(os.environ.pop, "TS_AUTHKEY", None)
        self.assertEqual(self.cli.resolve_authkey(self.args()), "tskey-auth-fromenv")
        self.assertEqual(self.minted, [])

    def test_authkey_file_wins_over_minting(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".key", delete=False) as handle:
            handle.write("tskey-auth-fromfile\n")
        self.addCleanup(os.unlink, handle.name)
        key = self.cli.resolve_authkey(self.args(authkey_file=handle.name))
        self.assertEqual(key, "tskey-auth-fromfile")
        self.assertEqual(self.minted, [])

    def test_falls_back_to_minting(self):
        self.assertEqual(self.cli.resolve_authkey(self.args()), "tskey-auth-minted")
        self.assertEqual(len(self.minted), 1)


class TestPreflightGate(unittest.TestCase):
    def setUp(self):
        self.cli = _load_cli()

    def test_bad_policy_blocks_before_provisioning(self):
        self.cli.tsapi.get_policy_file = lambda *a, **k: {}
        with self.assertRaises(SystemExit) as ctx:
            self.cli.preflight("tok", "-", ["tag:exit"])
        message = str(ctx.exception)
        self.assertIn("autoApprovers", message)
        self.assertIn("Nothing was provisioned", message)

    def test_good_policy_passes(self):
        self.cli.tsapi.get_policy_file = lambda *a, **k: {
            "tagOwners": {"tag:exit": []},
            "autoApprovers": {"exitNode": ["tag:exit"]},
        }
        self.cli.preflight("tok", "-", ["tag:exit"])  # must not raise

    def test_unreadable_policy_warns_but_continues(self):
        def boom(*args, **kwargs):
            raise tsapi.TailscaleAPIError("HTTP 403: forbidden")

        self.cli.tsapi.get_policy_file = boom
        self.cli.preflight("tok", "-", ["tag:exit"])  # must not raise


if __name__ == "__main__":
    unittest.main()
