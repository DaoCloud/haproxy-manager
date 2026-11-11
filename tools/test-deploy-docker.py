#!/usr/bin/env python3
"""Run the production deployment script without touching host Docker or sysctls."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
_tmp = tempfile.TemporaryDirectory(prefix="ham-deploy-config-")
for key, name in {
    "HAM_DATA_DIR": "manager", "HAM_CERT_DIR": "certs",
    "HAM_HAPROXY_CFG": "haproxy.cfg", "HAM_KEEPALIVED_CFG": "keepalived.conf",
    "HAM_ERROR_DIR": "errors", "HAM_CROWDSEC_SPOE": "crowdsec.cfg",
}.items():
    os.environ[key] = str(Path(_tmp.name) / name)
os.environ["HAM_DRY_RUN"] = "1"
sys.path.insert(0, str(ROOT))

import ham  # noqa: E402
from ham import apply, haproxy, keepalived, startup, vrrp  # noqa: E402
from ham.config import CONF_PATH, load_config  # noqa: E402
from ham.validate import check_file_config  # noqa: E402
from tools.integration.deploy_fixture import DeploymentFixture  # noqa: E402


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ham-deploy-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fixture = DeploymentFixture(self.root)
        self.secrets = ("test-shared-cluster-key-1234567890-abcd", "test-password-$with spaces")
        self.data, self.stage, self.env = self.fixture.data, self.fixture.stage, self.fixture.env
        self.env["TEST_ADDRESSES"] = json.dumps([
            {"ifname": "mgmt0", "operstate": "UP", "addr_info": [{"local": "172.20.0.11"}]},
            {"ifname": "ens192", "operstate": "UP", "addr_info": [{"local": "10.40.0.11"}]},
        ])
        self.args = [
            "--hostname-a", "proxy-a", "--hostname-b", "proxy-b",
            "--mng-ip-a", "172.20.0.11", "--mng-ip-b", "172.20.0.12",
            "--vrrp-interface-a", "ens192", "--vrrp-interface-b", "ens224",
            "--vrrp-ip-a", "10.40.0.11", "--vrrp-ip-b", "10.40.0.12",
            "--vip", "10.40.0.100/25", "--vrid", "91",
            "--api-key", self.secrets[0], "--admin-password", self.secrets[1],
            "--data-dir", str(self.data),
            "--tcp-forward", "web", "18080", "10.60.0.20", "28080",
            "--tcp-forward", "tls", "18443", "10.60.0.21", "28443",
        ]

    def invoke(self, extra=(), env=None, args=None):
        return self.fixture.run([*(self.args if args is None else args), *extra], env)

    def multi_vip_args(self):
        return self.args[:-10] + [
            "--vip", "10.40.0.101/25",
            "--tcp-forward", "app", "10.40.0.100:443", "10.60.0.20", "8443",
            "--tcp-forward", "monitor", "10.40.0.101:443", "10.60.0.21", "9443",
            "--tcp-forward", "extra", "10.40.0.101:8443", "10.60.0.22", "443",
        ]

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

    def test_a_generates_parameterized_rules_that_the_manager_can_load_and_render(self):
        result = self.invoke(["--priority-a", "140", "--admin-user", "operator",
                              "--image", "registry.example.test/ham:fixed"])
        self.assertEqual(result.returncode, 0, result.stderr)
        raw = json.loads((self.data / "manager/config.json").read_text())
        self.assertEqual(raw["cluster"], {"vips": "10.40.0.100/25", "vrid": 91})
        local = raw["local"]
        self.assertEqual(local["node_url"], "http://172.20.0.11:8080")
        self.assertEqual(local["sync"]["peers"][0]["url"], "http://172.20.0.12:8080")
        self.assertEqual(local["api_key"], self.secrets[0])
        self.assertEqual(local["sync"]["peers"][0]["api_key"], self.secrets[0])
        self.assertEqual(local["keepalived"], {
            "interface": "ens192", "priority": 140,
            "unicast_src": "10.40.0.11", "unicast_peer": "10.40.0.12"})
        CONF_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONF_PATH.write_text(json.dumps(raw))
        cfg = load_config()
        check_file_config(cfg)
        with patch.object(apply, "node_interfaces", return_value=[
                {"name": "ens192", "addresses": ["10.40.0.11/25"]}]):
            vrrp.check_interface(cfg)
        self.assertEqual(startup._decision(cfg, [], [])[0], "local")
        rendered = haproxy.render_haproxy(cfg)
        for endpoint in ("10.40.0.100:18080", "10.40.0.100:18443",
                         "10.60.0.20:28080", "10.60.0.21:28443"):
            self.assertIn(endpoint, rendered)
        env = (self.data / "manager.env").read_text()
        self.assertIn("HAM_ADMIN_USER=operator\n", env)
        self.assertIn("HAM_ADMIN_PASSWORD=" + self.secrets[1] + "\n", env)
        self.assertIn("HAM_PORT=8080\n", env)
        for path in (self.data / "manager/config.json", self.data / "manager.env",
                     self.data / "api-header"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertIn("X-API-Key: " + self.secrets[0], (self.data / "api-header").read_text())
        run = next(c for c in self.commands() if c[:2] == ["docker", "run"])
        self.assertEqual(run[-1], "registry.example.test/ham:fixed")
        self.assertEqual(run[run.index("--network") + 1], "host")
        self.assertEqual(run.count("-v"), 4)
        self.assertIn(str(self.data / "manager.env"), run)
        output = result.stdout + result.stderr + json.dumps(self.commands())
        for secret in self.secrets:
            self.assertNotIn(secret, output)
        self.assertIn("does not confirm startup Apply", result.stdout)
        self.assertEqual(list(self.stage.iterdir()), [])

    def test_b_uses_its_own_identity_and_waits_instead_of_seeding_shared_rules(self):
        addresses = json.dumps([
            {"ifname": "mgmt0", "addr_info": [{"local": "172.20.0.12"}]},
            {"ifname": "ens224", "operstate": "UP", "addr_info": [{"local": "10.40.0.12"}]},
        ])
        result = self.invoke(["--mng-port", "18090"],
                             env={"TEST_HOSTNAME": "proxy-b", "TEST_ADDRESSES": addresses})
        self.assertEqual(result.returncode, 0, result.stderr)
        raw = json.loads((self.data / "manager/config.json").read_text())
        self.assertEqual(set(raw), {"local"})
        self.assertEqual(raw["local"]["api_key"], self.secrets[0])
        self.assertEqual(raw["local"]["sync"]["peers"][0]["api_key"], self.secrets[0])
        self.assertEqual(raw["local"]["node_url"], "http://172.20.0.12:18090")
        self.assertEqual(raw["local"]["keepalived"]["interface"], "ens224")
        self.assertEqual(raw["local"]["keepalived"]["unicast_src"], "10.40.0.12")
        self.assertEqual(raw["local"]["sync"]["peers"][0]["url"], "http://172.20.0.11:18090")
        self.assertIn("HAM_PORT=18090\n", (self.data / "manager.env").read_text())
        CONF_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONF_PATH.write_text(json.dumps(raw))
        cfg = load_config()
        check_file_config(cfg)
        self.assertEqual(startup._decision(cfg, [], [])[0], "wait")
        self.assertIn(self.secrets[0], (self.data / "api-header").read_text())

    def test_custom_management_port_updates_urls_and_frees_default_port(self):
        result = self.invoke([
            "--mng-port", "18090",
            "--tcp-forward", "app", "8080", "10.60.0.22", "80",
        ])
        self.assertEqual(result.returncode, 0, result.stderr)
        raw = json.loads((self.data / "manager/config.json").read_text())
        self.assertEqual(raw["local"]["node_url"], "http://172.20.0.11:18090")
        self.assertEqual(raw["local"]["sync"]["peers"][0]["url"], "http://172.20.0.12:18090")
        self.assertIn("HAM_PORT=18090\n", (self.data / "manager.env").read_text())
        self.assertEqual(raw["haproxy"]["frontends"][-1]["binds"], "10.40.0.100:8080")

    def test_invalid_or_conflicting_management_ports_fail_before_host_changes(self):
        for port in ("0", "65536", "not-a-port"):
            with self.subTest(port=port):
                result = self.invoke(["--mng-port", port])
                self.assert_no_changes(result)
                self.assertIn("--mng-port must be an integer", result.stderr)
        for extra in (
                ["--mng-port", "18080"],
                ["--mng-port", "18090",
                 "--tcp-forward", "bad", "18090", "10.60.0.22", "80"]):
            with self.subTest(extra=extra):
                result = self.invoke(extra)
                self.assert_no_changes(result)
                self.assertIn("is reserved for the management API", result.stderr)

    def test_multiple_vips_share_ports_but_keep_their_own_tcp_targets(self):
        result = self.invoke(args=self.multi_vip_args())
        self.assertEqual(result.returncode, 0, result.stderr)
        raw = json.loads((self.data / "manager/config.json").read_text())
        self.assertEqual(raw["cluster"]["vips"], "10.40.0.100/25\n10.40.0.101/25")
        self.assertEqual([f["binds"] for f in raw["haproxy"]["frontends"]],
                         ["10.40.0.100:443", "10.40.0.101:443", "10.40.0.101:8443"])
        self.assertTrue(all(f["mode"] == "tcp" and not f.get("ssl_enabled")
                            for f in raw["haproxy"]["frontends"]))
        self.assertEqual([s["address"] for s in raw["haproxy"]["servers"]],
                         ["10.60.0.20", "10.60.0.21", "10.60.0.22"])
        CONF_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONF_PATH.write_text(json.dumps(raw))
        cfg = load_config()
        check_file_config(cfg)
        rendered = haproxy.render_haproxy(cfg)
        self.assertNotIn(" ssl ", rendered)
        self.assertIn("bind 10.40.0.100:443", rendered)
        self.assertIn("bind 10.40.0.101:443", rendered)
        for vip in ("10.40.0.100/25", "10.40.0.101/25"):
            self.assertIn(vip, keepalived.render_keepalived(cfg))
        self.assertEqual(list((self.data / "haproxy").iterdir()), [])
        self.assertIn("10.40.0.101:443 -> 10.60.0.21:9443", result.stdout)

    def test_multi_vip_b_still_waits_for_shared_publication(self):
        result = self.invoke(args=self.multi_vip_args(), env={
            "TEST_HOSTNAME": "proxy-b", "TEST_ADDRESSES": json.dumps([{
                "ifname": "ens224", "operstate": "UP", "addr_info": [
                    {"local": "172.20.0.12"}, {"local": "10.40.0.12"}]}])})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(json.loads((self.data / "manager/config.json").read_text())), {"local"})

    def multi_vip_client(self):
        result = self.invoke(args=self.multi_vip_args())
        self.assertEqual(result.returncode, 0, result.stderr)
        CONF_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONF_PATH.write_text((self.data / "manager/config.json").read_text())
        role = patch("ham.auth.node_role", return_value=("active", ["10.40.0.100", "10.40.0.101"]))
        role.start()
        self.addCleanup(role.stop)
        return ham.app.test_client(), {"X-API-Key": self.secrets[0]}

    def test_multi_vip_services_can_be_edited_republished_and_added(self):
        client, headers = self.multi_vip_client()
        services = client.get("/api/services", headers=headers).get_json()
        original_binds = {f["id"]: f["binds"] for f in load_config()["haproxy"]["frontends"]}
        for service in services:
            body = {"name": service["pool"], "url": service["url"],
                    "target": "\n".join(service["targets"]), "certificate": False}
            for editing in (True, False):
                with self.subTest(url=service["url"], editing=editing):
                    response = client.post("/api/wizard/publish", headers=headers,
                                           json=dict(body, **({"service_id": service["id"]}
                                                              if editing else {})))
                    self.assertEqual(response.status_code, 200, response.get_json())
                    self.assertEqual({f["id"]: f["binds"] for f in
                                      load_config()["haproxy"]["frontends"]}, original_binds)
        monitor = next(s for s in services if s["url"] == "tcp://10.40.0.101:443")
        response = client.delete("/api/services/" + monitor["id"], headers=headers)
        self.assertEqual(response.status_code, 200, response.get_json())
        response = client.post("/api/wizard/publish", headers=headers, json={
            "name": monitor["pool"], "url": monitor["url"],
            "target": "\n".join(monitor["targets"]), "certificate": False})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(sorted(f["binds"] for f in load_config()["haproxy"]["frontends"]),
                         sorted(original_binds.values()))
        self.assertEqual({s["url"]: s["targets"] for s in
                          client.get("/api/services", headers=headers).get_json()},
                         {s["url"]: s["targets"] for s in services})

    def test_tcp_listener_overlaps_are_rejected_without_saving(self):
        client, headers = self.multi_vip_client()
        original = load_config()["haproxy"]
        monitor = next(s for s in client.get("/api/services", headers=headers).get_json()
                       if s["url"] == "tcp://10.40.0.101:443")
        for url, service_id in (
                ("tcp://10.40.0.100:443", monitor["id"]),
                ("tcp://0.0.0.0:443", monitor["id"]),
                ("tcp://0.0.0.0:443", None),
                ("tcp://*:443", None),
                ("tcp://[::]:443", None)):
            with self.subTest(url=url, service_id=service_id):
                response = client.post("/api/wizard/publish", headers=headers, json={
                    "name": "overlap", "url": url, "target": "tcp://10.60.0.99:443",
                    "service_id": service_id, "certificate": False})
                self.assertEqual(response.status_code, 400, response.get_json())
                self.assertIn("already", response.get_json()["error"])
                self.assertEqual(load_config()["haproxy"], original)
        cfg = load_config()
        cfg["haproxy"]["frontends"] = [cfg["haproxy"]["frontends"][0]]
        for bind in ("0.0.0.0:443", "*:443", ":::443 v4v6",
                     "127.0.0.1:9443,0.0.0.0:443",
                     "10.40.0.100:443,10.40.0.101:443"):
            with self.subTest(existing_bind=bind):
                cfg["haproxy"]["frontends"][0]["binds"] = bind
                CONF_PATH.write_text(json.dumps(cfg))
                response = client.post("/api/wizard/publish", headers=headers, json={
                    "name": "overlap", "url": "tcp://10.40.0.101:443",
                    "target": "tcp://10.60.0.99:443", "certificate": False})
                self.assertEqual(response.status_code, 400, response.get_json())
                self.assertEqual(load_config()["haproxy"], cfg["haproxy"])

    def test_multi_vip_binding_errors_fail_before_host_changes(self):
        for extra in (
                ["--vip", "10.40.0.100/25"],
                ["--vip", "10.40.0.100/24"],
                ["--vip", "10.40.0.12/25"],
                ["--tcp-forward", "bad", "10.40.0.102:443", "10.60.0.20", "443"],
                ["--tcp-forward", "bad", "0.0.0.0:443", "10.60.0.20", "443"],
                ["--tcp-forward", "bad", "10.40.0.101:443", "10.60.0.20", "443"],
                ["--tcp-forward", "bad", "10.40.0.101:8080", "10.60.0.20", "443"],
                ["--tcp-forward", "bad", "10.40.0.101:65536", "10.60.0.20", "443"],
                ["--tcp-forward", "bad", "10.40.0.101/25:443", "10.60.0.20", "443"],
                ["--tcp-forward", "bad", "443", "10.60.0.20", "443"]):
            with self.subTest(extra=extra):
                self.assert_no_changes(self.invoke(args=self.multi_vip_args() + extra))
        self.assert_no_changes(self.invoke(["--vip", "10.40.0.101/25"]))

    def test_dns_backend_and_fqdn_hostnames_are_kept_as_supplied(self):
        result = self.invoke([
            "--hostname-a", "proxy-a.example.test", "--hostname-b", "proxy-b.example.test",
            "--tcp-forward", "database", "15432", "db.internal.example.test", "5432",
        ], env={"TEST_HOSTNAME": "proxy-a.example.test"})
        self.assertEqual(result.returncode, 0, result.stderr)
        raw = json.loads((self.data / "manager/config.json").read_text())
        self.assertEqual(raw["haproxy"]["servers"][-1]["address"], "db.internal.example.test")
        self.assertEqual(raw["haproxy"]["frontends"][-1]["binds"], "10.40.0.100:15432")
        run = next(c for c in self.commands() if c[:2] == ["docker", "run"])
        self.assertEqual(run[run.index("--hostname") + 1], "proxy-a.example.test")

    def test_help_requires_no_root_or_host_operations(self):
        result = self.invoke(args=["--help"], env={"TEST_UID": "1000"})
        self.assertEqual(result.returncode, 0)
        self.assertIn("--tcp-forward", result.stdout)
        self.assertEqual(self.commands(), [])

    def test_invalid_arguments_fail_before_host_changes(self):
        cases = [
            ["--hostname-b", "proxy-a"], ["--mng-ip-a", "999.1.1.1"],
            ["--vip", "10.40.0.100"], ["--vip", "10.40.0.100/33"],
            ["--vip", "10.40.0.11/25"], ["--vrid", "256"],
            ["--priority-a", "0"], ["--tcp-forward", "bad", "8080", "10.60.0.20", "80"],
            ["--tcp-forward", "web", "19090", "10.60.0.20", "80"],
            ["--tcp-forward", "other", "18080", "10.60.0.20", "80"],
            ["--tcp-forward", "bad", "19090", "10.60.0.20", "65536"],
            ["--tcp-forward", "bad", "19090", "bad\nbackend", "80"],
            ["--data-dir", "/"], ["--data-dir", "relative/path"],
            ["--unknown"], ["--vrid"], ["--hostname-a", "--hostname-b"],
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                self.assert_no_changes(self.invoke(extra))
        self.assert_no_changes(self.invoke(args=[]))

    def test_credentials_fail_closed_without_logging_their_contents(self):
        cases = [("--api-key", "bad-key"), ("--api-key", "a" * 32 + " "),
                 ("--api-key", "a" * 32 + "\n"), ("--api-key", "\u5bc6" * 32),
                 ("--admin-password", "short"),
                 ("--admin-password", "first line\nsecond line"),
                 ("--admin-password", "a-valid-password-with-trailing-newline\n")]
        for option, value in cases:
            with self.subTest(option=option):
                result = self.invoke([option, value])
                self.assert_no_changes(result)
                self.assertNotIn(value, result.stdout + result.stderr)

    def test_root_hostname_interface_and_container_guards(self):
        for env in ({"TEST_UID": "1000"}, {"TEST_HOSTNAME": "other-host"},
                    {"TEST_ADDRESSES": "[]"}, {"TEST_ROOTLESS": "1"},
                    {"TEST_CONTAINER": "haproxy-manager"}):
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
