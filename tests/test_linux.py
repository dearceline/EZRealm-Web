"""Service rollback and discovery tests use a fake systemctl, never the host's services."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ezrealm.core import Store, ValidationError
from ezrealm.linux import Controller, discovery


class LinuxTests(unittest.TestCase):
    def test_failed_restart_restores_applied(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.toml"
            original = b"endpoints = []\n"
            path.write_bytes(original)
            store = Store(path, Path(temp) / "state")
            store.mutate("import", "1|8000|127.0.0.1:80|test")
            controller = Controller(store)
            calls = []
            def fake(*args, **kwargs):
                calls.append(args)
                if args == ("restart", "realm.service") and kwargs.get("check", True):
                    raise ValidationError("simulated failure")
                return subprocess.CompletedProcess(args, 0, "active\n", "")
            with patch("ezrealm.linux.systemctl", fake), self.assertRaises(ValidationError):
                controller.service("restart")
            self.assertEqual(path.read_bytes(), original)
            self.assertIn(("restart", "realm.service"), calls)
            self.assertTrue(list((store.state / "backups").glob("failed-*")))

    def test_discovery_uses_existing_execstart(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "legacy.json"
            path.write_text('{"endpoints": []}', encoding="utf-8")
            def fake(*args, **kwargs):
                value = "loaded\n" if "LoadState" in args else f'{{ path=/opt/realm; argv[]=/opt/realm -c "{path}" ; ignore_errors=no ; }}'
                return subprocess.CompletedProcess(args, 0, value, "")
            with patch("ezrealm.linux.systemctl", fake):
                self.assertEqual(discovery(), str(path))

    def test_reject_unknown_service_request(self):
        controller = Controller.__new__(Controller)
        with self.assertRaises(ValidationError):
            controller.service("restart; reboot")


if __name__ == "__main__":
    unittest.main()
