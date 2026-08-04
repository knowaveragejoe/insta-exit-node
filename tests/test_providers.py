"""Tests for the provider drivers.

Runs against a local stub HTTP server — no tokens, no network, no VMs created:
    python3 -m unittest discover -s tests -v
"""
import http.server
import json
import os
import sys
import threading
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import providers  # noqa: E402


class _StubHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _handle(self, body=b""):
        self.server.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": dict(self.headers),
                "body": body,
            }
        )
        queue = self.server.responses.get((self.command, self.path.split("?")[0]))
        if not queue:
            code, payload = 200, {}
        elif len(queue) > 1:
            code, payload = queue.pop(0)  # successive calls can differ
        else:
            code, payload = queue[0]
        raw = json.dumps(payload).encode() if payload is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self._handle()

    def do_DELETE(self):
        self._handle()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self._handle(self.rfile.read(length))


class DriverTest(unittest.TestCase):
    driver = providers.DigitalOcean

    def setUp(self):
        os.environ["no_proxy"] = "*"
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _StubHandler)
        self.server.requests = []
        self.server.responses = {}
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.provider = self.driver("token-abc")
        self.provider.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def respond(self, method, path, payload, code=200):
        self.server.responses.setdefault((method, path), []).append((code, payload))

    @property
    def last(self):
        return self.server.requests[-1]

    def last_json(self):
        return json.loads(self.last["body"])

    def make(self, **overrides):
        kwargs = dict(
            name="insta-exit-1",
            region="reg",
            size="sz",
            image="img",
            ssh_keys=[],
            user_data="#cloud-config\n",
        )
        kwargs.update(overrides)
        return self.provider.create(**kwargs)


DO_DROPLET = {
    "id": 12345,
    "name": "insta-exit-1",
    "networks": {
        "v4": [
            {"ip_address": "10.1.0.5", "type": "private"},
            {"ip_address": "203.0.113.9", "type": "public"},
        ]
    },
}


class TestDigitalOcean(DriverTest):
    driver = providers.DigitalOcean

    def test_create_sends_expected_body(self):
        self.respond("POST", "/droplets", {"droplet": DO_DROPLET})
        server = self.make(region="nyc3", size="s-1vcpu-512mb-10gb", image="ubuntu-24-04-x64")

        body = self.last_json()
        self.assertEqual(body["name"], "insta-exit-1")
        self.assertEqual(body["region"], "nyc3")
        self.assertEqual(body["size"], "s-1vcpu-512mb-10gb")
        self.assertEqual(body["image"], "ubuntu-24-04-x64")
        self.assertEqual(body["user_data"], "#cloud-config\n")
        self.assertEqual(body["tags"], [providers.MANAGED_TAG])
        self.assertEqual(self.last["headers"]["Authorization"], "Bearer token-abc")
        self.assertEqual(server, {"id": "12345", "name": "insta-exit-1", "ip": "203.0.113.9"})

    def test_public_ip_is_chosen_over_private(self):
        self.respond("POST", "/droplets", {"droplet": DO_DROPLET})
        self.assertEqual(self.make()["ip"], "203.0.113.9")

    def test_missing_ip_yields_empty_string_not_error(self):
        # A fresh droplet has no network yet; create returns 202 regardless.
        self.respond("POST", "/droplets", {"droplet": {"id": 1, "name": "x"}})
        self.assertEqual(self.make()["ip"], "")

    def test_numeric_ssh_key_sent_as_int_fingerprint_as_string(self):
        self.respond("POST", "/droplets", {"droplet": DO_DROPLET})
        self.make(ssh_keys=["771", "aa:bb:cc"])
        self.assertEqual(self.last_json()["ssh_keys"], [771, "aa:bb:cc"])

    def test_find_filters_by_name(self):
        self.respond("GET", "/droplets", {"droplets": [DO_DROPLET]})
        found = self.provider.find("insta-exit-1")
        self.assertEqual(self.last["path"], "/droplets?name=insta-exit-1")
        self.assertEqual(found[0]["id"], "12345")

    def test_find_returns_empty_list_when_absent(self):
        self.respond("GET", "/droplets", {"droplets": []})
        self.assertEqual(self.provider.find("nope"), [])

    def test_delete_issues_delete(self):
        self.respond("DELETE", "/droplets/12345", None)
        self.provider.delete("12345")
        self.assertEqual(self.last["method"], "DELETE")
        self.assertEqual(self.last["path"], "/droplets/12345")

    def test_error_message_is_surfaced(self):
        self.respond("POST", "/droplets", {"message": "you do not have access"}, code=403)
        with self.assertRaises(providers.ProviderError) as ctx:
            self.make()
        self.assertIn("you do not have access", str(ctx.exception))


