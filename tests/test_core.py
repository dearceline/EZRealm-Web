import json
from pathlib import Path
import tempfile
import unittest

import tomlkit
from ezrealm.core import Conflict, Store, ValidationError, address, rule

FIXTURE = '''# Old configuration, must survive
[log]
level = "warn"
[network]
use_udp = true
tcp_timeout = 7
[[endpoints]]
listen = "0.0.0.0:7000" # 备注: 旧服务器
remote = "192.0.2.1:443"
extra_remotes = ["192.0.2.2:443"]
balance = "roundrobin: 1, 2"
[endpoints.network]
tcp_timeout = 11
'''


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "config.toml"
        self.path.write_text(FIXTURE, encoding="utf-8")
        self.store = Store(self.path, self.root / "state")

    def test_legacy_unchanged_on_read(self):
        before = self.path.read_bytes()
        self.assertEqual(self.store.snapshot()["rules"][0]["note"], "旧服务器")
        self.assertEqual(self.path.read_bytes(), before)

    def test_import_ipv6_notes_duplicates_conflicts(self):
        result = self.store.mutate("import", "1|8080|192.168.1.1:8000| 一号服务器 \n2|9000|[2001:0db8::1]:8080|IPv6服务器\n3|8080|192.168.1.1:8000|重复\n4|7000|192.0.2.99:443|冲突\n5|0|192.0.2.1:1|错误\n6|23|2001:db8::1:80|无括号\n")
        self.assertEqual({k: result["report"][k] for k in ("success", "duplicate", "failed")}, {"success": 2, "duplicate": 1, "failed": 3})
        self.assertEqual(result["rules"][1]["note"], " 一号服务器 ")
        self.assertEqual(result["rules"][2]["remote"], "[2001:db8::1]:8080")
        doc = tomlkit.parse(self.path.read_text("utf-8"))
        self.assertEqual(doc["endpoints"][0]["extra_remotes"], ["192.0.2.2:443"])
        self.assertIn("# Old configuration", self.path.read_text("utf-8"))
        self.assertEqual(next((self.root / "state/backups").iterdir()).read_text("utf-8"), FIXTURE)

    def test_empty_invalid_import_is_noop(self):
        before = self.path.read_bytes()
        result = self.store.mutate("import", "\n序号|本地端口|远程IP:端口|备注\n1|3|bad|a|b")
        self.assertEqual(result["report"]["success"], 0)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.root / "state/backups").exists())

    def test_optimistic_lock_and_delete(self):
        old = self.store.snapshot()["revision"]
        self.store.mutate("add", {"listen": "127.0.0.1:9000", "remote": "example.org:443", "note": "test"}, old)
        with self.assertRaises(Conflict):
            self.store.mutate("delete", {"id": 1}, old)
        self.store.mutate("delete", {"id": 2}, self.store.snapshot()["revision"])
        self.assertEqual(len(self.store.snapshot()["rules"]), 1)

    def test_edit_preserves_advanced(self):
        self.store.mutate("edit", {"id": 1, "listen": "0.0.0.0:7000", "remote": "[::1]:443", "protocol": "udp", "note": '中文"\\<script> & 备注'})
        doc = tomlkit.parse(self.path.read_text("utf-8"))
        self.assertEqual(doc["endpoints"][0]["network"]["tcp_timeout"], 11)
        self.assertEqual(self.store.snapshot()["rules"][0]["protocol"], "udp")
        self.assertEqual(doc["endpoints"][0]["balance"], "roundrobin: 1, 2")

    def test_protocol_disjoint_port_allowed(self):
        self.store.mutate("edit", {"id": 1, "listen": "0.0.0.0:7000", "remote": "192.0.2.1:1", "protocol": "tcp"})
        self.store.mutate("add", {"listen": "[::]:7000", "remote": "[::1]:2", "protocol": "udp"})
        with self.assertRaises(Conflict):
            self.store.mutate("add", {"listen": "127.0.0.1:7000", "remote": "example.org:4"})

    def test_roundtrip_export(self):
        self.store.mutate("import", "1|8080|[2001:db8::1]:443|  测试  ")
        text = self.store.export()
        path = self.root / "another.toml"
        path.write_text("endpoints = []\n", encoding="utf-8")
        another = Store(path, self.root / "other")
        self.assertEqual(another.mutate("import", text)["report"]["success"], 2)
        self.assertEqual(another.snapshot()["rules"][-1]["note"], "  测试  ")
        self.assertEqual(self.store.export(full=True), self.path.read_bytes().decode("utf-8"))

    def test_json_preserves_unknown_and_notes(self):
        self.path = self.root / "config.json"
        self.path.write_text(json.dumps({"custom": {"x": 2}, "endpoints": []}), encoding="utf-8")
        data = Store(self.path, self.root / "json-state")
        data.mutate("import", "1|9000|[::1]:2|JSON备注")
        self.assertEqual(json.loads(self.path.read_text("utf-8"))["custom"], {"x": 2})
        self.assertEqual(data.snapshot()["rules"][0]["note"], "JSON备注")

    def test_corrupt_original_not_overwritten(self):
        self.path.write_text('[[endpoints]\nbad', encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaises(ValidationError):
            self.store.mutate("import", "1|9000|[::1]:2|测试")
        self.assertEqual(before, self.path.read_bytes())

    def test_invalid_values_and_injection(self):
        for value in ("999.1.1.1:80", "[2001:db8:::1]:90", "foo:0", "a:65536", "$(id):80", "a:80\nrm -rf /", "[127.0.0.1]:80"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                address(value)
        with self.assertRaises(ValidationError):
            rule({"listen": "0.0.0.0:1", "remote": "1.1.1.1:2", "note": "evil\x1b[31m"})
        self.assertEqual(address("[2001:0db8::1]:00080"), "[2001:db8::1]:80")


if __name__ == "__main__":
    unittest.main()
