"""Settings validation."""

import re
import ipaddress
from urllib.parse import urlsplit


#
# `haproxy -c` is the authority on whether a configuration works, but it is
# lenient about types: `maxconn not-a-number` parses as zero and validates
# clean, silently capping the proxy at nothing. So the obviously-typed fields
# are checked here first, before anything is written.
# --------------------------------------------------------------------------

NUMERIC_SETTINGS = {
    "haproxy": {"maxconn": (1, 2000000), "nbthread": (1, 256), "retries": (0, 100)},
    "acme": {"challenge_port": (1, 65535), "renew_hours": (1, 8760)},
}
TIME_SETTINGS = {"haproxy": ["timeout_client", "timeout_connect", "timeout_server",
                             "hard_stop_after", "timeout_tunnel"]}
DEFAULT_SITES = ("503", "404", "drop", "redirect")


def plain_url(value):
    """An http(s) address with nothing in it that could end a directive:
    no space, no quote."""
    value = str(value or "").strip()
    return bool(re.match(r"^https?://\S+$", value)) and not any(q in value for q in "\"'")


_plain_url = plain_url
TIME_RE = re.compile(r"^\d+(us|ms|s|m|h|d)?$")


def check_setting_types(sec, proposed):
    """Return a human-readable complaint, or "" when the values make sense."""
    problems = []
    for key, (lo, hi) in NUMERIC_SETTINGS.get(sec, {}).items():
        if key not in proposed:
            continue
        raw = proposed[key]
        if raw in ("", None):                  # empty means "use the default"
            continue
        try:
            val = int(str(raw).strip())
        except (TypeError, ValueError):
            problems.append("%s must be a whole number, not %r." % (key, raw))
            continue
        if not lo <= val <= hi:
            problems.append("%s must be between %d and %d." % (key, lo, hi))
    if sec == "haproxy" and "default_site" in proposed:
        if proposed["default_site"] not in DEFAULT_SITES:
            problems.append("default_site must be one of %s." % ", ".join(DEFAULT_SITES))
        elif proposed["default_site"] == "redirect" and \
                not _plain_url(str(proposed.get("default_site_url") or "").strip()):
            problems.append("a redirect for unknown hosts needs an address starting with http:// or https://.")
    for key in TIME_SETTINGS.get(sec, []):
        raw = proposed.get(key)
        if raw in ("", None):
            continue
        if not TIME_RE.match(str(raw).strip()):
            problems.append("%s must be a time such as 50s, 5000 or 1m -- not %r." % (key, raw))
    return "\n".join(problems)


# --------------------------------------------------------------------------
# generic CRUD


# --------------------------------------------------------------------------
# Backend Pools

def check_pool(item):
    """Refuse a pool whose rate limit would not render, before it is stored.

    Raises ValueError with the reason. The renderer treats anything that is
    not a positive whole number as no limit, so a typo accepted here would
    save as a limit and serve as none -- the one outcome worse than an error.
    """
    rate, window = item.get("rate_limit"), item.get("rate_window")
    if rate not in ("", None):
        try:
            r = int(str(rate).strip())
        except (TypeError, ValueError):
            raise ValueError("the rate limit must be a whole number of requests per client")
        if r < 1:
            raise ValueError("the rate limit must be at least 1, or empty for no limit")
    if window not in ("", None):
        try:
            w = int(str(window).strip())
        except (TypeError, ValueError):
            raise ValueError("the rate window must be a whole number of seconds")
        if not 1 <= w <= 3600:
            raise ValueError("the rate window must be between 1 and 3600 seconds")