HZ_SERVER = {
    "id": 98765,
    "name": "insta-exit-1",
    "public_net": {"ipv4": {"ip": "198.51.100.7"}},
}


class TestHetzner(DriverTest):
    driver = providers.Hetzner

    def test_create_sends_expected_body(self):
        self.respond("POST", "/servers", {"server": HZ_SERVER})
        server = self.make(region="nbg1", size="cx22", image="ubuntu-24.04")

        body = self.last_json()
        self.assertEqual(body["name"], "insta-exit-1")
        self.assertEqual(body["server_type"], "cx22")   # not "size"
        self.assertEqual(body["location"], "nbg1")      # not "region"
        self.assertEqual(body["image"], "ubuntu-24.04")
        self.assertEqual(body["labels"], {providers.MANAGED_TAG: "true"})
        self.assertEqual(server, {"id": "98765", "name": "insta-exit-1", "ip": "198.51.100.7"})

    def test_ssh_key_names_are_resolved_to_ids(self):
        self.respond("GET", "/ssh_keys", {"ssh_keys": [{"id": 4242, "name": "laptop"}]})
        self.respond("POST", "/servers", {"server": HZ_SERVER})
        self.make(ssh_keys=["laptop"])

        lookup = self.server.requests[0]
        self.assertEqual(lookup["path"], "/ssh_keys?name=laptop")
        self.assertEqual(self.last_json()["ssh_keys"], [4242])

    def test_numeric_ssh_key_skips_lookup(self):
        self.respond("POST", "/servers", {"server": HZ_SERVER})
        self.make(ssh_keys=["4242"])
        self.assertEqual(self.last_json()["ssh_keys"], [4242])
        self.assertEqual(len(self.server.requests), 1)  # no /ssh_keys call

    def test_unknown_ssh_key_name_is_an_error(self):
        self.respond("GET", "/ssh_keys", {"ssh_keys": []})
        with self.assertRaises(providers.ProviderError) as ctx:
            self.make(ssh_keys=["missing"])
        self.assertIn("missing", str(ctx.exception))

    def test_omits_ssh_keys_when_none_given(self):
        self.respond("POST", "/servers", {"server": HZ_SERVER})
        self.make(ssh_keys=[])
        self.assertNotIn("ssh_keys", self.last_json())

    def test_nested_error_message_is_surfaced(self):
        self.respond(
            "POST", "/servers",
            {"error": {"code": "invalid_input", "message": "server_type not found"}},
            code=400,
        )
        with self.assertRaises(providers.ProviderError) as ctx:
            self.make()
        self.assertIn("server_type not found", str(ctx.exception))

    def test_find_filters_by_name(self):
        self.respond("GET", "/servers", {"servers": [HZ_SERVER]})
        self.assertEqual(self.provider.find("insta-exit-1")[0]["ip"], "198.51.100.7")
        self.assertEqual(self.last["path"], "/servers?name=insta-exit-1")


class TestWaitForIp(DriverTest):
    driver = providers.DigitalOcean

    def test_polls_until_an_ip_appears(self):
        self.respond("GET", "/droplets/1", {"droplet": {"id": 1, "name": "x"}})
        self.respond("GET", "/droplets/1", {"droplet": DO_DROPLET})
        slept = []
        server = self.provider.wait_for_ip(1, timeout=30, interval=3, sleep=slept.append)
        self.assertEqual(server["ip"], "203.0.113.9")
        self.assertEqual(slept, [3])  # slept exactly once between polls

    def test_times_out(self):
        self.respond("GET", "/droplets/1", {"droplet": {"id": 1, "name": "x"}})
        with self.assertRaises(providers.ProviderError) as ctx:
            self.provider.wait_for_ip(1, timeout=0, sleep=lambda _: None)
        self.assertIn("timed out", str(ctx.exception))


