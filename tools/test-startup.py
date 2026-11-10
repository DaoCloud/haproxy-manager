#!/usr/bin/env python3
"""Preseeded startup, first-join recovery and publication arbitration."""
import copy
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
_tmp = tempfile.TemporaryDirectory(prefix="ham-startup-")
for key, name in {
    "HAM_DATA_DIR": "data", "HAM_CERT_DIR": "certs",
    "HAM_HAPROXY_CFG": "haproxy.cfg", "HAM_KEEPALIVED_CFG": "keepalived.conf",
    "HAM_ERROR_DIR": "errors", "HAM_CROWDSEC_SPOE": "crowdsec.cfg",
}.items():
    os.environ[key] = str(pathlib.Path(_tmp.name) / name)
os.environ["HAM_DRY_RUN"] = "1"

import ham  # noqa: E402
from ham import apply, auth, startup, sync, vrrp  # noqa: E402
from ham.config import (CONF_PATH, DEFAULT_CONFIG, load_config, save_config,  # noqa: E402
                        shared_fingerprint, shared_view)


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.object(startup, "ENABLED", True).start()
        patch.object(startup, "_status", {"enabled": True, "state": "checking",
                                        "local_ok": False, "error": "", "attempts": 0}).start()
        patch.object(startup, "_join_baseline", None).start()
        self.peers = patch.object(startup, "_publications", return_value=([], ["peer offline"])).start()
        self.get = patch.object(startup._requests, "get").start()
        patch.object(startup.watchdog, "probe_self", return_value=(True, "", 0)).start()
        self.sleep = patch.object(startup.time, "sleep").start()
        patch.object(apply, "node_interfaces",
                     return_value=[{"name": "eth0", "addresses": ["192.0.2.11/24"]}]).start()
        patch.object(apply, "_haproxy_check", return_value=(0, "", "global\n")).start()
        self.health = patch.object(apply, "_haproxy_not_serving", return_value="").start()
        patch.object(apply, "ensure_cert_files", return_value=[]).start()
        patch.object(apply, "run", return_value=(0, "")).start()
        patch.object(apply.errorpages, "write_files", return_value=[]).start()
        patch.object(apply.crowdsec, "write_files", return_value=[]).start()
        patch.object(apply.crowdsec, "ensure_service", return_value=("", "")).start()
        patch.object(apply.keepalived, "write_track_script", return_value=True).start()
        patch.object(apply.vrrp, "derive_unicast", return_value=False).start()
        patch.object(apply, "check_rendered", return_value=(True, "")).start()
        patch.object(auth, "node_role", return_value=("active", [])).start()
        self.push = patch.object(sync, "sync_push").start()
        for path in (apply.HAPROXY_CFG, apply.KEEPALIVED_CFG):
            path.unlink(missing_ok=True)
        self.cfg = copy.deepcopy(DEFAULT_CONFIG)
        self.cfg["local"].update(api_key="local-test-key", node_url="http://192.0.2.11:8080")
        self.cfg["local"]["sync"]["peers"] = [
            {"id": "peer", "name": "peer", "url": "http://192.0.2.12:8080",
             "api_key": "peer-test-key"}]
        self.seed(self.cfg)
        save_config(self.cfg)

    def seed(self, cfg):
        cfg["cluster"]["vips"] = "192.0.2.100/24"
        cfg["haproxy"]["servers"] = [
            {"id": "server", "name": "web", "address": "198.51.100.20", "port": 8080}]
        cfg["haproxy"]["backends"] = [
            {"id": "pool", "name": "web-pool", "mode": "tcp", "servers": ["server"]}]
        cfg["haproxy"]["frontends"] = [
            {"id": "listener", "name": "web-in", "mode": "tcp",
             "binds": "192.0.2.100:80", "default_backend": "pool"}]

    def publish(self):
        result = apply.do_apply(allow_push=False, startup_apply=True)
        self.assertTrue(result["local_ok"], result)
        startup._status["state"] = "checking"
        return load_config()

    def offer(self, cfg, rev):
        publication = {"peer": self.cfg["local"]["sync"]["peers"][0],
                       "rev": rev, "fp": shared_fingerprint(cfg)}
        payload = {"config": shared_view(cfg), "rev": rev, "fp": publication["fp"], "certs": {}}
        self.peers.return_value = ([publication], [])
        self.get.return_value = Mock(json=Mock(return_value=payload))
        return publication

    def receive(self, cfg, rev=1, force=False):
        ham.app.config["TESTING"] = True
        return ham.app.test_client().post(
            "/api/sync/receive",
            json={"config": shared_view(cfg), "rev": rev, "fp": shared_fingerprint(cfg),
                  "certs": {}, "force": force},
            headers={"X-API-Key": "local-test-key"})

    def local_only(self):
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["local"] = copy.deepcopy(self.cfg["local"])
        save_config(cfg)
        return cfg

    def test_partial_seed_is_filled_applied_and_observable_with_peer_offline(self):
        partial = {key: copy.deepcopy(self.cfg[key]) for key in ("cluster", "local", "haproxy")}
        partial["haproxy"].pop("settings")
        CONF_PATH.write_text(json.dumps(partial))
        with self.assertLogs("haproxy-manager", level="INFO") as logs:
            startup.run()
        stored = load_config()
        self.assertEqual(stored["haproxy"]["settings"]["maxconn"], 4000)
        self.assertTrue(stored["_meta"]["setup_complete"])
        self.assertTrue(stored["_meta"]["published"])
        self.assertEqual(startup.status()["state"], "ready")
        self.assertTrue(startup.status()["local_ok"])
        self.push.assert_not_called()
        self.assertIn("state=ready", "\n".join(logs.output))
        self.assertNotIn("local-test-key", "\n".join(logs.output))
        self.assertNotIn("peer-test-key", "\n".join(logs.output))
        ham.app.config["TESTING"] = True
        response = ham.app.test_client().get("/api/status?brief=1",
                                            headers={"X-API-Key": "local-test-key"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["startup"]["state"], "ready")

    def test_local_only_node_waits_without_publishing_empty_configuration(self):
        self.local_only()
        startup.attempt()
        self.assertEqual(startup.status()["state"], "waiting_peer")
        self.assertNotIn("published", load_config()["_meta"])
        self.assertFalse(apply.HAPROXY_CFG.exists())
        self.assertTrue(startup.blocks_sync())

    def test_waiting_node_retries_and_receives_publication_preserving_identity(self):
        local = self.local_only()
        publication = self.offer(self.cfg, 1)
        self.peers.side_effect = [([], ["peer offline"]), ([publication], [])]
        startup.run()
        stored = load_config()
        self.assertEqual(startup.status()["attempts"], 2)
        self.assertEqual(startup.status()["state"], "ready")
        self.assertEqual(stored["local"]["api_key"], local["local"]["api_key"])
        self.assertEqual(stored["_meta"]["applied_rev"], 1)
        self.assertEqual(stored["cluster"]["vips"], "192.0.2.100/24")
        self.assertTrue(stored["_meta"]["setup_complete"])
        self.sleep.assert_called_once_with(5)

    def test_first_join_retries_a_transient_pull_failure(self):
        self.local_only()
        self.offer(self.cfg, 1)
        response = self.get.return_value
        self.get.side_effect = [startup._requests.exceptions.ConnectionError("temporarily offline"),
                                response]
        startup.run()
        self.assertEqual(startup.status()["state"], "ready")
        self.assertEqual(startup.status()["attempts"], 2)

    def test_clean_restart_accepts_newer_peer_publication(self):
        published = self.publish()
        published["_meta"]["shared_rev"] = 100
        save_config(published)
        remote = copy.deepcopy(published)
        remote["haproxy"]["servers"][0]["port"] = 9090
        self.offer(remote, published["_meta"]["published"]["rev"] + 1)
        startup.run()
        self.assertEqual(load_config()["haproxy"]["servers"][0]["port"], 9090)
        self.assertEqual(startup.status()["state"], "ready")

    def test_same_revision_different_publication_does_not_overwrite_local(self):
        published = self.publish()
        remote = copy.deepcopy(published)
        remote["haproxy"]["servers"][0]["port"] = 9090
        self.offer(remote, published["_meta"]["published"]["rev"])
        startup.run()
        self.assertEqual(startup.status()["state"], "conflict")
        self.assertEqual(load_config()["haproxy"]["servers"][0]["port"], 8080)

    def test_restart_applies_a_draft_when_peer_still_holds_its_baseline(self):
        published = self.publish()
        edited = load_config()
        edited["haproxy"]["servers"][0]["port"] = 9090
        save_config(edited)
        self.offer(published, published["_meta"]["published"]["rev"])
        startup.run()
        self.assertEqual(startup.status()["state"], "ready")
        self.assertEqual(load_config()["haproxy"]["servers"][0]["port"], 9090)
        self.assertEqual(load_config()["_meta"]["applied_fp"], shared_fingerprint(load_config()))

    def test_offline_restart_restores_an_existing_clean_publication(self):
        self.publish()
        startup.run()
        self.assertEqual(startup.status()["state"], "ready")

    def test_offline_restart_does_not_publish_reverted_drafts_as_a_newer_version(self):
        published = self.publish()
        cfg = load_config()
        cfg["haproxy"]["servers"][0]["port"] = 9090
        save_config(cfg)
        cfg["haproxy"]["servers"][0]["port"] = 8080
        save_config(cfg)
        self.assertGreater(cfg["_meta"]["shared_rev"], published["_meta"]["published"]["rev"])
        startup.run()
        restored = load_config()
        self.assertEqual(restored["_meta"]["published"], published["_meta"]["published"])
        self.assertEqual(restored["_meta"]["shared_rev"], published["_meta"]["published"]["rev"])
        remote = copy.deepcopy(published)
        remote["haproxy"]["servers"][0]["port"] = 9091
        self.offer(remote, published["_meta"]["published"]["rev"] + 1)
        startup.run()
        self.assertEqual(load_config()["haproxy"]["servers"][0]["port"], 9091)
        self.assertEqual(startup.status()["state"], "ready")

    def test_offline_draft_is_not_blindly_published(self):
        previous = self.publish()
        cfg = load_config()
        cfg["haproxy"]["servers"][0]["port"] = 9090
        save_config(cfg)
        startup.run()
        self.assertEqual(startup.status()["state"], "conflict")
        self.assertEqual(load_config()["_meta"]["published"], previous["_meta"]["published"])
        self.assertEqual(load_config()["haproxy"]["servers"][0]["port"], 9090)

    def test_competing_draft_and_peer_publication_are_preserved(self):
        previous = self.publish()
        local = load_config()
        local["haproxy"]["servers"][0]["port"] = 9090
        save_config(local)
        remote = copy.deepcopy(previous)
        remote["haproxy"]["settings"]["maxconn"] = 9000
        self.offer(remote, local["_meta"]["shared_rev"])
        original = CONF_PATH.read_text()
        startup.run()
        self.assertEqual(startup.status()["state"], "conflict")
        self.assertEqual(load_config()["haproxy"], json.loads(original)["haproxy"])
        self.assertEqual(load_config()["_meta"]["published"], previous["_meta"]["published"])
        self.get.assert_not_called()
        self.push.assert_not_called()

    def test_simultaneous_identical_seeds_adopt_the_same_publication(self):
        self.offer(self.cfg, 1)
        startup.run()
        self.assertEqual(startup.status()["state"], "ready")
        self.assertEqual(load_config()["_meta"]["applied_rev"], 1)

    def test_unrelated_seed_does_not_overwrite_existing_cluster(self):
        remote = copy.deepcopy(self.cfg)
        remote["haproxy"]["servers"][0]["port"] = 9090
        self.offer(remote, 3)
        startup.run()
        self.assertEqual(startup.status()["state"], "conflict")
        self.assertNotIn("published", load_config()["_meta"])

    def test_failed_apply_is_reported_and_a_restart_can_recover(self):
        self.health.return_value = "port already in use"
        with patch.object(apply, "_restore_previous_haproxy_cfg", return_value="previous restored"):
            startup.run()
        self.assertEqual(startup.status()["state"], "failed")
        self.assertFalse(startup.status()["local_ok"])
        self.assertNotIn("published", load_config()["_meta"])
        self.health.return_value = ""
        startup.run()
        self.assertEqual(startup.status()["state"], "ready")
        self.assertNotIn("apply_error", load_config()["_meta"])

    def test_invalid_json_and_wrong_structure_are_not_overwritten(self):
        for text in ('{"haproxy":', '{"haproxy": null}', '{"haproxy":{"servers":[null]}}',
                     '{"haproxy":{"servers":[{"name":23}]}}'):
            with self.subTest(text=text):
                CONF_PATH.write_text(text)
                startup.run()
                self.assertEqual(startup.status()["state"], "failed")
                self.assertEqual(CONF_PATH.read_text(), text)

    def test_invalid_references_and_duplicate_ids_cannot_be_applied(self):
        for change in ("reference", "duplicate"):
            with self.subTest(change=change):
                cfg = copy.deepcopy(self.cfg)
                if change == "reference":
                    cfg["haproxy"]["backends"][0]["servers"] = ["missing"]
                else:
                    cfg["haproxy"]["servers"].append(copy.deepcopy(cfg["haproxy"]["servers"][0]))
                CONF_PATH.write_text(json.dumps(cfg))
                startup.run()
                self.assertEqual(startup.status()["state"], "failed")
                self.assertFalse(apply.HAPROXY_CFG.exists())
                self.assertNotIn("published", load_config()["_meta"])

    def test_ui_edit_during_peer_check_is_not_overwritten(self):
        def edit(cfg):
            current = load_config()
            current["haproxy"]["settings"]["maxconn"] = 8000
            save_config(current)
            return [], []
        self.peers.side_effect = edit
        startup.run()
        self.assertEqual(startup.status()["state"], "conflict")
        self.assertEqual(load_config()["haproxy"]["settings"]["maxconn"], 8000)
        self.assertNotIn("published", load_config()["_meta"])

    def test_shared_fields_outside_haproxy_are_checked_for_concurrent_edits(self):
        def edit(cfg):
            current = load_config()
            current["notify"]["enabled"] = False
            save_config(current)
            return [], []
        self.peers.side_effect = edit
        startup.run()
        self.assertEqual(startup.status()["state"], "conflict")
        self.assertFalse(load_config()["notify"]["enabled"])
        self.assertNotIn("published", load_config()["_meta"])

    def test_waiting_does_not_turn_into_a_file_watcher(self):
        self.local_only()
        def edit(_):
            current = load_config()
            self.seed(current)
            CONF_PATH.write_text(json.dumps(current))
        self.sleep.side_effect = edit
        startup.run()
        self.assertEqual(startup.status()["state"], "conflict")
        self.assertNotIn("published", load_config()["_meta"])
        self.assertFalse(apply.HAPROXY_CFG.exists())

    def test_first_push_during_peer_check_finishes_waiting_without_false_conflict(self):
        self.local_only()
        startup.attempt()
        payload = {"config": shared_view(self.cfg), "rev": 1,
                   "fp": shared_fingerprint(self.cfg), "certs": {}}
        def receive(cfg):
            with ham.app.test_request_context("/api/sync/receive"):
                result = sync._receive_locked(load_config(), payload, payload["config"])
                self.assertTrue(result.get_json()["ok"])
            return [], []
        self.peers.side_effect = receive
        startup.attempt()
        self.assertEqual(startup.status()["state"], "ready")
        self.assertEqual(load_config()["_meta"]["applied_rev"], 1)

    def test_seed_startup_allows_a_shared_cluster_api_key(self):
        cfg = load_config()
        cfg["local"]["sync"]["peers"][0]["api_key"] = cfg["local"]["api_key"]
        save_config(cfg)
        startup.run()
        self.assertEqual(startup.status()["state"], "ready")
        self.assertTrue(load_config()["_meta"]["published"])

    def test_first_push_allows_shared_keys_but_not_a_self_peer_url(self):
        self.cfg["local"]["sync"]["peers"][0]["api_key"] = self.cfg["local"]["api_key"]
        self.local_only()
        startup.attempt()
        response = self.receive(self.cfg)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertEqual(startup.status()["state"], "ready")
        cfg = load_config()
        self.assertEqual(cfg["local"]["sync"]["peers"][0]["api_key"], cfg["local"]["api_key"])
        cfg["local"]["sync"]["peers"][0]["url"] = cfg["local"]["node_url"]
        save_config(cfg)
        startup.run()
        self.assertEqual(startup.status()["state"], "failed")
        self.assertIn("must not point to this node", startup.status()["error"])

    def test_first_push_cannot_overwrite_a_draft_saved_after_initialization(self):
        for auto_sync, revision in ((True, 1), (True, 9), (False, 9)):
            with self.subTest(auto_sync=auto_sync, revision=revision):
                cfg = self.local_only()
                cfg["local"]["sync"]["auto_sync"] = auto_sync
                save_config(cfg)
                startup._status["state"] = "checking"
                startup.attempt()
                ham.app.config["TESTING"] = True
                response = ham.app.test_client().put(
                    "/api/haproxy/settings", json={"maxconn": 9000},
                    headers={"X-API-Key": "local-test-key"})
                self.assertEqual(response.status_code, 200)
                response = self.receive(self.cfg, revision)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(load_config()["haproxy"]["settings"]["maxconn"], 9000)
                self.assertNotIn("published", load_config()["_meta"])
                response = self.receive(self.cfg, revision, force=True)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.get_json()["ok"])
                self.assertEqual(startup.status()["state"], "ready")

    def test_untouched_auto_sync_disabled_node_accepts_the_first_publication(self):
        cfg = self.local_only()
        cfg["local"]["sync"]["auto_sync"] = False
        save_config(cfg)
        startup.run()
        self.assertEqual(startup.status()["state"], "idle")
        response = self.receive(self.cfg)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertFalse(load_config()["local"]["sync"]["auto_sync"])
        self.assertEqual(startup.status()["state"], "ready")
        self.assertEqual(load_config()["_meta"]["applied_rev"], 1)

    def test_first_push_after_a_failed_initial_pull_uses_the_same_draft_guard(self):
        for draft in (False, True):
            with self.subTest(draft=draft):
                self.local_only()
                startup._status["state"] = "checking"
                self.offer(self.cfg, 1)
                self.get.return_value.json.return_value["rev"] = 2
                startup.attempt()
                self.assertEqual(startup.status()["state"], "waiting_peer")
                if draft:
                    cfg = load_config()
                    cfg["haproxy"]["settings"]["maxconn"] = 9000
                    save_config(cfg)
                response = self.receive(self.cfg, 9 if draft else 1)
                self.assertEqual(response.status_code, 409 if draft else 200)
                if draft:
                    self.assertEqual(load_config()["haproxy"]["settings"]["maxconn"], 9000)

    def test_successful_push_between_retry_check_and_attempt_stays_ready(self):
        self.local_only()
        attempt = startup.attempt

        def receive_before_retry(expected=None):
            if expected is not None:
                response = self.receive(self.cfg)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.get_json()["ok"])
            return attempt(expected)

        with patch.object(startup, "attempt", side_effect=receive_before_retry):
            startup.run()
        self.assertEqual(startup.status()["state"], "ready")
        self.assertFalse(startup.blocks_sync())
        self.assertEqual(load_config()["_meta"]["applied_rev"], 1)

    def test_non_vip_node_does_not_require_a_keepalived_interface(self):
        cfg = load_config()
        cfg["cluster"]["vips"] = ""
        cfg["haproxy"]["frontends"][0]["binds"] = "192.0.2.11:80"
        save_config(cfg)
        with patch.object(apply, "node_interfaces",
                          return_value=[{"name": "ens192", "addresses": ["192.0.2.11/24"]}]):
            startup.run()
        self.assertEqual(startup.status()["state"], "ready")
        self.assertTrue(apply.HAPROXY_CFG.exists())
        self.assertFalse(apply.KEEPALIVED_CFG.exists())

    def test_first_vip_receive_validates_the_receivers_interface_before_saving(self):
        self.local_only()
        with patch.object(apply, "node_interfaces",
                          return_value=[{"name": "ens192", "addresses": ["192.0.2.11/24"]}]):
            startup.attempt()
            self.assertEqual(startup.status()["state"], "waiting_peer")
            response = self.receive(self.cfg)
            self.assertEqual(response.status_code, 422)
            self.assertIn("VIP interface eth0", response.get_json()["error"])
            self.assertNotIn("published", load_config()["_meta"])
            self.assertFalse(apply.HAPROXY_CFG.exists())
            self.assertFalse(apply.KEEPALIVED_CFG.exists())
            cfg = load_config()
            cfg["local"]["keepalived"]["interface"] = "ens192"
            save_config(cfg)
            response = self.receive(self.cfg)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertEqual(load_config()["local"]["keepalived"]["interface"], "ens192")

    def test_blocked_startup_can_be_recovered_only_by_an_explicit_action(self):
        startup._status["state"] = "conflict"
        self.assertTrue(startup.receive_error())
        self.assertEqual(sync.reconcile(load_config(), []), [])
        self.assertIsNone(sync.catch_up(load_config()))
        self.assertFalse(startup.receive_error(force=True))
        self.assertTrue(apply.do_apply(allow_push=False)["local_ok"])
        self.assertEqual(startup.status()["state"], "ready")
        self.assertTrue(load_config()["_meta"]["setup_complete"])

    def test_busy_startup_rejects_even_a_forced_receive(self):
        self.assertTrue(startup.receive_error(force=True))
        result = apply.do_apply(allow_push=False)
        self.assertFalse(result["local_ok"])
        self.assertFalse(apply.HAPROXY_CFG.exists())
        ham.app.config["TESTING"] = True
        response = ham.app.test_client().post("/api/apply", json={},
                                             headers={"X-API-Key": "local-test-key"})
        self.assertEqual(response.status_code, 409)

    def test_invalid_vrrp_source_and_virtual_management_address_are_rejected(self):
        for field in ("source", "url"):
            with self.subTest(field=field):
                cfg = copy.deepcopy(self.cfg)
                if field == "source":
                    cfg["local"]["keepalived"]["unicast_src"] = "198.51.100.11"
                else:
                    cfg["local"]["node_url"] = "http://192.0.2.100:8080"
                CONF_PATH.write_text(json.dumps(cfg))
                startup.run()
                self.assertEqual(startup.status()["state"], "failed")
                self.assertFalse(apply.HAPROXY_CFG.exists())

    def test_native_installation_does_not_apply_on_start(self):
        with patch.object(startup, "ENABLED", False):
            startup.run()
        self.peers.assert_not_called()
        self.assertNotIn("published", load_config()["_meta"])

    def test_auto_sync_disabled_applies_local_seed_without_pulling_a_peer(self):
        cfg = load_config()
        cfg["local"]["sync"]["auto_sync"] = False
        save_config(cfg)
        startup.run()
        self.assertEqual(startup.status()["state"], "ready")
        self.peers.assert_not_called()
        self.get.assert_not_called()

    def test_local_only_node_does_not_auto_join_when_auto_sync_is_disabled(self):
        cfg = self.local_only()
        cfg["local"]["sync"]["auto_sync"] = False
        save_config(cfg)
        startup.run()
        self.assertEqual(startup.status()["state"], "idle")
        self.assertNotIn("published", load_config()["_meta"])
        self.peers.assert_not_called()


