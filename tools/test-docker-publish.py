#!/usr/bin/env python3
"""Check image references, Git tag versions and the latest publishing policy."""
import ast
import fnmatch
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
    def test_git_events_supply_versions_and_latest_policy(self):
        script = next(script for script in SCRIPTS if "GITHUB_OUTPUT" in script)
        root = pathlib.Path(__file__).resolve().parent.parent
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        for event, ref, version, latest in (
                ("push", "refs/tags/V1.0.0", "V1.0.0", "true"),
                ("push", "refs/tags/v1.0.0-beta.1", "v1.0.0-beta.1", "false"),
                ("push", "refs/tags/release-1.0.0", "release-1.0.0", "true"),
                ("push", "refs/tags/release-1.0.0-beta.1", "release-1.0.0-beta.1", "false"),
                ("push", "refs/heads/main", "sha-" + sha[:7], "true"),
                ("push", "refs/heads/feature", "sha-" + sha[:7], "false"),
                ("workflow_dispatch", "refs/tags/V1.0.0-beta.1", "sha-" + sha[:7], "true"),
                ("pull_request", "refs/pull/42/merge", "sha-" + sha[:7], "false")):
            with self.subTest(event=event, ref=ref), tempfile.TemporaryDirectory(
                    prefix="ham-git-version-") as directory:
                output = pathlib.Path(directory) / "output"
                env = dict(os.environ, GITHUB_EVENT_NAME=event, GITHUB_REF=ref,
                           DEFAULT_BRANCH="main", GITHUB_OUTPUT=str(output),
                           PYTHONDONTWRITEBYTECODE="1")
                subprocess.run(["bash", "-eu", "-c", script], cwd=root, env=env,
                               check=True, capture_output=True, text=True)
                values = dict(line.split("=", 1) for line in output.read_text().splitlines())
                self.assertEqual(values["value"], version)
                self.assertEqual(values["latest"], latest)
                self.assertEqual(values["revision"], sha)
                self.assertEqual(values["sha"], sha[:7])

    def test_git_tags_are_not_silently_sanitized(self):
        script = next(script for script in SCRIPTS if "GITHUB_OUTPUT" in script)
        with tempfile.TemporaryDirectory(prefix="ham-image-tag-") as directory:
            output = pathlib.Path(directory) / "output"
            env = dict(os.environ, GITHUB_EVENT_NAME="push", GITHUB_REF="refs/tags/v1/feature",
                       DEFAULT_BRANCH="main", GITHUB_OUTPUT=str(output))
            result = subprocess.run(["bash", "-eu", "-c", script], env=env,
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("cannot be used unchanged", result.stderr)
            self.assertFalse(output.exists())

    def test_tag_events_accept_uppercase_v_without_weekly_image_builds(self):
        patterns = ast.literal_eval(re.search(r"^    tags: (.+)$", WORKFLOW, re.M)[1])
        for tag in ("v1.0.0", "V1.0.0", "V1.0.0-beta.1", "release-1.0.0"):
            self.assertTrue(any(fnmatch.fnmatchcase(tag, pattern) for pattern in patterns), tag)
        self.assertNotRegex(WORKFLOW, r"(?m)^  schedule:")

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