def check_file_config(cfg):
    """Validate identities and references that a file edit can bypass."""
    from .config import VALID_COLLECTIONS, merged

    if cfg["_meta"].get("unreadable"):
        raise ValueError("config.json is unreadable: %s" %
                         cfg["_meta"].get("config_error", "repair the file on the host"))
    for section in ("haproxy", "acme"):
        error = check_setting_types(section, cfg[section]["settings"])
        if error:
            raise ValueError("%s.settings: %s" % (section, error))
        for collection in VALID_COLLECTIONS[section]:
            seen = set()
            for body in (cfg[section], cfg["local"].get(section, {})):
                items = body.get(collection, [])
                if not isinstance(items, list):
                    raise ValueError("%s.%s must be an array" % (section, collection))
                for item in items:
                    where = "%s.%s" % (section, collection)
                    if not isinstance(item, dict):
                        raise ValueError("%s entries must be objects" % where)
                    if not isinstance(item.get("id"), str) or not item["id"]:
                        raise ValueError("%s entries need a non-empty id" % where)
                    if item["id"] in seen:
                        raise ValueError("%s contains duplicate id %s" % (where, item["id"]))
                    seen.add(item["id"])
                    if not isinstance(item.get("name"), str) or not item["name"]:
                        raise ValueError("%s entries need a non-empty name" % where)
    view = merged(cfg)
    hp = view["haproxy"]
    ids = {key: {item["id"] for item in hp[key]} for key in VALID_COLLECTIONS["haproxy"]}
    ids["certificates"] = {item["id"] for item in view["acme"]["certificates"]}

    def references(item, key, target, many=False):
        value = item.get(key)
        if value in (None, ""):
            return
        values = value if many else [value]
        if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
            raise ValueError("%s.%s has invalid references" % (item["id"], key))
        for value in values:
            if value not in ids[target]:
                raise ValueError("%s.%s references missing %s id %s" %
                                 (item["id"], key, target, value))

    for server in hp["servers"]:
        if not isinstance(server.get("address"), str) or not server["address"].strip():
            raise ValueError("%s needs a server address" % server["id"])
        try:
            port = int(server.get("port", 80))
        except (ValueError, TypeError):
            port = 0
        if not 1 <= port <= 65535:
            raise ValueError("%s needs a port between 1 and 65535" % server["id"])
    for pool in hp["backends"]:
        check_pool(pool)
        references(pool, "servers", "servers", many=True)
        if pool.get("healthcheck_enabled"):
            references(pool, "healthcheck", "healthchecks")
    for frontend in hp["frontends"]:
        if not isinstance(frontend.get("binds"), str) or not frontend["binds"].strip():
            raise ValueError("%s needs listener binds" % frontend["id"])
        references(frontend, "default_backend", "backends")
        references(frontend, "rules", "rules", many=True)
        references(frontend, "certificates", "certificates", many=True)
    for rule in hp["rules"]:
        references(rule, "backend", "backends")
        references(rule, "conditions", "conditions", many=True)
        references(rule, "any_of", "conditions", many=True)
    for condition in hp["conditions"]:
        if condition.get("type") == "source_list":
            references(condition, "list", "addresslists")

    vips = cfg["cluster"]["vips"]
    if not isinstance(vips, str):
        raise ValueError("cluster.vips must be a string of address/prefix lines")
    for vip in vips.splitlines():
        if vip.strip():
            try:
                ipaddress.ip_interface(vip.strip())
            except ValueError:
                raise ValueError("cluster.vips contains an invalid address/prefix") from None
    for value, name in ((cfg["cluster"]["vrid"], "cluster.vrid"),
                        (cfg["local"]["keepalived"]["priority"], "local.keepalived.priority")):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 255:
            raise ValueError("%s must be an integer between 1 and 255" % name)
    for key in ("interface", "unicast_src", "unicast_peer"):
        if not isinstance(cfg["local"]["keepalived"][key], str):
            raise ValueError("local.keepalived.%s must be a string" % key)
    for key in ("unicast_src", "unicast_peer"):
        addresses = cfg["local"]["keepalived"][key].replace(",", " ").split()
        if key == "unicast_src" and len(addresses) > 1:
            raise ValueError("local.keepalived.unicast_src needs a single IPv4 address")
        for address in addresses:
            try:
                ipaddress.IPv4Address(address)
            except ValueError:
                raise ValueError("local.keepalived.%s needs IPv4 addresses" % key) from None
    for key in ("api_key", "node_url"):
        if not isinstance(cfg["local"][key], str):
            raise ValueError("local.%s must be a string" % key)

    def node_url(value):
        if not isinstance(value, str) or not plain_url(value):
            return False
        parsed = urlsplit(value)
        vip_addresses = {vip.split("/")[0].strip() for vip in vips.splitlines()}
        return (bool(parsed.hostname) and not parsed.username and not parsed.password
                and parsed.hostname not in vip_addresses)

    peers = cfg["local"]["sync"]["peers"]
    urls = set()
    for peer in peers:
        if not isinstance(peer, dict):
            raise ValueError("local.sync.peers entries must be objects")
        if not peer.get("enabled", True):
            continue
        if not node_url(peer.get("url")):
            raise ValueError("each enabled peer needs an http(s) URL without embedded credentials")
        url = peer["url"].rstrip("/").lower()
        if url in urls or url == cfg["local"]["node_url"].rstrip("/").lower():
            raise ValueError("peer URLs must be unique and must not point to this node")
        urls.add(url)
        if not isinstance(peer.get("api_key"), str) or not peer["api_key"].strip():
            raise ValueError("each enabled peer needs an API key")
    if urls and (not node_url(cfg["local"]["node_url"]) or not cfg["local"]["api_key"].strip()):
        raise ValueError("a clustered node needs its own node_url and API key")
