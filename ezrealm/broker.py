"""Root-only Unix socket broker; panel is an unprivileged client."""
import grp
import json
import os
import pwd
import socket
import socketserver
import struct

from .core import ValidationError
from .linux import Controller, SOCKET

LIMIT = 2 * 1024 * 1024


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(50)
        _, uid, _ = struct.unpack("3i", self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid not in (0, pwd.getpwnam("ezrealm-web").pw_uid):
            return
        try:
            line = self.rfile.readline(LIMIT + 1)
            if len(line) > LIMIT or not line.endswith(b"\n"):
                raise ValidationError("请求过大或不完整")
            result = {"ok": True, "result": self.server.controller.dispatch(json.loads(line))}
        except (ValueError, OSError, KeyError, TypeError) as exc:
            result = {"ok": False, "error": str(exc)[:600]}
        self.wfile.write(json.dumps(result, ensure_ascii=False).encode() + b"\n")


def call(request):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(50)
        connection.connect(SOCKET)
        connection.sendall(json.dumps(request, ensure_ascii=False).encode() + b"\n")
        with connection.makefile("rb") as stream:
            line = stream.readline(8 * 1024 * 1024)
        response = json.loads(line)
        if not response.get("ok"):
            raise ValidationError(response.get("error", "服务请求失败"))
        return response["result"]


def main():
    if os.path.exists(SOCKET):
        os.unlink(SOCKET)  # systemd guarantees a single broker instance.
    with socketserver.UnixStreamServer(SOCKET, Handler) as server:
        os.chown(SOCKET, 0, grp.getgrnam("ezrealm-web").gr_gid)
        os.chmod(SOCKET, 0o660)
        server.controller = Controller()
        server.serve_forever()
