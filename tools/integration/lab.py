#!/usr/bin/env python3
"""An isolated, real HAProxy/Keepalived lab, using only Python's standard library."""
import argparse
import hashlib
import http.client
import http.cookiejar
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PASSWORD = "lab-demo-password"
ROOT = Path(__file__).resolve().parent.parent.parent


class LabError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise LabError(message)


def wait_for(description, check, timeout=120):
    deadline = time.monotonic() + timeout
    last = "condition not met"
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except (LabError, OSError, urllib.error.URLError, http.client.HTTPException) as error:
            last = str(error)
        time.sleep(1)
    raise LabError("Timed out waiting for %s: %s" % (description, last))


class Client:
    def __init__(self, address):
        self.base = "http://%s:8080" % address
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, path, body=None, method=None, allow_failure=False):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.base + "/api/" + path, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=120) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            raise LabError("%s %s: HTTP %s: %s" % (
                self.base, path, error.code, error.read().decode())) from error
        if isinstance(result, dict) and not allow_failure:
            require(result.get("ok") is not False, "%s: %s" % (path, result))
            applied = result.get("applied")
            if isinstance(applied, dict):
                require(applied.get("ok") is True, "%s: %s" % (path, applied))
        return result

    def login(self):
        wait_for(self.base + " login", lambda: self.call(
            "login", {"username": "admin", "password": PASSWORD}))
        return self


def clients():
    return {name: Client(os.environ[key]).login()
            for name, key in (("proxy-a", "LAB_A"), ("proxy-b", "LAB_B"))}


def holders(nodes):
    return [name for name, client in nodes.items() if client.call("status").get("vip_held")]


def single_holder(nodes):
    names = holders(nodes)
    require(len(names) == 1, "expected exactly one VIP holder, got %s" % names)
    return names[0]


def agreement(nodes):
    statuses = [client.call("status") for client in nodes.values()]
    versions = {(s["published_rev"], s["published_fp"]) for s in statuses}
    require(len(versions) == 1 and statuses[0]["published_fp"],
            "published versions differ: %s" % sorted(versions))
    for status in statuses:
        require((status["applied_rev"], status["applied_fp"]) ==
                (status["published_rev"], status["published_fp"]) and not status["apply_error"],
                "%s has not applied its publication: %s" % (
                    status["hostname"], status.get("apply_error")))
    require(len(holders(nodes)) == 1, "expected exactly one VIP holder")
    return statuses


def publish(client, name, port, apply=False):
    return client.call("wizard/publish", {
        "name": name, "url": "tcp://0.0.0.0:%d" % port,
        "target": "tcp://%s:%d" % (
            os.environ["LAB_BACKEND"], 8443 if port == 443 else 8080),
        "certificate": False, "health": {"type": "tcp"}, "apply": apply,
    })


def http_body(address, port=80, host=None):
    connection = http.client.HTTPConnection(address, port, timeout=3)
    try:
        connection.request("GET", "/", headers={"Host": host} if host else {})
        response = connection.getresponse()
        require(response.status == 200, "HTTP status %d" % response.status)
        result = json.loads(response.read())
        require(result == {"service": "lab-backend", "protocol": "http"},
                "unexpected backend response: %s" % result)
        return True
    finally:
        connection.close()


def absent(address, port):
    try:
        with socket.create_connection((address, port), timeout=1):
            return False
    except ConnectionRefusedError:
        return True


