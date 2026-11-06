"""Run the actual shell preflight with isolated fake system packages."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.platform == "linux" and hasattr(os, "geteuid") and os.geteuid() == 0,
                     "Shell preflight integration runs as root on Linux")
class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        source = (Path(__file__).resolve().parent.parent / "realm.sh").read_text("utf-8")
        header = source.split("3<<'EZREALM_BUNDLE_PY'\n", 1)[0]
        self.script = self.root / "realm.sh"
        self.script.write_text(header + "3<<'EZREALM_BUNDLE_PY'\n# Test payload\nEZREALM_BUNDLE_PY\n", encoding="utf-8")
        self.env = {**os.environ, "PATH": str(self.bin), "EZ_TEST_ROOT": str(self.root)}
        self.python = self.root / "python-template"
        self.python.write_text('''#!/bin/bash
if [ "$1" = "-c" ]; then
  exit 0
fi
if [ "$1" = "--version" ]; then
  echo "Python 3.11.9"
  exit 0
fi
echo "SELECTED_PYTHON=$0"
echo "FORWARDED_ARGUMENT=$3"
while IFS= read -r line; do printf '%s\\n' "$line"; done
''', encoding="utf-8")
        self.python.chmod(0o755)

    def add_apt(self, update_fail=False, install_fail=False):
        path = self.bin / "apt-get"
        path.write_text(f'''#!/bin/bash
printf '%s\\n' "$*" >> "$EZ_TEST_ROOT/apt.log"
# apt must receive /dev/null, never consume a pasted rule from stdin.
if IFS= read -r unexpected; then
  echo "apt consumed rule input" >&2
  exit 77
fi
if [ "$1" = "update" ]; then
  exit {1 if update_fail else 0}
fi
if [ "$1" = "install" ]; then
  if [ "${{DEBIAN_FRONTEND:-}}" != "noninteractive" ]; then exit 78; fi
  if [ "{1 if install_fail else 0}" = "1" ]; then exit 1; fi
  /bin/cp "$EZ_TEST_ROOT/python-template" "$EZ_TEST_ROOT/bin/python3"
fi
''', encoding="utf-8")
        path.chmod(0o755)

    def run_script(self):
        return subprocess.run(["/bin/bash", str(self.script), "--import", "-"],
                              input="1|8080|[::1]:443|保留备注\n", text=True,
                              capture_output=True, env=self.env, timeout=10)

    def test_missing_python_installs_deps_preserving_rule_stdin(self):
        self.add_apt()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("1|8080|[::1]:443|保留备注", result.stdout)
        self.assertIn("FORWARDED_ARGUMENT=-", result.stdout)
        calls = (self.root / "apt.log").read_text().splitlines()
        self.assertEqual(calls, ["update", "install -y --no-install-recommends python3 ca-certificates"])

    def test_existing_python_never_invokes_package_manager(self):
        shutil.copyfile(self.python, self.bin / "python3")
        (self.bin / "python3").chmod(0o755)
        self.add_apt()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "apt.log").exists())

    def test_versioned_python_selected_without_replacing_system_python(self):
        old = self.bin / "python3"
        old.write_text('#!/bin/bash\nexit 1\n', encoding="utf-8")
        old.chmod(0o755)
        shutil.copyfile(self.python, self.bin / "python3.11")
        (self.bin / "python3.11").chmod(0o755)
        self.add_apt()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("bin/python3.11", result.stdout)
        self.assertFalse((self.root / "apt.log").exists())

    def test_failed_update_stops_before_install(self):
        self.add_apt(update_fail=True)
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("更新软件源失败", result.stderr)
        self.assertEqual((self.root / "apt.log").read_text().splitlines(), ["update"])

    def test_failed_install_is_reported(self):
        self.add_apt(install_fail=True)
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("安装 Python 依赖失败", result.stderr)

    def test_no_package_manager_reports_missing_runtime(self):
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("未安装 Python 3.11+", result.stderr)


if __name__ == "__main__":
    unittest.main()
