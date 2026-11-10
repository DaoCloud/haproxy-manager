#!/usr/bin/env python3
"""Check the workflow's image references for mixed-case repository owners."""
import os
import pathlib
import re
import subprocess
import tempfile
import textwrap
import unittest


WORKFLOW = (pathlib.Path(__file__).resolve().parent.parent
            / ".github/workflows/docker-publish.yml").read_text()
SCRIPTS = [
    textwrap.dedent(match.group(1))
    for match in re.finditer(r"^        run: \|\n((?:          .*\n|\n)+)",
                            WORKFLOW, re.MULTILINE)
]


class ImageReferenceTests(unittest.TestCase):
    def test_startup_checks_use_lowercase_image_names(self):
        setup = "\n".join(script for script in SCRIPTS if "GITHUB_ENV" in script)
        check = next(script for script in SCRIPTS if 'image="${REGISTRY}/' in script)
        selection = check.split('echo "## Image contents"', 1)[0]
        digest = "sha256:" + "a" * 64
        for repository in ("DaoCloud/haproxy-manager", "daocloud/haproxy-manager"):
            for event in ("pull_request", "workflow_dispatch"):
                with self.subTest(repository=repository, event=event):
                    env = dict(os.environ, GITHUB_REPOSITORY=repository)
                    for name, value in re.findall(r"^  ([A-Z_]+): (.+)$",
                                                  WORKFLOW, re.MULTILINE):
                        env[name] = value.replace("${{ github.repository }}", repository)
                    script = (setup + '\nset -a\n. "$GITHUB_ENV"\nset +a\n'
                              + selection + '\nprintf "%s\\n" "$image" "$platforms"\n')
                    for expression, value in {
                        "${{ github.event_name }}": event,
                        "${{ steps.version.outputs.value }}": "2.1.0",
                        "${{ steps.build.outputs.digest }}": digest,
                    }.items():
                        script = script.replace(expression, value)
                    with tempfile.TemporaryDirectory(prefix="ham-image-test-") as tmp:
                        env_file = pathlib.Path(tmp) / "env"
                        env_file.touch()
                        env["GITHUB_ENV"] = str(env_file)
                        result = subprocess.run(
                            ["bash", "-eu", "-c", script], env=env,
                            capture_output=True, text=True, check=True)
                    suffix = ":2.1.0" if event == "pull_request" else "@" + digest
                    platforms = ("linux/amd64" if event == "pull_request"
                                 else "linux/amd64 linux/arm64")
                    self.assertEqual(result.stdout.splitlines(),
                                     ["ghcr.io/daocloud/haproxy-manager" + suffix,
                                      platforms])


if __name__ == "__main__":
    unittest.main()