def check_tls():
    backend = os.environ["LAB_BACKEND"]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open("http://%s:8080/certificate" % backend, timeout=5) as response:
        certificate = response.read().decode()
    context = ssl.create_default_context(cadata=certificate)
    expected = ssl.PEM_cert_to_DER_cert(certificate)
    for address, port in ((backend, 8443), (os.environ["LAB_VIP"], 443)):
        with socket.create_connection((address, port), timeout=3) as raw:
            with context.wrap_socket(raw, server_hostname="backend.lab.test") as connection:
                require(connection.getpeercert(binary_form=True) == expected,
                        "TLS certificate differs from the backend")
                connection.sendall(b"GET / HTTP/1.1\r\nHost: backend.lab.test\r\nConnection: close\r\n\r\n")
                response = http.client.HTTPResponse(connection)
                response.begin()
                require(response.status == 200 and json.loads(response.read()) ==
                        {"service": "lab-backend", "protocol": "https"},
                        "HTTPS did not reach the TLS backend")
    return True


def check():
    nodes = clients()
    wait_for("matching applied versions and a single VIP", lambda: agreement(nodes))
    for address in (os.environ["LAB_A"], os.environ["LAB_B"], os.environ["LAB_VIP"]):
        wait_for("HTTP through " + address, lambda: http_body(address))
    wait_for("backend certificate and HTTPS through VIP", check_tls)
    print("PASS: both nodes applied the same version; VIP HTTP and TLS passthrough work.", flush=True)


def initialize():
    nodes = clients()
    created = False
    a, b = nodes["proxy-a"], nodes["proxy-b"]
    if not a.call("setup/state")["complete"]:
        publish(a, "lab-http", 80)
        publish(a, "lab-https", 443)
        a.call("setup/create", {
            "mode": "cluster", "vips": os.environ["LAB_VIP"] + "/24",
            "vrid": 77, "interface": "eth0", "priority": 150,
            "node_url": a.base, "api_key": os.environ["LAB_PROJECT"] + "-a-lab-only",
            "auth_pass": "ham-lab", "apply": True,
        })
        wait_for("proxy-a to hold the initial VIP", lambda: a.call("status")["vip_held"])
        created = True
    if not b.call("setup/state")["complete"]:
        joined = b.call("setup/join", {
            "peer_url": a.base, "peer_api_key": os.environ["LAB_PROJECT"] + "-a-lab-only",
            "node_url": b.base, "api_key": os.environ["LAB_PROJECT"] + "-b-lab-only",
            "interface": "eth0", "priority": 100,
        })
        require(joined.get("synced"), "join did not apply the configuration: %s" % joined)
        a.call("apply", {})
        created = True
    wait_for("cluster readiness", lambda: agreement(nodes))
    if created:
        check()
    print("Cluster ready. Existing configuration was preserved.", flush=True)


def delete_service(client, port):
    services = client.call("services")
    service = next((s for s in services
                    if s["scheme"] == "tcp" and s["port"] == port), None)
    require(service is not None, "TCP service on port %d not found" % port)
    client.call("services/" + urllib.parse.quote(service["id"], safe=""), method="DELETE")


def delete_probe(client):
    delete_service(client, 18090)


def test_reload_conflict():
    nodes = clients()
    active = nodes[wait_for("a single VIP holder", lambda: single_holder(nodes))]
    before = [(c.call("status")["applied_rev"], c.call("status")["published_rev"])
              for c in nodes.values()]
    # The manager already owns 8080. haproxy -c accepts this, but a real
    # reload must fail while the old worker keeps answering on 80 and 443.
    publish(active, "lab-reload-conflict", 8080)
    result = active.call("apply", {}, allow_failure=True)
    require(result.get("local_ok") is False and result.get("ok") is False,
            "a rejected reload was reported as applied: %s" % result)
    after = [(c.call("status")["applied_rev"], c.call("status")["published_rev"])
             for c in nodes.values()]
    require(before == after, "a failed reload advanced an applied or published version")
    for address in (os.environ["LAB_A"], os.environ["LAB_B"], os.environ["LAB_VIP"]):
        wait_for("old configuration remains available", lambda: http_body(address))
    delete_service(active, 8080)
    publish(active, "lab-reload-probe", 18091)
    active.call("apply", {})
    wait_for("corrected draft publication", lambda: agreement(nodes))
    for address in (os.environ["LAB_A"], os.environ["LAB_B"]):
        wait_for("corrected draft forwarding", lambda: http_body(address, 18091))
    delete_service(active, 18091)
    active.call("apply", {})
    wait_for("reload probe cleanup", lambda: agreement(nodes))
    print("PASS: failed reload keeps the old publication; a corrected draft applies.", flush=True)


