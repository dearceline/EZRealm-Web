"""Exercise generated TOML/JSON against an actual official Realm v2.7.0 binary."""
import os
from pathlib import Path
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
import unittest

from ezrealm.core import Store


class TCPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(3)
        while data := self.request.recv(65536):
            self.request.sendall(data)


class UDPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        data, connection = self.request
        connection.sendto(data, self.client_address)


@unittest.skipUnless(os.environ.get("REALM_BINARY"), "Set REALM_BINARY to official Realm v2.7.0")
class ForwardingTests(unittest.TestCase):
    def exercise(self, suffix="toml", mode="tcp+udp", ipv6=False):
        family, host = (socket.AF_INET6, "::1") if ipv6 else (socket.AF_INET, "127.0.0.1")
        class TCP(socketserver.ThreadingTCPServer):
            address_family = family
            daemon_threads = True
        class UDP(socketserver.ThreadingUDPServer):
            address_family = family
            daemon_threads = True
        try:
            tcp = TCP((host, 0), TCPHandler)
        except OSError as exc:
            if ipv6:
                self.skipTest(f"IPv6 unavailable: {exc}")
            raise
        udp = UDP((host, tcp.server_address[1]), UDPHandler)
        for server in (tcp, udp):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        with socket.socket(family) as candidate:
            candidate.bind((host, 0))
            local_port = candidate.getsockname()[1]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / ("config." + suffix)
            path.write_text('{"endpoints": []}' if suffix == "json" else "endpoints = []\n", encoding="utf-8")
            store = Store(path, Path(temp) / "state")
            prefix = f"[{host}]" if ipv6 else host
            store.mutate("add", {"listen": f"{prefix}:{local_port}", "remote": f"{prefix}:{tcp.server_address[1]}",
                                 "protocol": mode, "note": '真实测试 "中文备注" <script> & \\ '})
            logfile = open(Path(temp) / "realm.log", "w+b")
            process = subprocess.Popen([os.environ["REALM_BINARY"], "-c", str(path)], stdout=logfile, stderr=logfile,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            try:
                message = b"ezrealm-forwarding-" + os.urandom(32)
                deadline = time.monotonic() + 8
                success = False
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        logfile.seek(0)
                        self.fail("Realm exited: " + logfile.read().decode(errors="replace"))
                    try:
                        if mode != "udp":
                            with socket.socket(family, socket.SOCK_STREAM) as client:
                                client.settimeout(.25)
                                client.connect((host, local_port))
                                client.sendall(message)
                                self.assertEqual(client.recv(65536), message)
                        if mode != "tcp":
                            with socket.socket(family, socket.SOCK_DGRAM) as client:
                                client.settimeout(.25)
                                client.sendto(message, (host, local_port))
                                self.assertEqual(client.recv(65536), message)
                        success = True
                        break
                    except (ConnectionError, TimeoutError, OSError):
                        time.sleep(.05)
                self.assertTrue(success, "No successful relay within 8 seconds")
                if mode == "tcp":
                    with socket.socket(family, socket.SOCK_DGRAM) as client:
                        client.settimeout(.3)
                        client.sendto(message, (host, local_port))
                        with self.assertRaises((TimeoutError, ConnectionError)):
                            client.recv(65536)
                if mode == "udp":
                    with socket.socket(family) as client:
                        client.settimeout(.3)
                        with self.assertRaises(OSError):
                            client.connect((host, local_port))
            finally:
                process.terminate()
                process.wait(timeout=5)
                logfile.close()

    def test_toml_tcp_udp(self):
        self.exercise()

    def test_json_tcp_udp(self):
        self.exercise(suffix="json")

    def test_tcp_only(self):
        self.exercise(mode="tcp")

    def test_udp_only(self):
        self.exercise(mode="udp")

    def test_ipv6_tcp_udp(self):
        self.exercise(ipv6=True)


if __name__ == "__main__":
    unittest.main()
