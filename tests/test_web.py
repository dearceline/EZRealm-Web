import http.client
import json
import threading
import unittest

from ezrealm.core import ValidationError
from ezrealm.web import Panel, credentials


class WebTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        def dispatch(data):
            self.calls.append(data)
            return {"rules": [], "revision": "abc", "service": {"active": "active"}}
        self.server = Panel(("127.0.0.1", 0), credentials("管理员", "a-very-strong-password"), dispatch)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host = f"127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, path, data=None, cookie=None, csrf=None, origin=True, host=None, content_type="application/json"):
        headers = {"Host": host or self.host}
        if origin:
            headers["Origin"] = "http://" + self.host
        if data is not None:
            headers["Content-Type"] = content_type
        if cookie:
            headers["Cookie"] = cookie
        if csrf:
            headers["X-CSRF-Token"] = csrf
        client = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        client.request("POST" if data is not None else "GET", path, json.dumps(data).encode() if data is not None else None, headers)
        response = client.getresponse()
        body, saved_headers, status = response.read(), dict(response.getheaders()), response.status
        client.close()
        return status, saved_headers, body

    def login(self):
        status, headers, body = self.request("/api/login", {"username": "管理员", "password": "a-very-strong-password"})
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly; SameSite=Strict", headers["Set-Cookie"])
        return headers["Set-Cookie"].split(";", 1)[0], json.loads(body)["csrf"]

    def test_auth_csrf_origin_and_logout(self):
        self.assertEqual(self.request("/api/state")[0], 401)
        cookie, csrf = self.login()
        self.assertEqual(self.request("/api/state", cookie=cookie)[0], 200)
        self.assertEqual(self.request("/api/action", {"action": "delete"}, cookie=cookie)[0], 403)
        self.assertEqual(self.request("/api/action", {"action": "delete"}, cookie=cookie, csrf=csrf, origin=False)[0], 403)
        self.assertEqual(self.request("/api/action", {"action": "list"}, cookie=cookie, csrf=csrf)[0], 200)
        self.assertEqual(self.request("/api/logout", {}, cookie=cookie, csrf=csrf)[0], 200)
        self.assertEqual(self.request("/api/state", cookie=cookie)[0], 401)

    def test_rebinding_and_static_security_headers(self):
        self.assertEqual(self.request("/", host="evil.example:8765")[0], 403)
        self.assertEqual(self.request("/", host="127.0.0.1:18765")[0], 200)
        status, headers, _ = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(self.request("/api/login", {}, content_type="text/plain")[0], 415)

    def test_rate_limit(self):
        for _ in range(10):
            self.assertEqual(self.request("/api/login", {"username": "管理员", "password": "wrong"})[0], 401)
        self.assertEqual(self.request("/api/login", {"username": "管理员", "password": "wrong"})[0], 429)

    def test_expired_session(self):
        cookie, csrf = self.login()
        for session in self.server.sessions.values():
            session["expires"] = 0
        self.assertEqual(self.request("/api/state", cookie=cookie)[0], 401)

    def test_no_public_binding_and_short_password(self):
        with self.assertRaises(ValidationError):
            Panel(("0.0.0.0", 0), {}, lambda _: {})
        with self.assertRaises(ValidationError):
            credentials("admin", "123456")


if __name__ == "__main__":
    unittest.main()