def test_local_ui():
    nodes = clients()
    active = nodes[wait_for("a single VIP holder", lambda: single_holder(nodes))]
    delete_service(active, 80)
    active.call("apply", {})
    wait_for("TCP port 80 removal", lambda: agreement(nodes))
    revision = active.call("status")["published_rev"]
    for name, client in nodes.items():
        host = name + ".example.test"
        client.call("webui", {"enabled": True, "url": "http://" + host,
                             "certificate": "none", "http_redirect": False, "apply": False})
        require(client.call("status")["dirty"], "a saved local UI was not marked dirty")
        client.call("apply", {})
        require(not client.call("status")["dirty"], "an applied local UI stayed dirty")
        require(client.call("status")["published_rev"] == revision,
                "a local UI change advanced the shared revision")
    active.call("wizard/publish", {
        "name": "lab-wildcard", "url": "http://*.example.test",
        "target": "http://%s:8080" % os.environ["LAB_BACKEND"],
        "certificate": False, "http_redirect": False, "apply": True,
    })
    wait_for("shared wildcard publication", lambda: agreement(nodes))
    for name, client in nodes.items():
        address = urllib.parse.urlsplit(client.base).hostname
        connection = http.client.HTTPConnection(address, 80, timeout=5)
        try:
            connection.request("GET", "/api/whoami", headers={"Host": name + ".example.test"})
            response = connection.getresponse()
            body = json.loads(response.read())
            require(response.status == 200 and body.get("authenticated") is False,
                    "the shared wildcard shadowed %s's local UI: %s" % (name, body))
        finally:
            connection.close()
        wait_for("wildcard business traffic", lambda: http_body(
            address, host="app.example.test"))
    print("PASS: local UI changes are dirty until applied; wildcard traffic cannot shadow them.",
          flush=True)


def test_draft():
    nodes = clients()
    active = nodes[wait_for("a single VIP holder", lambda: single_holder(nodes))]
    before = [(c.call("status")["applied_rev"], c.call("status")["applied_fp"])
              for c in nodes.values()]
    publish(active, "lab-probe", 18090)
    # Allow two default 20-second watchdog rounds to reconcile the saved draft.
    time.sleep(45)
    after = [(c.call("status")["applied_rev"], c.call("status")["applied_fp"])
             for c in nodes.values()]
    require(before == after, "a draft changed an applied version")
    for address in (os.environ["LAB_A"], os.environ["LAB_B"]):
        require(absent(address, 18090), "an unapplied draft started forwarding")
    active.call("apply", {})
    wait_for("probe publication", lambda: agreement(nodes))
    for address in (os.environ["LAB_A"], os.environ["LAB_B"]):
        wait_for("probe forwarding", lambda: http_body(address, 18090))
    delete_probe(active)
    active.call("apply", {})
    wait_for("probe deletion publication", lambda: agreement(nodes))
    for address in (os.environ["LAB_A"], os.environ["LAB_B"]):
        wait_for("probe listener removal", lambda: absent(address, 18090))
    print("PASS: drafts stay inactive; Apply and deletion reach both real proxies.", flush=True)