class VrrpAndSupervisorTests(unittest.TestCase):
    def test_explicit_business_network_addresses_survive_management_url_derivation(self):
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["cluster"]["vips"] = "192.0.2.100/24"
        cfg["local"]["keepalived"].update(unicast_src="192.0.2.11", unicast_peer="192.0.2.12")
        cfg["local"]["sync"]["peers"] = [{"url": "http://198.51.100.12:8080"}]
        with patch.object(vrrp, "resolve_host", return_value=["198.51.100.12"]):
            self.assertFalse(vrrp.derive_unicast(cfg))
        self.assertEqual(cfg["local"]["keepalived"]["unicast_peer"], "192.0.2.12")

    def test_derived_addresses_refresh_but_are_not_cleared_on_dns_failure(self):
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["cluster"]["vips"] = "192.0.2.100/24"
        cfg["local"]["sync"]["peers"] = [{"url": "http://peer:8080"}]
        with patch.object(vrrp, "resolve_host", return_value=["192.0.2.12"]), \
                patch.object(vrrp, "local_vrrp_address", return_value="192.0.2.11"):
            self.assertTrue(vrrp.derive_unicast(cfg))
        with patch.object(vrrp, "resolve_host", return_value=[]):
            self.assertFalse(vrrp.derive_unicast(cfg))
        self.assertEqual(cfg["local"]["keepalived"]["unicast_peer"], "192.0.2.12")

    def test_first_derivation_failure_never_falls_back_to_multicast(self):
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["cluster"]["vips"] = "192.0.2.100/24"
        cfg["local"]["sync"]["peers"] = [{"url": "http://unresolved:8080"}]
        with patch.object(vrrp, "resolve_host", return_value=[]):
            with self.assertRaisesRegex(ValueError, "Cannot derive VRRP"):
                vrrp.derive_unicast(cfg)

    def test_waiting_haproxy_wrapper_is_restarted_not_signalled(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory(prefix="ham-supervisor-startup-") as directory:
            path = pathlib.Path(directory)
            shim = (root / "docker/systemctl").read_text().replace(
                "/run/haproxy/haproxy.pid", str(path / "haproxy.pid"))
            supervisor = path / "supervisorctl"
            supervisor.write_text(
                '#!/bin/sh\ncase "$*" in\n'
                '  *"status haproxy") echo "haproxy RUNNING";;\n'
                '  *"pid haproxy") echo "12345";;\n'
                '  *"restart haproxy") echo "haproxy: restarted";;\n'
                '  *"signal USR2 haproxy") echo "haproxy: signalled";;\n'
                '  *) exit 1;;\nesac\n')
            supervisor.chmod(0o700)
            env = dict(os.environ, PATH=directory + os.pathsep + os.environ["PATH"])
            for master_ready in (False, True):
                if master_ready:
                    (path / "haproxy.pid").write_text("12345\n")
                result = subprocess.run(["sh", "-c", shim, "systemctl", "reload-or-restart", "haproxy"],
                                        env=env, text=True, capture_output=True, check=True)
                self.assertIn("signalled" if master_ready else "restarted", result.stdout)
                if not master_ready:
                    self.assertNotIn("signalled", result.stdout)


if __name__ == "__main__":
    unittest.main()