class TestGetProvider(unittest.TestCase):
    def test_unknown_provider_lists_valid_choices(self):
        with self.assertRaises(providers.ProviderError) as ctx:
            providers.get_provider("linode", env={})
        self.assertIn("digitalocean", str(ctx.exception))

    def test_token_read_from_env(self):
        provider = providers.get_provider(
            "digitalocean", env={"DIGITALOCEAN_TOKEN": "from-env"}
        )
        self.assertEqual(provider.token, "from-env")

    def test_alternate_env_var_is_accepted(self):
        provider = providers.get_provider("digitalocean", env={"DO_API_TOKEN": "alt"})
        self.assertEqual(provider.token, "alt")

    def test_explicit_token_wins(self):
        provider = providers.get_provider(
            "hetzner", token="explicit", env={"HCLOUD_TOKEN": "from-env"}
        )
        self.assertEqual(provider.token, "explicit")

    def test_missing_token_names_the_env_var(self):
        with self.assertRaises(providers.ProviderError) as ctx:
            providers.get_provider("hetzner", env={})
        self.assertIn("HCLOUD_TOKEN", str(ctx.exception))

    def test_every_provider_declares_defaults(self):
        for name, cls in providers.PROVIDERS.items():
            for field in ("region", "size", "image"):
                self.assertIn(field, cls.defaults, f"{name} missing default {field}")


class _FakeProvider:
    name = "digitalocean"
    defaults = {"region": "r", "size": "s", "image": "i"}

    def __init__(self, matches):
        self.matches = matches
        self.deleted = []

    def find(self, name):
        return list(self.matches)

    def delete(self, server_id):
        self.deleted.append(server_id)


class _FakeStdin:
    def __init__(self, tty):
        self._tty = tty

    def isatty(self):
        return self._tty


class TestDestroyGuard(unittest.TestCase):
    """Destroying a VM is irreversible, so it must never happen by accident."""

    MATCH = [{"id": "12345", "name": "insta-exit-1", "ip": "203.0.113.9"}]

    def setUp(self):
        from test_tsapi import _load_cli  # tests dir is on sys.path under discovery

        self.cli = _load_cli()
        self.fake = _FakeProvider(self.MATCH)
        # cli.providers is the same module object these tests import, so the
        # patch must be undone or it leaks into every other test.
        self.addCleanup(
            setattr, self.cli.providers, "get_provider", providers.get_provider
        )
        self.cli.providers.get_provider = lambda *a, **k: self.fake
        self.addCleanup(setattr, self.cli.sys, "stdin", self.cli.sys.stdin)

    def args(self, **kwargs):
        import argparse

        defaults = dict(provider="digitalocean", provider_token=None,
                        name="insta-exit-1", yes=False)
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    def test_refuses_without_yes_when_not_a_terminal(self):
        self.cli.sys.stdin = _FakeStdin(tty=False)
        with self.assertRaises(SystemExit) as ctx:
            self.cli.cmd_destroy(self.args())
        self.assertIn("--yes", str(ctx.exception))
        self.assertEqual(self.fake.deleted, [])  # nothing destroyed

    def test_yes_flag_destroys_without_prompting(self):
        self.cli.sys.stdin = _FakeStdin(tty=False)
        self.cli.cmd_destroy(self.args(yes=True))
        self.assertEqual(self.fake.deleted, ["12345"])

    def test_declining_the_prompt_destroys_nothing(self):
        self.cli.sys.stdin = _FakeStdin(tty=True)
        self.cli.input = lambda *a: "no"
        with self.assertRaises(SystemExit) as ctx:
            self.cli.cmd_destroy(self.args())
        self.assertIn("Aborted", str(ctx.exception))
        self.assertEqual(self.fake.deleted, [])

    def test_confirming_the_prompt_destroys(self):
        self.cli.sys.stdin = _FakeStdin(tty=True)
        self.cli.input = lambda *a: "yes"
        self.cli.cmd_destroy(self.args())
        self.assertEqual(self.fake.deleted, ["12345"])

    def test_unknown_name_is_an_error_not_a_no_op(self):
        self.fake.matches = []
        with self.assertRaises(SystemExit) as ctx:
            self.cli.cmd_destroy(self.args(name="ghost"))
        self.assertIn("ghost", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
