"""Optional Ansible tests: only assertions, fake systemctl and temporary files."""
import copy
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

try:
    import yaml
    import jinja2
except ImportError:
    yaml = jinja2 = None

SOURCE = Path(__file__).resolve().parent.parent
ANSIBLE = os.environ.get("ANSIBLE_PLAYBOOK") or shutil.which("ansible-playbook")


@unittest.skipUnless(ANSIBLE and yaml and jinja2, "Install deploy/requirements.txt for Ansible integration tests")
class DeployTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.sandbox = Path(self.temporary.name)
        (self.sandbox / "deploy").mkdir()
        shutil.copy2(SOURCE / "deploy/ansible.cfg", self.sandbox / "deploy/ansible.cfg")
        shutil.copytree(SOURCE / "deploy/templates", self.sandbox / "deploy/templates")
        shutil.copytree(SOURCE / "scripts", self.sandbox / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
        self.play = yaml.safe_load((SOURCE / "deploy/site.yml").read_text())[0]
        self.variables = yaml.safe_load((SOURCE / "deploy/group_vars/all.yml").read_text())
        self.variables.update({
            "ansible_facts": {
                "distribution": "Ubuntu", "distribution_version": "26.04",
                "architecture": "x86_64", "user_uid": 1000, "user_id": "arktest",
                "env": {"HOME": str(self.sandbox / "home")},
            },
            "ark_server_password": "participant_0123456789",
            "ark_admin_password": "administrator_9876543210",
        })
        self.variables.update(self.play["vars"])
        self.systemctl = self.sandbox / "fake-systemctl"
        self.set_systemctl("inactive", 3)

    def set_systemctl(self, state, code):
        self.systemctl.write_text(f"#!/bin/sh\nprintf '%s\\n' '{state}' 'inactive' 'inactive'\nexit {code}\n")
        self.systemctl.chmod(0o700)

    def run_play(self, tasks, changes=None, check=False):
        variables = copy.deepcopy(self.variables)
        variables.update(changes or {})
        playbook = [{"name": "Offline sandbox only", "hosts": "all", "gather_facts": False,
                     "vars": variables, "tasks": copy.deepcopy(tasks)}]
        path = self.sandbox / "deploy/test-play.yml"
        path.write_text(yaml.safe_dump(playbook, sort_keys=False))
        command = [ANSIBLE, "-i", "localhost,", "-c", "local", str(path)]
        if check:
            command.append("--check")
        # Keep BOTH controller and local-target Ansible state out of the real
        # home directory; the source-only container's root filesystem is RO.
        environment = os.environ | {
            "HOME": str(self.sandbox / "controller-home"),
            "ANSIBLE_HOME": str(self.sandbox / "ansible-home"),
            "ANSIBLE_NOCOLOR": "1",
            "ANSIBLE_LOCAL_TEMP": str(self.sandbox / "ansible-local"),
            "ANSIBLE_REMOTE_TEMP": str(self.sandbox / "ansible-remote"),
        }
        result = subprocess.run(command, cwd=self.sandbox / "deploy", env=environment,
                                text=True, capture_output=True, timeout=60)
        # The preflight rejects bad passwords without ever echoing their values.
        self.assertNotIn(variables["ark_server_password"], result.stdout + result.stderr)
        self.assertNotIn(variables["ark_admin_password"], result.stdout + result.stderr)
        return result

    def preflight_tasks(self):
        tasks = copy.deepcopy(self.play["pre_tasks"])
        probe = next(t for t in tasks if "ansible.builtin.command" in t)
        probe["ansible.builtin.command"]["argv"][0] = str(self.systemctl)
        return tasks

    def test_preflight_accepts_stopped_supported_target(self):
        result = self.run_play(self.preflight_tasks())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_preflight_rejects_unsafe_configuration(self):
        cases = [
            {"ark_query_port": 7778},
            {"ark_session_name": "bad?Port=1"},
            {"ark_session_name": "   "},
            {"ark_map": "Genesis\n"},
            {"ark_server_password": "participant_0123456789\n"},
            {"ark_admin_password": "participant_0123456789"},
            {"ark_root_dir": "/srv/ark"},
            {"ark_enable_autostart": True, "ark_enable_linger": False},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                result = self.run_play(self.preflight_tasks(), changes)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_preflight_refuses_active_service_and_unavailable_user_bus(self):
        for state, code in (("active", 0), ("activating", 0), ("inactive", 1)):
            with self.subTest(state=state, code=code):
                self.set_systemctl(state, code)
                result = self.run_play(self.preflight_tasks())
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_file_plan_supports_check_mode_on_a_fresh_target(self):
        tasks = [t for t in self.play["tasks"] if t["name"] in (
            "Create private installation directories",
            "Copy only source scripts, never the controller's Saved or credentials",
            "Install portable systemd user units",
        )]
        result = self.run_play(tasks, check=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.sandbox / "home").exists())

    def test_secret_application_is_idempotent_through_ansible(self):
        root = self.sandbox / "installed"
        (root / "scripts").mkdir(parents=True)
        shutil.copy2(SOURCE / "scripts/ark_config.py", root / "scripts/ark_config.py")
        tasks = [t for t in self.play["tasks"] if t["name"].startswith("Merge managed INI")]
        first = self.run_play(tasks, {"ark_root_dir": str(root)})
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn("changed=1", first.stdout)
        second = self.run_play(tasks, {"ark_root_dir": str(root)})
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("changed=0", second.stdout)

    def test_ci_workflow_yaml_and_steps_are_valid(self):
        workflow = yaml.safe_load((SOURCE / ".github/workflows/test.yml").read_text())
        self.assertEqual(workflow["name"], "source-tests")
        self.assertEqual(set(workflow["on"]), {"push", "pull_request"})
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        for job in workflow["jobs"].values():
            for step in job["steps"]:
                self.assertIsInstance(step.get("run", step.get("uses")), str)
                if "uses" in step:
                    self.assertRegex(step["uses"], r"^actions/(checkout|setup-python)@[0-9a-f]{40}$")

    def test_units_render_for_a_different_home_without_local_user_dependencies(self):
        environment = jinja2.Environment(undefined=jinja2.StrictUndefined)
        for path in (SOURCE / "deploy/templates").glob("*.j2"):
            rendered = environment.from_string(path.read_text()).render(ark_root_dir="/home/YOUR_USER/Documents/ark-server")
            self.assertNotIn(str(SOURCE), rendered)
            if "ExecStart=" in rendered:
                self.assertIn("ExecStart=/home/YOUR_USER/Documents/ark-server/scripts/", rendered)
            self.assertNotIn("User=root", rendered)


if __name__ == "__main__":
    unittest.main()
