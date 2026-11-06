"""PTY integration: a literal Ctrl+D ends import and returns to the menu."""
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
import unittest


@unittest.skipUnless(sys.platform == "linux", "Linux PTY required")
class EOFTests(unittest.TestCase):
    def test_ctrl_d_returns_to_menu(self):
        import pty
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config.toml"
            config.write_text("endpoints = []\n", encoding="utf-8")
            program = '''import sys
from pathlib import Path
from unittest.mock import patch
from ezrealm.core import Store
from ezrealm.linux import Controller
from ezrealm.cli import run_menu
controller = Controller(Store(sys.argv[1], Path(sys.argv[1]).parent / "state"))
controller.status = lambda: {"active": "inactive", "enabled": "enabled"}
with patch("ezrealm.cli.Controller", lambda: controller):
    run_menu()
'''
            master, slave = pty.openpty()
            process = subprocess.Popen([sys.executable, "-c", program, str(config)], stdin=slave, stdout=slave, stderr=slave)
            os.close(slave)
            output = bytearray()
            def until(fragment):
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    if fragment.encode() in output:
                        return
                    readable, _, _ = select.select([master], [], [], .1)
                    if readable:
                        try:
                            output.extend(os.read(master, 65536))
                        except OSError:
                            break
                self.fail("PTY output missing " + fragment + ": " + output.decode(errors="replace"))
            try:
                until("请选择一个选项[0-13]")
                os.write(master, b"12\n")
                until("Ctrl+D")
                os.write(master, "1|8080|[2001:db8::1]:8080|保留备注\n".encode())
                os.write(master, b"\x04")
                until("成功 1，重复 0，失败 0")
                output.clear()
                os.write(master, b"3\n")
                until("保留备注")
                os.write(master, b"0\n")
                self.assertEqual(process.wait(timeout=5), 0)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                os.close(master)


if __name__ == "__main__":
    unittest.main()
