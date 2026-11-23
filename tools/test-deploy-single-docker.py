#!/usr/bin/env python3
"""Run the standalone single-node deployment script without touching host Docker."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
_tmp = tempfile.TemporaryDirectory(prefix="ham-deploy-single-config-")
for key, name in {
    "HAM_DATA_DIR": "manager", "HAM_CERT_DIR": "certs",
    "HAM_HAPROXY_CFG": "haproxy.cfg", "HAM_KEEPALIVED_CFG": "keepalived.conf",
    "HAM_ERROR_DIR": "errors", "HAM_CROWDSEC_SPOE": "crowdsec.cfg",
}.items():
    os.environ[key] = str(Path(_tmp.name) / name)
os.environ["HAM_DRY_RUN"] = "1"
sys.path.insert(0, str(ROOT))

import ham  # noqa: E402
from ham import apply, auth, haproxy, startup, vrrp  # noqa: E402
from ham.config import CONF_PATH, load_config  # noqa: E402
from ham.validate import check_file_config  # noqa: E402
from tools.integration.deploy_fixture import DeploymentFixture  # noqa: E402


class SingleNodeDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ham-deploy-single-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fixture = DeploymentFixture(self.root)
        self.password = "test-password-$with spaces"
        self.api_key = "test-standalone-api-key-1234567890-abcd"
        self.data, self.stage, self.env = self.fixture.data, self.fixture.stage, self.fixture.env
        self.env["TEST_HOSTNAME"] = "proxy-solo"
        self.env["TEST_ADDRESSES"] = json.dumps([
            {"ifname": "lo", "operstate": "UNKNOWN", "addr_info": [{"local": "127.0.0.1"}]},
            {"ifname": "ens192", "operstate": "UP", "addr_info": [{"local": "10.40.0.11"}]},
        ])
        self.args = [
            "--interface", "ens192",
            "--admin-password", self.password,
            "--data-dir", str(self.data),
            "--tcp-forward", "web", "18080", "10.60.0.20", "28080",
            "--tcp-forward", "tls", "18443", "10.60.0.21", "28443",
        ]

    def invoke(self, extra=(), env=None, args=None):
        return self.fixture.run(
            [*(self.args if args is None else args), *extra],
            env=env,
            script="deploy-single-node-docker.sh",
        )

    def commands(self):
        path = self.root / "commands.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def assert_no_changes(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertTrue(result.stderr)
        self.assertFalse(self.data.exists())
        self.assertFalse((self.root / "installed-sysctl.conf").exists())
        self.assertEqual(list(self.stage.iterdir()), [])
        self.assertFalse(any(c[0] in ("install", "sysctl")
                             or c[:2] in (["docker", "run"], ["docker", "pull"])
                             for c in self.commands()))

    def test_auto_detects_single_interface_ip_and_generates_standalone_config(self):
        result = self.invoke(["--admin-user", "operator",
                              "--image", "registry.example.test/ham:fixed"])
        self.assertEqual(result.returncode, 0, result.stderr)

        raw = json.loads((self.data / "manager/config.json").read_text())
        self.assertEqual(raw["cluster"], {"vips": ""})
        local = raw["local"]
        self.assertEqual(local["node_url"], "http://10.40.0.11:8080")
        self.assertEqual(local["sync"]["peers"], [])
        self.assertEqual(local["keepalived"], {"enabled": False, "interface": "ens192"})
        generated_key = local["api_key"]
        self.assertRegex(generated_key, r"^[0-9a-f]{48}$")

        CONF_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONF_PATH.write_text(json.dumps(raw))
        cfg = load_config()
        check_file_config(cfg)
        self.assertFalse(vrrp.keepalived_wanted(cfg))
        vrrp.check_interface(cfg)
        self.assertEqual(auth.node_role(cfg), ("standalone", []))
        self.assertEqual(auth.readonly_state(cfg), (False, ""))
        self.assertEqual(startup._decision(cfg, [], [])[0], "local")

        rendered = haproxy.render_haproxy(cfg)
        self.assertNotIn(" ssl ", rendered)
        for endpoint in ("bind 10.40.0.11:18080", "bind 10.40.0.11:18443",
                         "10.60.0.20:28080", "10.60.0.21:28443"):
            self.assertIn(endpoint, rendered)

        env = (self.data / "manager.env").read_text()
        self.assertIn("HAM_ADMIN_USER=operator\n", env)
        self.assertIn("HAM_ADMIN_PASSWORD=" + self.password + "\n", env)
        self.assertIn("HAM_PORT=8080\n", env)
        self.assertIn("HAM_APPLY_ON_START=1\n", env)
        self.assertEqual((self.data / "api-header").read_text().strip(),
                         "X-API-Key: " + generated_key)
        for path in (self.data / "manager/config.json", self.data / "manager.env",
                     self.data / "api-header"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

        # No sysctl modification and no NET_* capabilities for standalone mode.
        self.assertFalse((self.root / "installed-sysctl.conf").exists())
        self.assertFalse(any(c[0] == "sysctl" for c in self.commands()))
        run = next(c for c in self.commands() if c[:2] == ["docker", "run"])
        self.assertEqual(run[-1], "registry.example.test/ham:fixed")
        self.assertEqual(run[run.index("--network") + 1], "host")
        self.assertEqual(run[run.index("--hostname") + 1], "proxy-solo")
        self.assertNotIn("--cap-add", run)
        self.assertEqual(run.count("-v"), 4)

        output = result.stdout + result.stderr + json.dumps(self.commands())
        self.assertNotIn(self.password, output)
        self.assertNotIn(generated_key, output)
        self.assertIn("does not confirm startup Apply", result.stdout)
        self.assertEqual(list(self.stage.iterdir()), [])

    def test_multi_ip_interface_requires_explicit_listen_ip_and_supports_multiple_ips(self):
        multi_env = {
            "TEST_ADDRESSES": json.dumps([
                {"ifname": "mgmt0", "operstate": "UP", "addr_info": [{"local": "172.20.0.11"}]},
                {"ifname": "ens192", "operstate": "UP", "addr_info": [
                    {"local": "10.40.0.11"}, {"local": "10.40.0.12"}]},
            ])
        }
        # Ambiguous interface without --listen-ip fails closed and lists candidates.
        res = self.invoke(env=multi_env)
        self.assert_no_changes(res)
        self.assertIn("multiple IPv4 addresses", res.stderr)
        self.assertIn("10.40.0.11", res.stderr)
        self.assertIn("10.40.0.12", res.stderr)

        # Explicit multiple --listen-ip + separate --mng-ip + custom --mng-port + supplied --api-key.
        args = [
            "--interface", "ens192",
            "--listen-ip", "10.40.0.11",
            "--listen-ip", "10.40.0.12",
            "--mng-ip", "172.20.0.11",
            "--mng-port", "18090",
            "--api-key", self.api_key,
            "--admin-password", self.password,
            "--data-dir", str(self.data),
            "--tcp-forward", "app", "10.40.0.11:443", "10.60.0.20", "8443",
            "--tcp-forward", "monitor", "10.40.0.12:443", "10.60.0.21", "9443",
            "--tcp-forward", "legacy", "10.40.0.12:8080", "db.internal.example.test", "5432",
        ]
        res = self.invoke(args=args, env=multi_env)
        self.assertEqual(res.returncode, 0, res.stderr)
        raw = json.loads((self.data / "manager/config.json").read_text())
        self.assertEqual(raw["local"]["node_url"], "http://172.20.0.11:18090")
        self.assertEqual(raw["local"]["api_key"], self.api_key)
        self.assertEqual([f["binds"] for f in raw["haproxy"]["frontends"]],
                         ["10.40.0.11:443", "10.40.0.12:443", "10.40.0.12:8080"])
        self.assertEqual(raw["haproxy"]["servers"][-1]["address"], "db.internal.example.test")
        self.assertIn("HAM_PORT=18090\n", (self.data / "manager.env").read_text())
        self.assertNotIn(self.api_key, res.stdout + res.stderr)

        # Verify standalone startup.attempt(), /api/status?brief=1, and UI/API writes without VIPs.
        CONF_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONF_PATH.write_text(json.dumps(raw))
        for path in (apply.HAPROXY_CFG, apply.KEEPALIVED_CFG):
            path.unlink(missing_ok=True)
        with patch.object(startup, "ENABLED", True), \
             patch.object(startup, "_status", {
                 "enabled": True, "state": "checking",
                 "local_ok": False, "error": "", "attempts": 0}), \
             patch.object(startup, "_join_baseline", None), \
             patch.object(apply, "run", return_value=(0, "active")), \
             patch.object(apply, "_haproxy_check", return_value=(0, "", "global\n")), \
             patch.object(apply, "_haproxy_not_serving", return_value=""), \
             patch.object(apply, "check_rendered", return_value=(True, "")):
            startup.attempt()
            self.assertTrue(apply.HAPROXY_CFG.exists())
            self.assertFalse(apply.KEEPALIVED_CFG.exists())

            client = ham.app.test_client()
            headers = {"X-API-Key": self.api_key}
            status = client.get("/api/status?brief=1", headers=headers).get_json()
            self.assertEqual(status["role"], "standalone")
            self.assertEqual(status["keepalived"], "disabled")
            self.assertEqual(status["vips"], [])
            self.assertEqual(status["vip_held"], [])
            self.assertFalse(status["read_only"])
            self.assertEqual(status["startup"]["state"], "ready")
            self.assertTrue(status["startup"]["local_ok"])
            self.assertGreaterEqual(status["published_rev"], 1)
            self.assertEqual(status["published_rev"], status["applied_rev"])

            pub = client.post(
                "/api/wizard/publish",
                headers=headers,
                json={"url": "http://web.example.test", "target": "http://10.60.0.30:8080"},
            )
            self.assertEqual(pub.status_code, 200, pub.get_data(as_text=True))
            self.assertTrue(pub.get_json()["ok"])

            applied = client.post("/api/apply", headers=headers, json={})
            self.assertEqual(applied.status_code, 200, applied.get_data(as_text=True))
            body = applied.get_json()
            self.assertTrue(body["local_ok"])
            self.assertTrue(body["cluster_ok"])
            self.assertFalse(apply.KEEPALIVED_CFG.exists())

    def test_explicit_ip_port_on_auto_detected_single_ip_interface(self):
        args = [
            "--interface", "ens192",
            "--admin-password", self.password,
            "--data-dir", str(self.data),
            "--tcp-forward", "app", "10.40.0.11:443", "10.60.0.20", "8443",
        ]
        res = self.invoke(args=args)
        self.assertEqual(res.returncode, 0, res.stderr)
        raw = json.loads((self.data / "manager/config.json").read_text())
        self.assertEqual(raw["haproxy"]["frontends"][0]["binds"], "10.40.0.11:443")

        # Mismatched IP against auto-detected interface IP must fail closed.
        bad = args[:-5] + ["--tcp-forward", "app", "10.40.0.99:443", "10.60.0.20", "8443"]
        clean_data = self.root / "data2"
        bad[bad.index(str(self.data))] = str(clean_data)
        res = self.invoke(args=bad)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("must match the selected interface IP", res.stderr)
        self.assertIn("10.40.0.99", res.stderr)
        self.assertFalse(clean_data.exists())

    def test_help_requires_no_root_or_host_operations(self):
        result = self.invoke(args=["--help"], env={"TEST_UID": "1000"})
        self.assertEqual(result.returncode, 0)
        self.assertIn("--interface", result.stdout)
        self.assertIn("--listen-ip", result.stdout)
        self.assertEqual(self.commands(), [])

    def test_invalid_arguments_and_conflicts_fail_before_host_changes(self):
        cases = [
            ["--interface", "lo"],
            ["--listen-ip", "999.1.1.1"],
            ["--listen-ip", "10.40.0.11", "--listen-ip", "10.40.0.11"],
            ["--listen-ip", "10.40.0.99"],
            ["--mng-ip", "10.40.0.99"],
            ["--mng-port", "0"],
            ["--mng-port", "65536"],
            ["--mng-port", "18080"],
            ["--tcp-forward", "bad", "8080", "10.60.0.20", "80"],
            ["--tcp-forward", "web", "19090", "10.60.0.20", "80"],
            ["--tcp-forward", "other", "18080", "10.60.0.20", "80"],
            ["--tcp-forward", "other", "10.40.0.11:18080", "10.60.0.20", "80"],
            ["--tcp-forward", "bad", "0.0.0.0:443", "10.60.0.20", "443"],
            ["--tcp-forward", "bad", "19090", "10.60.0.20", "65536"],
            ["--tcp-forward", "bad", "19090", "bad\nbackend", "80"],
            ["--data-dir", "/"],
            ["--data-dir", "relative/path"],
            ["--unknown"],
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                self.assert_no_changes(self.invoke(extra))
        self.assert_no_changes(self.invoke(args=[]))

        # Multiple --listen-ip requires explicit IP:PORT on every rule.
        multi_env = {
            "TEST_ADDRESSES": json.dumps([
                {"ifname": "ens192", "operstate": "UP", "addr_info": [
                    {"local": "10.40.0.11"}, {"local": "10.40.0.12"}]}
            ])
        }
        self.assert_no_changes(self.invoke(
            ["--listen-ip", "10.40.0.11", "--listen-ip", "10.40.0.12"],
            env=multi_env,
        ))

    def test_credentials_fail_closed_without_logging_their_contents(self):
        cases = [
            ("--api-key", "short-key"),
            ("--api-key", "a" * 32 + " "),
            ("--api-key", "a" * 32 + "\n"),
            ("--api-key", "\u5bc6" * 32),
            ("--admin-password", "short"),
            ("--admin-password", "first line\nsecond line"),
            ("--admin-password", "a-valid-password-with-trailing-newline\n"),
        ]
        for option, value in cases:
            with self.subTest(option=option):
                result = self.invoke([option, value])
                self.assert_no_changes(result)
                self.assertNotIn(value, result.stdout + result.stderr)

    def test_host_preflight_guards_fail_closed(self):
        cases = [
            {"TEST_UID": "1000"},
            {"TEST_ADDRESSES": "[]"},
            {"TEST_ADDRESSES": json.dumps([
                {"ifname": "ens192", "operstate": "DOWN", "addr_info": [{"local": "10.40.0.11"}]}
            ])},
            {"TEST_ADDRESSES": json.dumps([
                {"ifname": "ens192", "operstate": "UP", "addr_info": []}
            ])},
            {"TEST_ROOTLESS": "1"},
            {"TEST_CONTAINER": "haproxy-manager"},
        ]
        for env in cases:
            with self.subTest(env=env):
                self.assert_no_changes(self.invoke(env=env))

    def test_existing_data_is_not_overwritten(self):
        self.data.mkdir()
        sentinel = self.data / "existing-config"
        sentinel.write_text("keep this")
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to replace", result.stderr)
        self.assertEqual(sentinel.read_text(), "keep this")
        self.assertFalse(any(c[0] in ("docker", "install", "sysctl") for c in self.commands()))

    def test_external_failures_are_reported_without_claiming_success(self):
        result = self.invoke(env={"TEST_PULL_STATUS": "42"})
        self.assertEqual(result.returncode, 42)
        self.assertIn("operation failed", result.stderr)
        self.assertFalse(self.data.exists())

        result = self.invoke(env={"TEST_RUN_FAIL": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("operation failed", result.stderr)
        self.assertNotIn("container started", result.stdout)
        self.assertTrue((self.data / "manager/config.json").exists())
        self.assertEqual(list(self.stage.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
