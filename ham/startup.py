"""One-shot container startup application, with a waiting first-join phase."""

from datetime import datetime, timezone
import os
import time

from .base import _lock, _requests, app, log, PEER_TIMEOUT
from .config import CONF_PATH, config_hash, load_config, save_config, shared_fingerprint
from .validate import check_file_config
from . import apply, sync, watchdog

ENABLED = os.environ.get("HAM_APPLY_ON_START", "0") == "1"
_status = {"enabled": ENABLED, "state": "pending" if ENABLED else "disabled",
           "local_ok": False, "error": "", "attempts": 0}
BUSY = ("pending", "checking", "applying")
_join_baseline = None


class StartupConflict(ValueError):
    pass


class PublicationChanged(RuntimeError):
    pass


def status():
    with _lock:
        return dict(_status)


def _set(state, message, **values):
    with _lock:
        changed = _status.get("state") != state or _status.get("message") != message
        _status.update(state=state, message=message,
                       updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       **values)
    if changed:
        level = log.error if state in ("failed", "conflict") else log.info
        level("startup: state=%s %s", state, message)


def busy():
    return ENABLED and _status["state"] in BUSY


def blocks_sync():
    return ENABLED and _status["state"] not in ("ready", "idle", "disabled")


def receive_error(force=False, cfg=None):
    if not ENABLED:
        return ""
    if _status["state"] in BUSY:
        return "Startup configuration arbitration is in progress; retry after it completes."
    if not force and _status["state"] in ("failed", "conflict"):
        return "Startup is blocked; repair the local configuration or explicitly choose Overwrite."
    if not force and cfg is not None and _join_pending(cfg) and not waiting(cfg):
        return "Local shared configuration changed before the first publication; " \
               "explicitly Apply or choose Overwrite after reviewing the draft."
    return ""


def _join_pending(cfg):
    return (ENABLED and _status["state"] in ("waiting_peer", "idle")
            and _join_baseline is not None and not cfg["_meta"].get("published"))


def waiting(cfg):
    """An untouched first join, including nodes waiting for a push without polling."""
    return _join_pending(cfg) and shared_fingerprint(cfg) == _join_baseline


def applied(cfg):
    """A successful explicit Apply or first receive can finish a waiting startup."""
    if not ENABLED:
        return
    if _status["state"] not in BUSY:
        _set("ready", "configuration applied at revision %s (%s)" %
             (cfg["_meta"].get("applied_rev"), cfg["_meta"].get("applied_fp")),
             local_ok=True, error="",
             applied_rev=cfg["_meta"].get("applied_rev"),
             applied_fp=cfg["_meta"].get("applied_fp"))


def _signature(cfg):
    return config_hash(cfg), shared_fingerprint(cfg)


def _publications(cfg):
    publications, unavailable = [], []
    peers = sync.enabled_peers(cfg)
    if peers and _requests is None:
        raise ValueError("peer synchronization requires python3-requests")
    for peer in peers:
        url = peer["url"].rstrip("/")
        try:
            response = _requests.get(
                url + "/api/status?brief=1", headers={"X-API-Key": peer["api_key"]},
                timeout=PEER_TIMEOUT, verify=bool(peer.get("verify_tls")))
            if response.status_code != 200:
                unavailable.append("%s (HTTP %s)" % (url, response.status_code))
                continue
            body = response.json()
            rev = int(body.get("published_rev") or 0)
            fp = body.get("published_fp")
            if rev and fp:
                publications.append(dict(peer=peer, rev=rev, fp=fp))
        except (ValueError, TypeError, AttributeError) as error:
            log.warning("startup: invalid peer status from %s (%s)", url, type(error).__name__)
            unavailable.append(url)
        except _requests.exceptions.RequestException:
            unavailable.append(url)
    return publications, unavailable


def _decision(cfg, publications, unavailable):
    published = cfg["_meta"].get("published")
    fp = shared_fingerprint(cfg)
    seeded = bool(cfg["cluster"]["vips"].strip() or
                  any(cfg["haproxy"][key] for key in ("servers", "backends", "frontends", "rules")))
    if not publications:
        if published and fp != published["fp"] and unavailable:
            raise StartupConflict("Unpublished local changes cannot be checked against offline peers. "
                                  "Restore connectivity or explicitly Apply the chosen configuration.")
        return ("local" if published or seeded else "wait"), None
    highest = max(p["rev"] for p in publications)
    newest = [p for p in publications if p["rev"] == highest]
    if len({p["fp"] for p in newest}) != 1:
        raise StartupConflict("Peers report conflicting publications at revision %s." % highest)
    best = newest[0]
    if fp == best["fp"]:
        if published and published["rev"] > highest:
            return "local", None
        return "receive", best
    if not published:
        if seeded:
            raise StartupConflict("The initial shared configuration differs from the existing cluster. "
                                  "Join with local-only configuration or explicitly choose the correct state.")
        return "receive", best
    dirty = fp != published["fp"]
    if dirty:
        if unavailable or highest > published["rev"] or (
                highest == published["rev"] and best["fp"] != published["fp"]):
            raise StartupConflict("Local unpublished changes conflict with the peer publication. "
                                  "Nothing was overwritten; explicitly choose the correct configuration.")
        return "local", None
    if highest == published["rev"] and best["fp"] != published["fp"]:
        raise StartupConflict("Conflicting publications at revision %s; explicitly choose the correct node."
                              % highest)
    return ("receive", best) if highest > published["rev"] else ("local", None)


