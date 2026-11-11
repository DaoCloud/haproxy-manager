#!/usr/bin/env python3
"""Check lab image selection without touching host Docker or networks."""
import ipaddress
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools/integration"))

from lab import Lab  # noqa: E402


class LabImageTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        command = patch("lab.subprocess.run",
                        return_value=subprocess.CompletedProcess([], 0, stdout=""))
        self.command = command.start()
        self.addCleanup(command.stop)
        subnet = patch.object(Lab, "free_subnet",
                              return_value=ipaddress.ip_network("172.30.77.0/24"))
        subnet.start()
        self.addCleanup(subnet.stop)

    def commands(self):
        return [entry.args[0] for entry in self.command.call_args_list]

    def compose_command(self, lab, *args):
        return ["docker", "compose", "-p", lab.project, "-f",
                str(ROOT / "tools/integration/compose.yml"), *args]

    def test_up_builds_workspace_images_when_no_image_is_selected(self):
        for value in (None, ""):
            with self.subTest(image=value):
                if value is not None:
                    os.environ["HAM_LAB_IMAGE"] = value
                lab = Lab()
                self.command.reset_mock()
                lab.up()
                self.assertEqual(self.commands(), [
                    self.compose_command(lab, "build", "proxy-a"),
                    self.compose_command(lab, "build", "backend"),
                    self.compose_command(lab, "up", "-d", "--no-build"),
                    self.compose_command(lab, "exec", "-T", "backend",
                                         "python3", "/lab/lab.py", "_init"),
                ])
                self.assertEqual(lab.env["LAB_IMAGE"], lab.project + ":local")

    def test_up_pulls_selected_tag_or_digest_instead_of_building_proxy(self):
        for image in ("ghcr.io/daocloud/haproxy-manager:V1.0.0",
                      "ghcr.io/daocloud/haproxy-manager@sha256:" + "a" * 64):
            with self.subTest(image=image):
                os.environ["HAM_LAB_IMAGE"] = image
                lab = Lab()
                self.command.reset_mock()
                lab.up()
                self.assertEqual(lab.env["LAB_IMAGE"], image)
                self.assertEqual(self.commands(), [
                    ["docker", "pull", image],
                    self.compose_command(lab, "build", "backend"),
                    self.compose_command(lab, "up", "-d", "--no-build"),
                    self.compose_command(lab, "exec", "-T", "backend",
                                         "python3", "/lab/lab.py", "_init"),
                ])
                for entry in self.command.call_args_list:
                    self.assertEqual(entry.kwargs["env"]["LAB_IMAGE"], image)

    def test_pull_failure_stops_before_build_or_startup(self):
        os.environ["HAM_LAB_IMAGE"] = "registry.example.test/ham:missing"
        lab = Lab()
        self.command.reset_mock()
        self.command.side_effect = subprocess.CalledProcessError(
            1, ["docker", "pull", lab.env["HAM_LAB_IMAGE"]])
        with self.assertRaises(subprocess.CalledProcessError):
            lab.up()
        self.assertEqual(self.commands(),
                         [["docker", "pull", lab.env["HAM_LAB_IMAGE"]]])

    def test_test_scenarios_prepare_selected_images_before_starting(self):
        os.environ["HAM_LAB_IMAGE"] = "ghcr.io/daocloud/haproxy-manager:V1.0.0"
        lab = Lab(testing=True)
        for scenario in (lab.test_tcp_deployment, lab.test_startup):
            with self.subTest(scenario=scenario.__name__), \
                    patch.object(lab, "compose", side_effect=RuntimeError("stop before startup")):
                self.command.reset_mock()
                with self.assertRaisesRegex(RuntimeError, "stop before startup"):
                    scenario()
                self.assertEqual(self.commands(),
                                 [["docker", "pull", os.environ["HAM_LAB_IMAGE"]]])


if __name__ == "__main__":
    unittest.main()
