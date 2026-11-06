"""Run as root ONLY inside a disposable Ubuntu CI runner."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ezrealm.core import atomic_write
from ezrealm.linux import Controller, install_realm, store, systemctl
from ezrealm.web import credentials


def main():
    assert os.environ.get("GITHUB_ACTIONS") == "true", "Only run in disposable GitHub Actions runner"
    path = Path("/etc/realm/config.toml")
    path.parent.mkdir(exist_ok=True)
    original = b'# CI existing configuration\nendpoints = []\n[network]\nuse_udp = true\n'
    path.write_bytes(original)
    # The actual downloaded-file UX must keep piped standard input intact.
    result = subprocess.run(["bash", str(ROOT / "realm.sh"), "--import", "-"], input="1|23456|127.0.0.1:9|CI备注\n", text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "成功 1" in result.stdout, result.stdout
    assert "CI备注" in path.read_text()
    assert next(Path("/var/lib/ezrealm-web/backups").glob("*.toml")).read_bytes() == original
    install_realm(path)
    Controller().service("start")
    atomic_write(Path("/etc/ezrealm-web/auth.json"), json.dumps(credentials("ci-admin", "ci-only-strong-password")).encode(), 0o640)
    import grp
    os.chown("/etc/ezrealm-web/auth.json", 0, grp.getgrnam("ezrealm-web").gr_gid)
    systemctl("start", "ezrealm-broker.service")
    systemctl("start", "ezrealm-web.service")
    try:
        for _ in range(30):
            try:
                connection = http.client.HTTPConnection("127.0.0.1", 8765, timeout=5)
                connection.request("GET", "/")
                response = connection.getresponse()
                assert response.status == 200, response.read()
                response.read()
                connection.close()
                break
            except ConnectionError:
                time.sleep(.2)
        else:
            raise AssertionError("Panel did not start")
        connection = http.client.HTTPConnection("127.0.0.1", 8765, timeout=10)
        connection.request("POST", "/api/login", json.dumps({"username": "ci-admin", "password": "ci-only-strong-password"}),
                           {"Content-Type": "application/json", "Origin": "http://127.0.0.1:8765"})
        response = connection.getresponse()
        assert response.status == 200, response.read()
        csrf = json.loads(response.read())["csrf"]
        cookie = response.getheader("Set-Cookie").split(";", 1)[0]
        connection.close()
        connection = http.client.HTTPConnection("127.0.0.1", 8765, timeout=10)
        connection.request("GET", "/api/state", headers={"Cookie": cookie})
        response = connection.getresponse()
        assert response.status == 200, response.read()
        snapshot = json.loads(response.read())
        assert snapshot["rules"][0]["note"] == "CI备注", snapshot
        connection.close()
        connection = http.client.HTTPConnection("127.0.0.1", 8765, timeout=10)
        connection.request("POST", "/api/action", json.dumps({"action": "add", "revision": snapshot["revision"],
            "data": {"listen": "127.0.0.1:23457", "remote": "[::1]:9", "protocol": "udp", "note": "broker"}}),
            {"Content-Type": "application/json", "Origin": "http://127.0.0.1:8765", "Cookie": cookie, "X-CSRF-Token": csrf})
        response = connection.getresponse()
        assert response.status == 200, response.read()
        assert len(json.loads(response.read())["rules"]) == 2
        connection.close()
        uid = systemctl("show", "ezrealm-web.service", "-p", "User", "--value").stdout.strip()
        assert uid == "ezrealm-web", uid
        assert subprocess.run(["runuser", "-u", "ezrealm-web", "--", "test", "-w", str(path)]).returncode != 0
        Controller().service("restart")
        # Running the bootstrap again is an upgrade, and must keep every config byte.
        before = path.read_bytes()
        result = subprocess.run(["bash", str(ROOT / "realm.sh"), "--export", "-", "--full"], capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert path.read_bytes() == before
        print("PASS: piped EOF import, preserved config, systemd service, unprivileged web, broker write, login, restart, upgrade")
    finally:
        systemctl("stop", "ezrealm-web.service", "ezrealm-broker.service", "realm.service", check=False)


if __name__ == "__main__":
    main()
