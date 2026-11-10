"""Execute the real deployment script with host-changing commands isolated."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


STUB = r'''
import json
import os
from pathlib import Path
import subprocess
import sys

command, args = Path(sys.argv[0]).name, sys.argv[1:]
root = Path(os.environ["TEST_ROOT"])
with (root / "commands.jsonl").open("a") as log:
    log.write(json.dumps([command, *args]) + "\n")
if command == "hostname":
    print(os.environ.get("TEST_HOSTNAME", "proxy-a"))
elif command == "id":
    print(os.environ.get("TEST_UID", "0"))
elif command == "ip":
    print(os.environ["TEST_ADDRESSES"])
elif command == "jq":
    sys.exit(subprocess.call([os.environ["TEST_REAL_JQ"], *args]))
elif command == "install":
    destination = Path(args[-1])
    if str(destination) == "/etc/sysctl.d/90-haproxy-manager.conf":
        args[-1] = str(root / "installed-sysctl.conf")
    elif not destination.resolve().is_relative_to(root):
        sys.exit("refusing a test write outside its temporary directory")
    sys.exit(subprocess.call([os.environ["TEST_REAL_INSTALL"], *args]))
elif command == "sysctl":
    print("net.ipv4.ip_nonlocal_bind = 1")
elif command == "docker":
    if args[0] == "info":
        print(json.dumps(["name=rootless"] if os.environ.get("TEST_ROOTLESS") else []))
    elif args[0] == "ps":
        print(os.environ.get("TEST_CONTAINER", ""), end="")
    elif args[0] == "pull":
        sys.exit(int(os.environ.get("TEST_PULL_STATUS", "0")))
    elif args[0] == "run":
        if os.environ.get("TEST_RUN_FAIL"):
            sys.exit(1)
        print("test-container-id")
    else:
        sys.exit("unexpected Docker operation")
else:
    sys.exit("unexpected command")
'''


class DeploymentFixture:
    def __init__(self, root):
        self.root = Path(root)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.stage = self.root / "staging"
        self.stage.mkdir()
        self.data = self.root / "data"
        for command in ("hostname", "id", "ip", "docker", "jq", "install", "sysctl"):
            path = self.bin / command
            path.write_text("#!" + sys.executable + "\n" + STUB)
            path.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        TEST_ROOT=str(self.root), TEST_REAL_JQ=shutil.which("jq") or "",
                        TEST_REAL_INSTALL=shutil.which("install") or "", TMPDIR=str(self.stage))

    def run(self, args, env=None):
        script = Path(__file__).resolve().parents[1] / "deploy-two-nodes-docker.sh"
        return subprocess.run(["bash", str(script), *args], text=True, capture_output=True,
                              env=self.env | (env or {}))
