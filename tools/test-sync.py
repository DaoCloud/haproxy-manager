#!/usr/bin/env python3
"""Published configurations, truthful acknowledgements and recovery.

    python3 tools/test-sync.py
"""
import base64
import copy
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
_tmp = tempfile.TemporaryDirectory(prefix="ham-sync-")
for key, name in {
    "HAM_DATA_DIR": "data", "HAM_CERT_DIR": "certs",
    "HAM_HAPROXY_CFG": "haproxy.cfg", "HAM_KEEPALIVED_CFG": "keepalived.conf",
    "HAM_ERROR_DIR": "errors", "HAM_CROWDSEC_SPOE": "crowdsec.cfg",
}.items():
    os.environ[key] = str(pathlib.Path(_tmp.name) / name)
os.environ["HAM_DRY_RUN"] = "1"

import ham  # noqa: E402
from ham import apply, auth, cluster, dnsapi, peering, setup, stats, sync, webui, wizard  # noqa: E402
from ham.config import (CONF_PATH, DEFAULT_CONFIG, config_hash, load_config, merged,
                        save_config, shared_fingerprint, shared_view)  # noqa: E402

_real_health_check = apply._haproxy_not_serving


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.object(apply, "_haproxy_check", return_value=(0, "", "global\n")).start()
        patch.object(apply, "_haproxy_not_serving", return_value="").start()
        patch.object(apply, "ensure_cert_files", return_value=[]).start()
        self.run = patch.object(apply, "run", return_value=(0, "")).start()
        patch.object(apply.errorpages, "write_files", return_value=[]).start()
        patch.object(apply.crowdsec, "write_files", return_value=[]).start()
        patch.object(apply.crowdsec, "ensure_service", return_value=("", "")).start()
        patch.object(apply.keepalived, "write_track_script", return_value=True).start()
        patch.object(apply.vrrp, "derive_unicast", return_value=False).start()
        patch.object(auth, "node_role", return_value=("active", [])).start()
        patch.object(sync, "_requests", Mock()).start()
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["local"]["api_key"] = "receiver-key"
        cfg["local"]["node_url"] = "http://node-a"
        cfg["acme"]["settings"]["enabled"] = False
        save_config(cfg)
        ham.app.config["TESTING"] = True
        self.client = ham.app.test_client()
        self.client.environ_base["HTTP_X_API_KEY"] = "receiver-key"
        self.peer = {"id": "p", "name": "node-b", "url": "http://node-b",
                     "api_key": "peer-key", "enabled": True}

    def publish(self):
        result = apply.do_apply(allow_push=False)
        self.assertTrue(result["ok"], result)
        return load_config()

    def edit(self):
        cfg = load_config()
        cfg["haproxy"]["backends"].append(
            {"id": "new", "name": "new-pool", "mode": "tcp", "servers": []})
        save_config(cfg)
        return load_config()

    def payload(self, cfg):
        return {"config": copy.deepcopy(shared_view(cfg)),
                "rev": cfg["_meta"]["shared_rev"],
                "fp": shared_fingerprint(cfg), "certs": {}}

    def response(self, body):
        return Mock(status_code=200, json=Mock(return_value=body))

    def test_unapplied_draft_is_not_sent_or_pulled(self):
        published = self.publish()
        cfg = self.edit()
        sent = sync.shared_payload(cfg)
        self.assertEqual(sent["config"], shared_view(published))
        self.assertEqual(sent["rev"], published["_meta"]["shared_rev"])
        pulled = self.client.get("/api/sync/pull").get_json()
        self.assertEqual(pulled["config"], sent["config"])
        self.assertEqual(shared_view(load_config()), shared_view(cfg))

    def test_mesh_uses_node_urls_not_a_shared_key_to_identify_itself(self):
        payload = self.payload(self.publish())
        payload["peers"] = [
            {"id": "self", "url": "HTTP://NODE-A/", "api_key": "another-key"},
            {"id": "peer", "url": "http://node-b", "api_key": "receiver-key", "self": True},
        ]
        response = self.client.post("/api/sync/receive", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        peers = load_config()["local"]["sync"]["peers"]
        self.assertEqual([peer["url"] for peer in peers], ["http://node-b"])
        self.assertEqual(peers[0]["api_key"], "receiver-key")

    def test_peer_connection_test_accepts_a_shared_cluster_key(self):
        cfg = load_config()
        cfg["local"]["sync"]["peers"] = [dict(self.peer, api_key="receiver-key")]
        save_config(cfg)
        requests = Mock()
        requests.get.return_value = self.response(
            {"hostname": "node-b", "version": "test", "role": "passive", "peers": []})
        with patch.object(peering, "_requests", requests):
            response = self.client.post("/api/peers/p/test")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"], response.get_json())
        self.assertEqual(requests.get.call_args.kwargs["headers"]["X-API-Key"], "receiver-key")

    def test_peer_connection_test_still_rejects_its_own_node_url(self):
        cfg = load_config()
        cfg["local"]["sync"]["peers"] = [
            dict(self.peer, url="HTTP://NODE-A/", api_key="receiver-key")]
        save_config(cfg)
        requests = Mock()
        requests.get.return_value = self.response({"hostname": "node-a", "role": "active"})
        with patch.object(peering, "_requests", requests):
            response = self.client.post("/api/peers/p/test")
        self.assertFalse(response.get_json()["ok"])
        requests.get.assert_not_called()
        peer = self.client.get("/api/peers").get_json()[0]
        self.assertTrue(peer["is_self"])

    def test_background_sync_does_not_publish_a_draft(self):
        published = self.publish()
        cfg = self.edit()
        cfg["local"]["sync"]["peers"] = [self.peer]
        nodes = [{"url": self.peer["url"], "reachable": True,
                  "config_rev": published["_meta"]["shared_rev"],
                  "config_fp": shared_fingerprint(published),
                  "published_rev": published["_meta"]["shared_rev"],
                  "published_fp": shared_fingerprint(published),
                  "applied_rev": published["_meta"]["shared_rev"],
                  "applied_fp": shared_fingerprint(published)}]
        self.assertEqual(sync.reconcile(cfg, nodes), [])
        sync._requests.post.assert_not_called()
        sync._requests.get.assert_not_called()

    def test_only_published_shared_certificate_files_are_sent(self):
        cfg = load_config()
        shared = {"id": "business", "name": "business-tls", "domains": "app.example.test"}
        local = {"id": "ui", "name": "haproxy-manager-ui", "domains": "node-a.example.test"}
        cfg["acme"]["certificates"] = [shared]
        cfg["local"]["acme"]["certificates"] = [local]
        save_config(cfg)
        self.publish()
        cfg = load_config()
        draft = {"id": "draft", "name": "draft-tls", "domains": "draft.example.test"}
        cfg["acme"]["certificates"].append(draft)
        save_config(cfg)
        for cert in (shared, local, draft, {"name": "untracked"}):
            path = apply.cert_path(cert)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(cert["name"].encode())
        payload = sync.shared_payload(load_config())
        self.assertEqual(set(payload["certs"]), {"business-tls.pem"})
        self.assertEqual(base64.b64decode(payload["certs"]["business-tls.pem"]),
                         b"business-tls")

    def test_receive_does_not_overwrite_local_pems_from_an_older_sender(self):
        cfg = self.publish()
        cert = {"id": "local-ui", "name": "haproxy-manager-ui",
                "domains": "node-a.example.test"}
        cfg["local"]["acme"]["certificates"] = [cert]
        save_config(cfg)
        path = apply.cert_path(cert)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"this node's CA-issued UI certificate")
        payload = self.payload(cfg)
        payload["rev"] += 1
        payload["certs"] = {path.name: base64.b64encode(b"peer UI certificate").decode()}
        response = self.client.post("/api/sync/receive", json=payload).get_json()
        self.assertTrue(response["ok"], response)
        self.assertEqual(path.read_bytes(), b"this node's CA-issued UI certificate")

    def test_receive_refuses_shared_and_local_certificate_filename_collisions(self):
        cfg = self.publish()
        cert = {"id": "local-cert", "name": "node-local-tls",
                "domains": "node-a.example.test"}
        cfg["local"]["acme"]["certificates"] = [cert]
        save_config(cfg)
        path = apply.cert_path(cert)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"local certificate")
        before = CONF_PATH.read_bytes()
        incoming = copy.deepcopy(cfg)
        incoming["acme"]["certificates"] = [
            dict(cert, id="shared-cert", domains="app.example.test")]
        incoming["_meta"]["shared_rev"] += 1
        payload = self.payload(incoming)
        payload["certs"] = {path.name: base64.b64encode(b"shared certificate").decode()}
        response = self.client.post("/api/sync/receive", json=payload)
        self.assertEqual(response.status_code, 422, response.get_json())
        self.assertEqual(CONF_PATH.read_bytes(), before)
        self.assertEqual(path.read_bytes(), b"local certificate")

    def test_shared_certificate_renewal_is_received_at_the_same_revision(self):
        cfg = load_config()
        cert = {"id": "shared-renewal", "name": "shared-renewal",
                "domains": "app.example.test"}
        cfg["acme"]["certificates"] = [cert]
        save_config(cfg)
        cfg = self.publish()
        path = apply.cert_path(cert)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"old shared certificate")
        payload = self.payload(cfg)
        payload["certs"] = {path.name: base64.b64encode(b"renewed shared certificate").decode()}
        response = self.client.post("/api/sync/receive", json=payload).get_json()
        self.assertTrue(response["ok"], response)
        self.assertEqual(path.read_bytes(), b"renewed shared certificate")

    def test_active_node_pulls_a_newer_publication_when_a_peer_recovers(self):
        cfg = self.publish()
        cfg["local"]["sync"]["peers"] = [self.peer]
        save_config(cfg)
        incoming = copy.deepcopy(cfg)
        incoming["haproxy"]["settings"]["maxconn"] = 9000
        incoming["_meta"]["shared_rev"] += 1
        payload = self.payload(incoming)
        status = {"published_rev": payload["rev"], "published_fp": payload["fp"]}
        sync._requests.get.side_effect = [self.response(status), self.response(payload)]
        result = sync.reconcile(cfg, [
            dict(status, url=self.peer["url"], reachable=True)])
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["ok"], result)
        self.assertEqual(load_config()["_meta"]["applied_fp"], payload["fp"])
        sync._requests.post.assert_not_called()

    def test_no_publication_requires_apply(self):
        result = sync.sync_push(load_config())
        self.assertFalse(result["ok"])
        sync._requests.post.assert_not_called()
        response = self.client.get("/api/sync/pull")
        self.assertNotEqual(response.status_code, 200)

    def test_http_200_with_failed_apply_is_not_success(self):
        payload = self.payload(self.publish())
        sync._requests.post.return_value = self.response(
            {"ok": False, "applied": {"ok": False, "error": "reload failed"}})
        result = sync.push_to_peer(self.peer, payload)
        self.assertFalse(result["ok"])
        self.assertIn("reload failed", result["error"])

    def test_peer_must_confirm_the_exact_applied_version(self):
        payload = self.payload(self.publish())
        for ack in ({"ok": True},
                    {"ok": True, "applied_rev": payload["rev"], "applied_fp": "other"}):
            with self.subTest(ack=ack):
                sync._requests.post.return_value = self.response(ack)
                self.assertFalse(sync.push_to_peer(self.peer, payload)["ok"])
        sync._requests.post.return_value = self.response(
            {"ok": True, "applied_rev": payload["rev"], "applied_fp": payload["fp"]})
        self.assertTrue(sync.push_to_peer(self.peer, payload)["ok"])

    def test_reload_failure_keeps_previous_applied_and_published_versions(self):
        previous = self.publish()
        self.edit()
        self.run.return_value = (1, "reload failed")
        result = apply.do_apply(allow_push=False)
        self.assertFalse(result["ok"])
        cfg = load_config()
        self.assertEqual(cfg["_meta"]["applied_hash"], previous["_meta"]["applied_hash"])
        self.assertEqual(sync.shared_payload(cfg)["config"], shared_view(previous))
        self.assertTrue(cfg["_meta"].get("apply_error"))

    def test_keepalived_failure_cannot_mark_configuration_applied(self):
        previous = self.publish()
        cfg = self.edit()
        cfg["cluster"]["vips"] = "192.0.2.10/24"
        save_config(cfg)
        self.run.reset_mock()
        self.run.side_effect = lambda cmd, **kw: (
            (1, "invalid interface") if cmd[0] == "keepalived" else (0, ""))
        result = apply.do_apply(allow_push=False)
        self.assertFalse(result["ok"])
        self.assertEqual(load_config()["_meta"]["applied_hash"],
                         previous["_meta"]["applied_hash"])
        self.assertFalse(any(c.args[0][:2] == ["systemctl", "reload-or-restart"]
                             for c in self.run.call_args_list))

    def test_keepalived_reload_failure_does_not_publish(self):
        previous = self.publish()
        cfg = self.edit()
        cfg["cluster"]["vips"] = "192.0.2.10/24"
        save_config(cfg)
        self.run.side_effect = lambda cmd, **kw: (
            (1, "reload failed") if cmd == ["systemctl", "reload-or-restart", "keepalived"]
            else (0, ""))
        result = apply.do_apply(allow_push=False)
        self.assertFalse(result["ok"])
        self.assertEqual(sync.shared_payload(load_config())["config"], shared_view(previous))

    def test_keepalived_apply_uses_the_real_docker_systemctl_shim(self):
        cfg = load_config()
        cfg["cluster"]["vips"] = "192.0.2.10/24"
        save_config(cfg)
        shim = pathlib.Path(__file__).resolve().parent.parent / "docker" / "systemctl"
        with tempfile.TemporaryDirectory(prefix="ham-supervisor-") as directory:
            supervisor = pathlib.Path(directory) / "supervisorctl"
            supervisor.write_text(
                '#!/bin/sh\ncase "$*" in\n'
                '  *"status keepalived") echo "keepalived RUNNING";;\n'
                '  *"signal HUP keepalived") echo "keepalived: signalled";;\n'
                '  *) echo "unknown"; exit 1;;\nesac\n')
            supervisor.chmod(0o700)

            def run(cmd, **kwargs):
                if cmd[0] == "systemctl" and cmd[-1] == "keepalived":
                    result = subprocess.run(
                        ["/bin/sh", str(shim), *cmd[1:]], capture_output=True, text=True,
                        env=dict(os.environ, PATH=directory + os.pathsep + os.environ["PATH"]),
                        timeout=5)
                    return result.returncode, result.stdout + result.stderr
                return 0, ""

            self.run.side_effect = run
            result = apply.do_apply(allow_push=False)
        self.assertTrue(result["local_ok"], result)
        self.assertTrue(load_config()["_meta"].get("published"))

    def test_failed_health_check_and_rollback_do_not_publish(self):
        previous = self.publish()
        self.edit()
        apply._haproxy_not_serving.return_value = "new worker is not answering"
        with patch.object(apply, "_restore_previous_haproxy_cfg", return_value="restored"):
            result = apply.do_apply(allow_push=False)
        self.assertFalse(result["ok"])
        self.assertIn("new worker is not answering", result["error"])
        self.assertEqual(sync.shared_payload(load_config())["config"], shared_view(previous))

    def test_a_healthy_old_worker_cannot_acknowledge_a_failed_reload(self):
        previous = self.publish()
        self.edit()
        with patch.object(apply, "DRY_RUN", False), \
                patch.object(apply, "_haproxy_not_serving", _real_health_check), \
                patch.object(apply.watchdog, "probe_haproxy", return_value=("ok", "old worker")), \
                patch.object(stats, "_socket_command",
                             return_value=("HAM_APPLY_GENERATION=old-worker\n", None)), \
                patch.object(apply.time, "sleep"), \
                patch.object(apply, "_restore_previous_haproxy_cfg", return_value="restored"):
            result = apply.do_apply(allow_push=False)
        self.assertFalse(result["local_ok"], result)
        self.assertEqual(load_config()["_meta"]["applied_hash"],
                         previous["_meta"]["applied_hash"])
        self.assertEqual(sync.shared_payload(load_config())["config"], shared_view(previous))

    def test_reapplying_the_same_config_requires_a_new_worker_generation(self):
        self.publish()
        first = apply._haproxy_not_serving.call_args
        self.publish()
        second = apply._haproxy_not_serving.call_args
        self.assertTrue(first.args, "Apply must verify the new worker's generation")
        self.assertNotEqual(first.args[0], second.args[0])

    def test_missing_validator_is_an_error_outside_dry_run(self):
        apply._haproxy_check.return_value = (127, "command not found", "")
        with patch.object(apply, "DRY_RUN", False):
            result = apply.do_apply(allow_push=False)
        self.assertFalse(result["ok"])
        self.assertNotIn("published", load_config()["_meta"])

    def test_partial_cluster_apply_reports_local_success_and_peer_failure(self):
        cfg = load_config()
        cfg["local"]["sync"]["peers"] = [self.peer]
        save_config(cfg)
        sync._requests.post.side_effect = TimeoutError("peer offline")
        result = apply.do_apply()
        self.assertFalse(result["ok"])
        self.assertTrue(result["local_ok"])
        self.assertFalse(result["sync"]["ok"])
        self.assertTrue(sync.shared_payload(load_config())["fp"])

    def test_invalid_received_configuration_leaves_store_untouched(self):
        published = self.publish()
        incoming = copy.deepcopy(published)
        incoming["haproxy"]["settings"]["maxconn"] = 9000
        incoming["_meta"]["shared_rev"] += 1
        before = CONF_PATH.read_bytes()
        apply._haproxy_check.return_value = (1, "invalid configuration", "")
        response = self.client.post("/api/sync/receive", json=self.payload(incoming))
        self.assertFalse(response.get_json()["ok"])
        self.assertEqual(CONF_PATH.read_bytes(), before)

    def test_conflicting_content_at_the_same_revision_is_refused(self):
        published = self.publish()
        incoming = copy.deepcopy(published)
        incoming["haproxy"]["settings"]["maxconn"] = 9000
        before = CONF_PATH.read_bytes()
        response = self.client.post("/api/sync/receive", json=self.payload(incoming))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(CONF_PATH.read_bytes(), before)

    def test_received_apply_failure_is_retried_at_the_same_revision(self):
        cfg = self.publish()
        incoming = copy.deepcopy(cfg)
        incoming["haproxy"]["settings"]["maxconn"] = 9000
        incoming["_meta"]["shared_rev"] += 1
        payload = self.payload(incoming)
        self.run.return_value = (1, "reload failed")
        response = self.client.post("/api/sync/receive", json=payload).get_json()
        self.assertFalse(response["ok"])
        saved = load_config()
        self.assertEqual(saved["_meta"]["shared_rev"], payload["rev"])
        self.assertNotEqual(saved["_meta"]["applied_rev"], payload["rev"])
        self.run.return_value = (0, "")
        response = self.client.post("/api/sync/receive", json=payload).get_json()
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["applied_rev"], payload["rev"])
        self.assertEqual(response["applied_fp"], payload["fp"])

    def test_receive_keeps_local_ui_listeners_out_of_the_canonical_snapshot(self):
        cfg = load_config()
        for public, target in (("tcp://0.0.0.0:443", "tcp://192.0.2.20:443"),
                               ("http://app.example.test", "http://192.0.2.20:8080")):
            pub, error = wizard._split_url(public, "public", allow=("tcp", "http"))
            self.assertFalse(error)
            backend, error = wizard._split_url(target, "backend", allow=("tcp", "http"))
            self.assertFalse(error)
            dnsapi.wizard_publish(cfg, [pub], [backend], want_cert=False, http_redirect=False)
        save_config(cfg)
        source = self.publish()
        payload = self.payload(source)
        payload["rev"] += 10
        for url in ("http://node-b:8181", "https://node-b:8443"):
            with self.subTest(url=url):
                cfg = copy.deepcopy(source)
                cfg["local"]["web_ui"].update(enabled=True, url=url, certificate="auto")
                webui.rebuild_webui(cfg)
                save_config(cfg)
                for force in (False, True):
                    response = self.client.post(
                        "/api/sync/receive", json=dict(payload, force=force)).get_json()
                    self.assertTrue(response["ok"], response)
                    saved = load_config()
                    self.assertEqual(shared_view(saved), payload["config"])
                    self.assertEqual(saved["_meta"]["applied_fp"], payload["fp"])
                    self.assertEqual(sync.shared_payload(saved)["config"], payload["config"])
                    rendered = apply.haproxy.render_haproxy(saved)
                    self.assertIn(":" + str(wizard._split_url(url, "UI")[0]["port"]), rendered)
                    if url.startswith("https"):
                        http = next(f for f in merged(saved)["haproxy"]["frontends"]
                                    if 80 in wizard._bind_ports(f))
                        self.assertFalse(http.get("http_to_https"))
                before = CONF_PATH.read_bytes()
                rejected = self.client.post(
                    "/api/sync/receive", json=dict(payload, rev=payload["rev"] + 1, fp="invalid"))
                self.assertEqual(rejected.status_code, 422)
                self.assertEqual(CONF_PATH.read_bytes(), before)
                if url.startswith("http:"):
                    cfg = load_config()
                    pub, _ = wizard._split_url("http://business.example.test:8181", "public")
                    backend, _ = wizard._split_url("http://192.0.2.20:8081", "backend")
                    dnsapi.wizard_publish(cfg, [pub], [backend], want_cert=False, http_redirect=False)
                    for listener in cfg["haproxy"]["frontends"]:
                        if 8181 in wizard._bind_ports(listener):
                            listener["name"] = "business-listener"
                    listeners = [f for f in merged(cfg)["haproxy"]["frontends"]
                                 if 8181 in wizard._bind_ports(f)]
                    self.assertEqual(len(listeners), 1)
                    self.assertEqual(len(listeners[0]["rules"]), 2)

    def test_withdrawing_a_local_ui_does_not_publish_its_listener(self):
        cfg = load_config()
        cfg["haproxy"]["frontends"] = [
            {"id": "http", "name": "http-80", "mode": "http", "binds": "0.0.0.0:80",
             "rules": [], "certificates": []}]
        save_config(cfg)
        source = self.publish()
        canonical = copy.deepcopy(shared_view(source))
        for url in ("http://node-a:8181", "https://node-a:8443"):
            with self.subTest(url=url):
                cfg = copy.deepcopy(source)
                cfg["local"]["web_ui"].update(enabled=True, url=url, certificate="auto")
                webui.rebuild_webui(cfg)
                save_config(cfg)
                response = self.client.post("/api/webui", json={"enabled": False}).get_json()
                self.assertTrue(response["ok"], response)
                self.assertEqual(shared_view(load_config()), canonical)
                self.assertFalse(merged(load_config())["haproxy"]["rules"])

    def test_shared_wildcard_cannot_shadow_the_local_ui_on_a_compatible_listener(self):
        source = self.publish()
        for rebuild in (False, True):
            with self.subTest(rebuild=rebuild):
                cfg = copy.deepcopy(source)
                cfg["local"]["web_ui"].update(
                    enabled=True, url="http://node-a.example.test:8181", certificate="none")
                webui.rebuild_webui(cfg)
                pub, _ = wizard._split_url("http://*.example.test:8181", "public")
                target, _ = wizard._split_url("http://192.0.2.20:8080", "target")
                dnsapi.wizard_publish(cfg, [pub], [target], name="business",
                                      want_cert=False, http_redirect=False)
                if rebuild:
                    webui.rebuild_webui(cfg)
                hp = merged(cfg)["haproxy"]
                listeners = [f for f in hp["frontends"] if 8181 in wizard._bind_ports(f)]
                self.assertEqual(len(listeners), 1)
                ui_rule = cfg["local"]["web_ui"]["rule_id"]
                business_rule = next(r["id"] for r in hp["rules"] if r["id"] != ui_rule)
                self.assertLess(listeners[0]["rules"].index(ui_rule),
                                listeners[0]["rules"].index(business_rule))

    def test_ui_rebuild_never_extends_a_shared_wildcard_certificate(self):
        cfg = load_config()
        cert = {"id": "shared-wildcard", "name": "corp-wildcard",
                "domains": "*.corp.test"}
        cfg["acme"]["certificates"] = [cert]
        save_config(cfg)
        source = self.publish()
        cfg["local"]["web_ui"].update(
            enabled=True, url="https://node-b.corp.test", certificate="auto")
        webui.rebuild_webui(cfg)
        save_config(cfg)
        incoming = copy.deepcopy(source)
        incoming["cluster"]["ui_url"] = "https://admin.shared.test"
        incoming["_meta"]["shared_rev"] += 1
        payload = self.payload(incoming)
        for _ in range(2):
            response = self.client.post("/api/sync/receive", json=payload).get_json()
            self.assertTrue(response["ok"], response)
            saved = load_config()
            self.assertEqual(shared_view(saved), payload["config"])
            self.assertEqual(saved["acme"]["certificates"], [cert])
            hp = merged(saved)["haproxy"]
            listener = next(f for f in hp["frontends"] if f.get("ssl_enabled"))
            certs = {c["id"]: c for c in merged(saved)["acme"]["certificates"]}
            self.assertTrue(any(
                all(wizard.cert_for_host([certs[cid]], host)[0]
                    for host in ("node-b.corp.test", "admin.shared.test"))
                for cid in listener["certificates"]))

    def test_merged_listener_preserves_explicit_attachment_positions(self):
        cfg = load_config()
        shared = {"id": "shared-listener", "name": "shared-listener", "mode": "http",
                  "binds": "0.0.0.0:8181", "rules": ["first", "last"],
                  "certificates": ["shared-cert"]}
        cfg["haproxy"]["frontends"] = [shared]
        cfg["haproxy"]["rules"] = [{"id": rid} for rid in ("first", "last")]
        cfg["local"]["haproxy"]["frontends"] = [
            dict(shared, id="local-listener", name="local-listener",
                 rules=["local-rule"], certificates=["local-cert"])]
        cfg["local"]["haproxy"]["rules"] = [{"id": "local-rule"}]
        cfg["local"]["attach"] = {"local-listener": {
            "rules": ["local-rule"], "certificates": ["local-cert"],
            "at": {"local-rule": 1, "local-cert": 0}}}
        listener = merged(cfg)["haproxy"]["frontends"][0]
        self.assertEqual(listener["rules"], ["first", "local-rule", "last"])
        self.assertEqual(listener["certificates"], ["local-cert", "shared-cert"])

    def test_local_ui_changes_are_dirty_without_changing_shared_revision(self):
        source = self.publish()
        canonical = shared_view(source)
        for url in ("http://node-a:8181", "https://node-a:8443"):
            with self.subTest(url=url):
                save_config(copy.deepcopy(source))
                response = self.client.post("/api/webui", json={
                    "enabled": True, "url": url, "certificate": "auto",
                    "http_redirect": False, "apply": False}).get_json()
                self.assertTrue(response["ok"], response)
                status = self.client.get("/api/status?brief=1").get_json()
                self.assertTrue(status["dirty"], status)
                saved = load_config()
                self.assertEqual(saved["_meta"]["shared_rev"], source["_meta"]["shared_rev"])
                self.assertEqual(shared_view(saved), canonical)
                self.publish()
                self.assertFalse(self.client.get("/api/status?brief=1").get_json()["dirty"])

    def test_tidying_a_local_ui_keeps_its_listener_local(self):
        cfg = self.publish()
        canonical = copy.deepcopy(shared_view(cfg))
        cfg["local"]["web_ui"].update(
            enabled=True, url="https://node-a:8443", certificate="auto")
        webui.rebuild_webui(cfg)
        local = cfg["local"]["haproxy"]
        duplicate = dict(local["rules"][0], id="extra-ui-rule")
        local["rules"].append(duplicate)
        listener = next(f for f in local["frontends"] if 8443 in wizard._bind_ports(f))
        listener["rules"].append(duplicate["id"])
        save_config(cfg)
        response = self.client.post("/api/webui/tidy", json={}).get_json()
        self.assertTrue(response["ok"], response)
        self.assertEqual(shared_view(load_config()), canonical)
        self.assertEqual(len(merged(load_config())["haproxy"]["rules"]), 1)
        self.assertTrue(any(f.get("http_to_https")
                            for f in merged(load_config())["haproxy"]["frontends"]))

    def test_background_sync_retries_received_but_unapplied_configuration(self):
        cfg = self.publish()
        cfg["local"]["sync"]["peers"] = [self.peer]
        payload = sync.shared_payload(cfg)
        node = {"url": self.peer["url"], "reachable": True,
                "config_rev": payload["rev"], "config_fp": payload["fp"],
                "published_rev": payload["rev"], "published_fp": payload["fp"],
                "applied_rev": 0, "applied_fp": "", "apply_error": "reload failed"}
        sync._requests.post.return_value = self.response(
            {"ok": True, "applied_rev": payload["rev"], "applied_fp": payload["fp"]})
        result = sync.reconcile(cfg, [node])
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["ok"])
        node.update(applied_rev=payload["rev"], applied_fp=payload["fp"], apply_error="")
        self.assertEqual(sync.reconcile(cfg, [node]), [])

    def test_background_does_not_repeatedly_reload_a_legacy_peer(self):
        cfg = self.publish()
        cfg["local"]["sync"]["peers"] = [self.peer]
        save_config(cfg)
        node = peering._node_summary({
            "hostname": "node-b", "config_rev": cfg["_meta"]["shared_rev"],
            "config_fp": shared_fingerprint(cfg)})
        node.update(id="p", name="node-b", url=self.peer["url"], reachable=True)
        sync._requests.post.return_value = self.response({"ok": True, "applied": {"ok": True}})
        sync.reconcile(cfg, [node])
        sync.reconcile(cfg, [node])
        sync._requests.post.assert_not_called()
        with patch.object(peering, "_query_peer", return_value=node):
            snapshot = cluster.cluster_snapshot(cfg)
        self.assertFalse(snapshot["summary"]["config_agreed"])
        self.assertTrue(any("upgrade" in w.lower() for w in snapshot["summary"]["warnings"]))

    def test_new_unpublished_peer_still_receives_automatic_sync(self):
        cfg = self.publish()
        cfg["local"]["sync"]["peers"] = [self.peer]
        node = peering._node_summary({"hostname": "node-b", "config_rev": 0, "config_fp": "",
                                     "published_rev": 0, "published_fp": "",
                                     "applied_rev": 0, "applied_fp": ""})
        node.update(url=self.peer["url"], reachable=True)
        payload = sync.shared_payload(cfg)
        sync._requests.post.return_value = self.response(
            {"ok": True, "applied_rev": payload["rev"], "applied_fp": payload["fp"]})
        self.assertTrue(sync.reconcile(cfg, [node])[0]["ok"])

    def test_startup_does_not_pull_an_unpublished_draft(self):
        cfg = self.publish()
        cfg["local"]["sync"]["peers"] = [self.peer]
        sync._requests.get.return_value = self.response(
            {"config_rev": 100, "config_fp": "unpublished",
             "published_rev": cfg["_meta"]["applied_rev"],
             "published_fp": cfg["_meta"]["applied_fp"]})
        self.assertIsNone(sync.catch_up(cfg))
        self.assertEqual(sync._requests.get.call_count, 1)

    def test_startup_retries_the_same_published_revision_after_apply_failure(self):
        cfg = self.publish()
        incoming = copy.deepcopy(cfg)
        incoming["haproxy"]["settings"]["maxconn"] = 9000
        incoming["_meta"]["shared_rev"] += 1
        payload = self.payload(incoming)
        self.run.return_value = (1, "reload failed")
        self.client.post("/api/sync/receive", json=payload)
        cfg = load_config()
        cfg["local"]["sync"]["peers"] = [self.peer]
        save_config(cfg)
        self.run.return_value = (0, "")
        sync._requests.get.side_effect = [
            self.response({"published_rev": payload["rev"], "published_fp": payload["fp"]}),
            self.response(payload)]
        result = sync.catch_up(cfg)
        self.assertTrue(result["ok"], result)
        self.assertEqual(load_config()["_meta"]["applied_rev"], payload["rev"])

    def test_background_retries_this_nodes_failed_published_revision(self):
        cfg = self.publish()
        cfg["local"]["sync"]["peers"] = [self.peer]
        save_config(cfg)
        incoming = copy.deepcopy(cfg)
        incoming["haproxy"]["settings"]["maxconn"] = 9000
        incoming["_meta"]["shared_rev"] += 1
        payload = self.payload(incoming)
        self.run.return_value = (1, "reload failed")
        self.client.post("/api/sync/receive", json=payload)
        cfg = load_config()
        self.run.return_value = (0, "")
        status = {"published_rev": payload["rev"], "published_fp": payload["fp"],
                  "applied_rev": payload["rev"], "applied_fp": payload["fp"]}
        sync._requests.get.side_effect = [self.response(status), self.response(payload)]
        result = sync.reconcile(cfg, [
            dict(status, url=self.peer["url"], reachable=True)])
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["ok"], result)
        self.assertEqual(load_config()["_meta"]["applied_rev"], payload["rev"])

    def test_overwrite_requires_apply_and_relabels_the_applied_snapshot(self):
        self.publish()
        cfg = self.edit()
        with self.assertRaises(ValueError):
            sync.make_newest(cfg)
        sync._requests.get.assert_not_called()
        cfg = self.publish()
        cfg["local"]["sync"]["peers"] = [self.peer]
        save_config(cfg)
        sync._requests.get.return_value = self.response({"config_rev": 12})
        cfg = sync.make_newest(cfg)
        self.assertEqual(cfg["_meta"]["applied_rev"], 13)
        self.assertEqual(sync.shared_payload(cfg)["rev"], 13)
        self.assertEqual(sync.shared_payload(cfg)["config"], shared_view(cfg))

    def test_recovered_peer_gets_published_snapshot_not_new_draft(self):
        published = self.publish()
        cfg = self.edit()
        cfg["local"]["sync"]["peers"] = [self.peer]
        payload = sync.shared_payload(cfg)
        node = {"url": self.peer["url"], "reachable": False}
        self.assertEqual(sync.reconcile(cfg, [node]), [])
        node.update(reachable=True, config_rev=0, config_fp="old",
                    published_rev=0, published_fp="old", applied_rev=0, applied_fp="old")
        sync._requests.post.return_value = self.response(
            {"ok": True, "applied_rev": payload["rev"], "applied_fp": payload["fp"]})
        self.assertTrue(sync.reconcile(cfg, [node])[0]["ok"])
        sent = sync._requests.post.call_args.kwargs["json"]
        self.assertEqual(sent["config"], shared_view(published))

    def test_legacy_configuration_requires_explicit_apply_before_publication(self):
        cfg = load_config()
        cfg["_meta"]["applied_hash"] = config_hash(cfg)
        cfg["_meta"].pop("published", None)
        save_config(cfg)
        with self.assertRaises(ValueError):
            sync.shared_payload(load_config())
        self.assertEqual(load_config()["_meta"]["applied_hash"], config_hash(cfg))
        self.assertTrue(sync.shared_payload(self.publish())["fp"])

    def test_legacy_unhashed_drafts_cannot_be_inferred_as_published(self):
        for section in ("access", "notify"):
            with self.subTest(section=section):
                cfg = copy.deepcopy(DEFAULT_CONFIG)
                cfg["local"]["api_key"] = "receiver-key"
                cfg["_meta"]["applied_hash"] = config_hash(cfg)
                if section == "access":
                    cfg["access"]["groups"].append({"id": "draft", "name": "unapplied"})
                else:
                    cfg["notify"]["repeat_hours"] = 12
                self.assertEqual(config_hash(cfg), cfg["_meta"]["applied_hash"])
                save_config(cfg)
                response = self.client.get("/api/sync/pull")
                self.assertEqual(response.status_code, 409)
                self.assertNotIn("published", load_config()["_meta"])

    def test_legacy_dirty_configuration_is_not_inferred_as_published(self):
        cfg = load_config()
        cfg["_meta"]["applied_hash"] = config_hash(cfg)
        cfg["haproxy"]["settings"]["maxconn"] = 9000
        save_config(cfg)
        with self.assertRaises(ValueError):
            sync.shared_payload(load_config())

    def test_join_uses_the_same_validated_receive_and_apply_path(self):
        incoming = load_config()
        incoming["haproxy"]["settings"]["maxconn"] = 9000
        payload = dict(self.payload(incoming), peers=[self.peer])
        for succeeds in (True, False):
            with self.subTest(succeeds=succeeds):
                cfg = copy.deepcopy(DEFAULT_CONFIG)
                cfg["local"]["api_key"] = "receiver-key"
                cfg["local"]["web_ui"].update(
                    enabled=True, url="http://node-a:8181", certificate="none")
                webui.rebuild_webui(cfg)
                save_config(cfg)
                self.run.return_value = (0, "") if succeeds else (1, "reload failed")
                requests = Mock()
                requests.request.side_effect = [
                    self.response({"hostname": "node-b", "vips": []}),
                    self.response([]), self.response({})]
                requests.get.return_value = self.response(payload)
                with patch.object(setup, "_requests", requests), \
                        patch.object(setup.vrrp, "check_node_url", return_value=("ok", "")), \
                        patch.object(apply, "node_interfaces", return_value=[]):
                    response = self.client.post("/api/setup/join", json={
                        "node_url": "http://node-a", "peer_url": self.peer["url"],
                        "peer_api_key": "peer-key"}).get_json()
                self.assertEqual(response["synced"], succeeds, response)
                if succeeds:
                    self.assertEqual(load_config()["_meta"]["applied_fp"], payload["fp"])

    def test_cluster_cannot_report_agreement_for_a_failed_application(self):
        cfg = self.publish()
        cfg["local"]["sync"]["peers"] = [self.peer]
        save_config(cfg)
        with ham.app.test_request_context("/api/status"):
            status = apply.api_status().get_json()
        node = peering._node_summary(status)
        node.update(id="p", name="node-b", url=self.peer["url"], reachable=True,
                    applied_fp="", applied_rev=0, apply_error="reload failed")
        with patch.object(peering, "_query_peer", return_value=node):
            result = cluster.cluster_snapshot()
        self.assertFalse(result["summary"]["config_agreed"])
        self.assertTrue(any("reload failed" in w for w in result["summary"]["warnings"]))


class CertificateStagingTests(unittest.TestCase):
    def test_missing_local_certificate_is_created_only_in_staging(self):
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cert = {"id": "local-ui", "name": "local-ui", "domains": "node.example.test"}
        cfg["acme"]["certificates"] = [cert]
        cfg["haproxy"]["frontends"] = [
            {"id": "tls", "ssl_enabled": True, "certificates": [cert["id"]]}]
        live_path = apply.cert_path(cert)
        with tempfile.TemporaryDirectory(prefix="ham-cert-stage-") as stage:
            with patch.object(apply, "_selfsigned_placeholder") as placeholder:
                placeholder.side_effect = lambda path, domains: path.write_bytes(b"placeholder")
                rendered = apply._staged(cfg, "crt " + str(live_path), stage, certificates={})
            staged_path = pathlib.Path(stage) / "certs" / live_path.name
            self.assertTrue(staged_path.exists())
            self.assertIn(str(staged_path), rendered)
            self.assertFalse(live_path.exists())


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        _tmp.cleanup()