def internal(action, node=None):
    if action == "_init":
        initialize()
    elif action == "_check":
        check()
    elif action == "_draft":
        test_draft()
    elif action == "_reload":
        test_reload_conflict()
    elif action == "_local_ui":
        test_local_ui()
    elif action == "_holder":
        nodes = clients()
        print(wait_for("one VIP holder", lambda: single_holder(nodes)))
    elif action == "_offline":
        survivor = "proxy-b" if node == "proxy-a" else "proxy-a"
        client = Client(os.environ["LAB_B" if survivor == "proxy-b" else "LAB_A"]).login()
        wait_for("VIP failover", lambda: client.call("status")["vip_held"])
        wait_for("HTTP after failover", lambda: http_body(os.environ["LAB_VIP"]))
        wait_for("TLS after failover", check_tls)
        publish(client, "lab-probe", 18090)
        result = client.call("apply", {}, allow_failure=True)
        require(result.get("local_ok") is True and result.get("ok") is False
                and result.get("cluster_ok") is False,
                "offline peer was not reported as partial success: %s" % result)
        print("PASS: VIP failed over; offline peer is reported without false success.", flush=True)
    elif action == "_recover":
        nodes = clients()
        wait_for("recovered node to catch up", lambda: agreement(nodes))
        for address in (os.environ["LAB_A"], os.environ["LAB_B"]):
            wait_for("recovered rule", lambda: http_body(address, 18090))
        active = nodes[wait_for("one VIP holder", lambda: single_holder(nodes))]
        delete_probe(active)
        active.call("apply", {})
        check()
        print("PASS: recovered node applied the offline update.", flush=True)


