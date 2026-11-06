"""Authenticated loopback-only HTTP frontend. Use an SSH tunnel for access."""
from __future__ import annotations

import hashlib
import hmac
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import re
import threading
import time

from .core import Conflict, ValidationError


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


def credentials(username, password):
    if not isinstance(username, str) or not 1 <= len(username) <= 64:
        raise ValidationError("用户名长度应为 1–64 字符")
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise ValidationError("密码长度应为 12–256 字符")
    salt = secrets.token_hex(16)
    return {"username": username, "salt": salt, "hash": password_hash(password, salt)}


class Panel(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, endpoint, auth, dispatch):
        if endpoint[0] != "127.0.0.1":
            raise ValidationError("面板只允许绑定 127.0.0.1，请通过 SSH 隧道访问")
        self.auth, self.dispatch = auth, dispatch
        self.sessions = {}
        self.attempts = []
        self.guard = threading.Lock()
        self.workers = threading.BoundedSemaphore(16)
        super().__init__(endpoint, Handler)

    def process_request(self, request, client_address):
        if not self.workers.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.workers.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.workers.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "EZRealm"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *_):
        pass  # Never log cookies, passwords, request bodies or forwarding notes.

    def send(self, status, body, mime="application/json; charset=utf-8", cookie=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def host_valid(self):
        # SSH tunnels may use a different local port. The name must still be
        # a literal loopback host; arbitrary DNS names are never trusted.
        match = re.fullmatch(r"(?:127\.0\.0\.1|localhost):([0-9]{1,5})", self.headers.get("Host", ""))
        return bool(match and 1 <= int(match[1]) <= 65535)

    def session(self):
        try:
            jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
            token = jar["ezrealm_session"].value
        except (KeyError, cookies.CookieError):
            return None, None
        now = time.monotonic()
        with self.server.guard:
            self.server.sessions = {k: v for k, v in self.server.sessions.items() if v["expires"] > now}
            session = self.server.sessions.get(token)
        return token, session

    def do_GET(self):
        if not self.host_valid():
            return self.send(403, {"error": "Host 不允许"})
        static = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                  "/style.css": ("style.css", "text/css; charset=utf-8")}
        if self.path in static:
            filename, mime = static[self.path]
            return self.send(200, Path(__file__).with_name("static").joinpath(filename).read_bytes(), mime)
        _, session = self.session()
        if not session:
            return self.send(401, {"error": "请先登录"})
        try:
            if self.path == "/api/state":
                return self.send(200, {**self.server.dispatch({"action": "list"}), "csrf": session["csrf"]})
            if self.path in ("/api/export", "/api/export/full"):
                return self.send(200, self.server.dispatch({"action": "export", "full": self.path.endswith("/full")}))
            self.send(404, {"error": "不存在的地址"})
        except (ValueError, OSError, KeyError) as exc:
            self.send(400, {"error": str(exc)[:600]})

    def do_POST(self):
        if not self.host_valid() or self.headers.get("Origin") != "http://" + self.headers.get("Host", ""):
            return self.send(403, {"error": "请求来源不允许"})
        if self.headers.get_content_type() != "application/json" or self.headers.get("Transfer-Encoding"):
            return self.send(415, {"error": "仅接受 JSON 请求"})
        try:
            size = int(self.headers.get("Content-Length", "-1"))
            if not 0 <= size <= 2 * 1024 * 1024:
                return self.send(413, {"error": "请求最多 2 MiB"})
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValidationError("请求必须是对象")
            if self.path == "/api/login":
                return self.login(data)
            token, session = self.session()
            if not session:
                return self.send(401, {"error": "会话已过期，请重新登录"})
            if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), session["csrf"]):
                return self.send(403, {"error": "CSRF 校验失败"})
            if self.path == "/api/logout":
                with self.server.guard:
                    self.server.sessions.pop(token, None)
                return self.send(200, {"ok": True}, cookie="ezrealm_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
            if self.path != "/api/action":
                return self.send(404, {"error": "不存在的地址"})
            return self.send(200, self.server.dispatch(data))
        except Conflict as exc:
            self.send(409, {"error": str(exc)})
        except (ValueError, OSError, KeyError, TypeError) as exc:
            self.send(400, {"error": str(exc)[:600]})

    def login(self, data):
        username, password = data.get("username"), data.get("password")
        if not isinstance(username, str) or not isinstance(password, str) or len(password) > 256:
            raise ValidationError("无效的登录信息")
        now = time.monotonic()
        with self.server.guard:
            self.server.attempts = [t for t in self.server.attempts if t > now - 300]
            if len(self.server.attempts) >= 10:
                return self.send(429, {"error": "尝试次数过多，请在 5 分钟后重试"})
            self.server.attempts.append(now)
        auth = self.server.auth
        valid_password = hmac.compare_digest(password_hash(password, auth["salt"]), auth["hash"])
        if not (valid_password and hmac.compare_digest(username.encode(), auth["username"].encode())):
            return self.send(401, {"error": "用户名或密码错误"})
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        with self.server.guard:
            self.server.sessions = {k: v for k, v in self.server.sessions.items() if v["expires"] > now}
            if len(self.server.sessions) >= 100:
                self.server.sessions.pop(next(iter(self.server.sessions)))
            self.server.sessions[token] = {"csrf": csrf, "expires": now + 1800}
        self.send(200, {"csrf": csrf}, cookie=f"ezrealm_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=1800")


def main():
    from .broker import call
    auth = json.loads(Path("/etc/ezrealm-web/auth.json").read_text("utf-8"))
    with Panel(("127.0.0.1", 8765), auth, call) as server:
        server.serve_forever()
