import ast
import base64
import hashlib
import io
from pathlib import Path
import re
import shlex
import unittest
import zipfile


class BundleTests(unittest.TestCase):
    def test_payload_matches_sources_and_preserves_stdin(self):
        root = Path(__file__).resolve().parent.parent
        script = (root / "realm.sh").read_text("utf-8")
        command = script[script.index("exec python3 -c "):]
        argv = shlex.split(command)
        self.assertEqual(argv[:4], ["exec", "python3", "-c", argv[3]])
        self.assertEqual(argv[4], "$@")
        program = argv[3]
        ast.parse(program)
        data = base64.b64decode(re.search(r'payload = base64.b64decode\("""\n(.*?)\n"""\)', program, re.S)[1])
        expected = re.search(r'expected = "([0-9a-f]{64})"', program)[1]
        self.assertEqual(hashlib.sha256(data).hexdigest(), expected)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for path in [root / "entry.py", *list((root / "ezrealm").rglob("*"))]:
                if path.is_file() and "__pycache__" not in path.parts:
                    self.assertEqual(archive.read(path.relative_to(root).as_posix()), path.read_bytes())
            self.assertIn(b"MIT", archive.read("TOMLKIT-LICENSE"))
            self.assertTrue(all(item.create_system == 3 for item in archive.infolist()))


if __name__ == "__main__":
    unittest.main()