class Lab:
    def __init__(self, testing=False):
        base = "ham-lab-" + hashlib.sha256(str(ROOT).encode()).hexdigest()[:8]
        self.project = base + ("-test-" + secrets.token_hex(3) if testing else "")
        self.env = dict(os.environ, LAB_IMAGE=base + ":local",
                        LAB_BACKEND_IMAGE=base + "-backend:local", LAB_PROJECT=self.project,
                        LAB_UI_BIND="127.0.0.1" if testing else os.environ.get(
                            "HAM_LAB_UI_BIND", "127.0.0.1"),
                        LAB_UI_A_PORT="0" if testing else os.environ.get("HAM_LAB_UI_A_PORT", "18081"),
                        LAB_UI_B_PORT="0" if testing else os.environ.get("HAM_LAB_UI_B_PORT", "18082"))
        network = self.command(["docker", "network", "ls", "-q",
                                "--filter", "name=^%s_lab$" % self.project], capture=True).strip()
        if network:
            info = json.loads(self.command(["docker", "network", "inspect", network], capture=True))[0]
            require(info.get("Labels", {}).get("com.docker.compose.project") == self.project,
                    "refusing to use a network not owned by this lab")
            subnet = ipaddress.ip_network(info["IPAM"]["Config"][0]["Subnet"])
        else:
            subnet = self.free_subnet()
        require(subnet.version == 4 and subnet.prefixlen == 24, "the lab needs an IPv4 /24 subnet")
        self.env.update(LAB_SUBNET=str(subnet), LAB_A=str(subnet[11]), LAB_B=str(subnet[12]),
                        LAB_BACKEND=str(subnet[20]), LAB_VIP=str(subnet[100]))

    def command(self, args, capture=False):
        result = subprocess.run(args, cwd=ROOT, env=self.env, check=True, text=True,
                                stdout=subprocess.PIPE if capture else None)
        return result.stdout if capture else None

    def compose(self, *args, capture=False):
        return self.command(["docker", "compose", "-p", self.project, "-f",
                             str(ROOT / "tools/integration/compose.yml"), *args], capture)

    def free_subnet(self):
        used = []
        networks = self.command(["docker", "network", "ls", "-q"], capture=True).split()
        if networks:
            for network in json.loads(self.command(
                    ["docker", "network", "inspect", *networks], capture=True)):
                used.extend(ipaddress.ip_network(c["Subnet"])
                            for c in network["IPAM"]["Config"] or [] if c.get("Subnet"))
        for route in json.loads(self.command(["ip", "-j", "route", "show"], capture=True)):
            if route.get("dst", "default") != "default":
                used.append(ipaddress.ip_network(route["dst"], strict=False))
        candidates = ([ipaddress.ip_network(os.environ["HAM_LAB_SUBNET"])]
                      if os.environ.get("HAM_LAB_SUBNET") else
                      [ipaddress.ip_network("172.30.%d.0/24" % n) for n in range(77, 255)])
        subnet = next((c for c in candidates if not any(
            u.version == c.version and u.overlaps(c) for u in used)), None)
        require(subnet is not None, "no free lab subnet; set HAM_LAB_SUBNET to an unused IPv4 /24")
        return subnet

    def phase(self, action, node=None, capture=False):
        args = ["exec", "-T", "backend", "python3", "/lab/lab.py", action]
        if node:
            args.append(node)
        return self.compose(*args, capture=capture)

    def up(self):
        self.compose("build", "proxy-a")
        self.compose("build", "backend")
        self.compose("up", "-d", "--no-build")
        self.phase("_init")

    def show(self):
        print("\nUI A: http://127.0.0.1:%s\nUI B: http://127.0.0.1:%s\n"
              "UI bind address: %s\n"
              "Login: admin / %s (lab only)\nVIP HTTP: http://%s/\n"
              "VIP HTTPS: https://%s/ (self-signed backend certificate)\n"
              "Backend targets: tcp://%s:8080 (HTTP), tcp://%s:8443 (HTTPS)\n"
              "Stop a node: python3 tools/integration/lab.py stop proxy-a\n"
              "Restart it: python3 tools/integration/lab.py start proxy-a\n"
              "Check: python3 tools/integration/lab.py check\n"
              "Delete this lab and its data: python3 tools/integration/lab.py down"
              % (self.env["LAB_UI_A_PORT"], self.env["LAB_UI_B_PORT"],
                 self.env["LAB_UI_BIND"], PASSWORD,
                 self.env["LAB_VIP"], self.env["LAB_VIP"],
                 self.env["LAB_BACKEND"], self.env["LAB_BACKEND"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("up", "check", "test", "down", "status",
                                         "stop", "start", "_init", "_check", "_draft",
                                         "_holder", "_offline", "_recover", "_reload",
                                         "_local_ui"))
    parser.add_argument("node", nargs="?", choices=("proxy-a", "proxy-b"))
    args = parser.parse_args()
    if args.action.startswith("_"):
        internal(args.action, args.node)
        return
    require(sys.platform.startswith("linux"), "the lab requires Linux")
    for executable in ("docker", "ip"):
        require(shutil.which(executable), "%s is required" % executable)
    subprocess.run(["docker", "compose", "version"], check=True, stdout=subprocess.DEVNULL)
    lab = Lab(testing=args.action == "test")
    if args.action == "up":
        lab.up()
        lab.show()
    elif args.action == "test":
        try:
            lab.up()
            lab.phase("_reload")
            lab.phase("_draft")
            active = lab.phase("_holder", capture=True).strip()
            require(active in ("proxy-a", "proxy-b"), "invalid active node: %s" % active)
            lab.compose("stop", active)
            lab.phase("_offline", active)
            lab.compose("start", active)
            lab.phase("_recover")
            lab.phase("_local_ui")
            print("PASS: real dual-node integration tests completed.", flush=True)
        except (LabError, subprocess.CalledProcessError):
            lab.compose("logs", "--tail", "80")
            raise
        finally:
            lab.compose("down", "--volumes")
    elif args.action == "check":
        lab.phase("_check")
    elif args.action == "down":
        lab.compose("down", "--volumes")
    elif args.action == "status":
        lab.compose("ps")
        lab.show()
    else:
        require(args.node is not None, "start/stop requires proxy-a or proxy-b")
        lab.compose(args.action, args.node)


if __name__ == "__main__":
    try:
        main()
    except (LabError, OSError, subprocess.CalledProcessError) as error:
        print("ERROR: %s" % error, file=sys.stderr)
        sys.exit(1)