def _receive(cfg, publication):
    peer = publication["peer"]
    response = _requests.get(
        peer["url"].rstrip("/") + "/api/sync/pull",
        headers={"X-API-Key": peer["api_key"]}, timeout=PEER_TIMEOUT,
        verify=bool(peer.get("verify_tls")))
    response.raise_for_status()
    payload = response.json()
    if payload.get("rev") != publication["rev"] or payload.get("fp") != publication["fp"]:
        raise PublicationChanged("The peer publication changed during startup; retry startup.")
    with app.test_request_context("/api/sync/receive"):
        with _lock:
            current = load_config()
            if _signature(current) != _signature(cfg):
                raise StartupConflict("The local configuration changed during startup; retry startup.")
            result = sync._receive_locked(current, payload, payload.get("config") or {},
                                          source=peer["url"], startup_apply=True)
        body = result[0] if isinstance(result, tuple) else result
        return body.get_json()


def attempt(expected=None):
    """Perform one serialized arbitration attempt; only first joins keep waiting."""
    global _join_baseline
    with _lock:
        if _status["state"] == "ready":
            return
        cfg = load_config()
        if expected is not None and _signature(cfg) != expected:
            raise StartupConflict("The local configuration changed while waiting for a peer; "
                                  "explicitly Apply or restart after reviewing the changes.")
        check_file_config(cfg)
        save_config(cfg)
        fingerprint = _signature(cfg)
        if expected is None:
            log.info("startup: config=%s shared_rev=%s shared_fp=%s published_rev=%s peers=%d",
                     CONF_PATH, cfg["_meta"].get("shared_rev"), fingerprint[1],
                     (cfg["_meta"].get("published") or {}).get("rev", 0),
                     len(sync.enabled_peers(cfg)))
    auto_sync = cfg["local"]["sync"].get("auto_sync")
    publications, unavailable = _publications(cfg) if auto_sync else ([], [])
    with _lock:
        if _status["state"] == "ready":
            return
        current = load_config()
        if _signature(current) != fingerprint:
            raise StartupConflict("The local configuration changed during startup; retry startup.")
        action, publication = _decision(cfg, publications, unavailable)
        if not cfg["_meta"].get("published") and _join_baseline is None:
            _join_baseline = shared_fingerprint(cfg)
        if action == "wait":
            if not sync.enabled_peers(cfg) or not auto_sync:
                _set("idle", "no preseeded services or automatic join; use the existing UI/API setup",
                     error="")
                return
            _set("waiting_peer", "waiting for a peer's published configuration",
                 error="", unavailable_peers=unavailable)
            return fingerprint
        log.info("startup: selected %s revision=%s fingerprint=%s; unavailable peers=%s",
                 publication["peer"]["url"] if publication else "local config",
                 publication["rev"] if publication else cfg["_meta"].get("shared_rev"),
                 publication["fp"] if publication else fingerprint[1],
                 ", ".join(unavailable) or "none")
        _set("applying", "applying %s configuration" % ("peer" if publication else "local"),
             error="", unavailable_peers=unavailable)
    if action == "receive":
        try:
            result = _receive(current, publication)
        except (PublicationChanged, _requests.exceptions.RequestException) as error:
            if current["_meta"].get("published"):
                raise
            log.warning("startup: fetching the first publication failed (%s); retrying",
                        type(error).__name__)
            _set("waiting_peer", "retrying the first peer publication",
                 error=type(error).__name__)
            return fingerprint
        ok = result.get("ok")
    else:
        with _lock:
            current = load_config()
            if _signature(current) != fingerprint:
                raise StartupConflict("The local configuration changed during startup; retry startup.")
            published = current["_meta"].get("published")
            if published and shared_fingerprint(current) == published["fp"]:
                if current["_meta"].get("shared_rev") != published["rev"]:
                    log.info("startup: restoring published revision %s, not unpublished counter %s",
                             published["rev"], current["_meta"].get("shared_rev"))
                current["_meta"].update(shared_rev=published["rev"], shared_fp=published["fp"])
            result = apply.do_apply(current, allow_push=False, startup_apply=True)
        ok = result.get("local_ok")
    if not ok:
        raise RuntimeError(result.get("error") or "Startup Apply did not succeed")
    current = load_config()
    _set("ready", "local configuration applied; normal cluster synchronization is enabled",
         local_ok=True, error="", applied_rev=current["_meta"].get("applied_rev"),
         applied_fp=current["_meta"].get("applied_fp"))


def run():
    global _join_baseline
    if not ENABLED:
        return
    with _lock:
        _join_baseline = None
        _set("checking", "loading preseeded configuration", local_ok=False, error="", attempts=0,
             started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    # Keep the API reachable while checking peers, and while operators repair failures.
    for _ in range(60):
        if watchdog.probe_self()[0]:
            break
        time.sleep(1)
    else:
        _set("failed", "management listener did not become available", error="listener timeout")
        return
    expected = None
    while True:
        with _lock:
            if _status["state"] == "ready":
                return
            _status["attempts"] += 1
        try:
            expected = attempt(expected)
        except StartupConflict as error:
            with _lock:
                if _status["state"] != "ready":
                    _set("conflict", str(error), error=str(error), local_ok=False)
            return
        except ValueError as error:
            with _lock:
                if _status["state"] != "ready":
                    _set("failed", str(error), error=str(error), local_ok=False)
            return
        except Exception as error:
            with _lock:
                if _status["state"] != "ready":
                    log.exception("startup: initialization failed")
                    _set("failed", str(error), error=str(error), local_ok=False)
            return
        if _status["state"] != "waiting_peer":
            return
        if _status["attempts"] % 12 == 0:
            log.info("startup: still waiting for a peer publication after %d attempts",
                     _status["attempts"])
        time.sleep(5)
