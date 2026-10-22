"""HTTP authentication context, cookie and CSRF integration."""
import json
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen
from unittest.mock import patch

from cryptography.fernet import Fernet

from atoms_demo.api.server import handler_type
from atoms_demo.bootstrap import compose_application
from atoms_demo.config import Settings


class CaptchaStub:
    async def verify(self, token, request_id):
        return token == "ok"


class MailStub:
    def __init__(self):
        self.url = ""

    async def send_verification(self, address, url, key):
        self.url = url
        return "stub"


class HTTPIdentityTests(unittest.TestCase):
    def test_session_cookie_is_http_only_and_csrf_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = Settings(database_url=f"sqlite:///{root / 'db.sqlite'}", blob_directory=root / "blobs",
                                public_origin="http://localhost:5173", preview_origin="http://localhost:8001",
                                model_provider="", model_secret_name="", task_timeout_seconds=180)
            with patch.dict(os.environ, {"ATOMS_SECRET_ENCRYPTION_KEY": Fernet.generate_key().decode()}):
                app = compose_application(settings)
            mail = MailStub()
            identity = app.services.identity
            identity.dependencies = replace(identity.dependencies, captcha=CaptchaStub(), mail=mail)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler_type(settings, app))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}/api/v1"

            def call(method, path, body=None, cookie="", csrf="", key=""):
                headers = {"Origin": settings.public_origin}
                if body is not None:
                    headers["Content-Type"] = "application/json"
                if cookie:
                    headers["Cookie"] = cookie
                if csrf:
                    headers["X-CSRF-Token"] = csrf
                if key:
                    headers["Idempotency-Key"] = key
                request = Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                  headers=headers, method=method)
                return urlopen(request)

            try:
                with self.assertRaises(HTTPError) as malformed:
                    call("POST", "/identity/check-email", {"email": None})
                self.assertEqual(malformed.exception.code, 422)
                with self.assertRaises(HTTPError) as terms:
                    call("POST", "/identity/register", {"email": "bad@example.com",
                                                         "password": "long-password-123", "captcha_token": "ok",
                                                         "accepted_terms": "false"})
                self.assertEqual(terms.exception.code, 422)
                with call("POST", "/identity/register", {"email": "http@example.com", "password": "long-password-123",
                                                          "captcha_token": "ok", "accepted_terms": True}) as response:
                    self.assertEqual(response.status, 201)
                with self.assertRaises(HTTPError) as result:
                    call("POST", "/identity/login", {"email": "http@example.com", "password": "long-password-123"})
                self.assertEqual(result.exception.code, 403)
                with call("POST", "/identity/verification", {"email": "http@example.com"}, key="verify"):
                    pass
                token = parse_qs(urlsplit(mail.url).fragment)["token"][0]
                with call("POST", "/identity/verify", {"token": token}) as response:
                    payload = json.load(response)
                    self.assertNotIn("id", payload)
                    raw_cookies = response.headers.get_all("Set-Cookie")
                    self.assertIn("HttpOnly", raw_cookies[0])
                parsed = SimpleCookie()
                for raw in raw_cookies:
                    parsed.load(raw)
                cookie = f"session={parsed['session'].value}; csrf_token={parsed['csrf_token'].value}"
                with self.assertRaises(HTTPError) as result:
                    call("POST", "/workspaces", {"name": "Team"}, cookie=cookie, key="create")
                self.assertEqual(result.exception.code, 403)
                with call("POST", "/workspaces", {"name": "Team"}, cookie=cookie,
                          csrf=parsed["csrf_token"].value, key="create") as response:
                    workspace = json.load(response)
                    self.assertEqual(response.status, 201)
                with call("POST", "/workspaces", {"name": "Team"}, cookie=cookie,
                          csrf=parsed["csrf_token"].value, key="create") as response:
                    self.assertEqual(json.load(response)["id"], workspace["id"])
                with self.assertRaises(HTTPError) as changed:
                    call("POST", "/workspaces", {"name": "Different"}, cookie=cookie,
                         csrf=parsed["csrf_token"].value, key="create")
                self.assertEqual(changed.exception.code, 409)
                with call("POST", f"/workspaces/{workspace['id']}/select", {}, cookie=cookie,
                          csrf=parsed["csrf_token"].value) as response:
                    self.assertEqual(response.status, 200)
                with call("GET", "/identity/me", cookie=cookie) as response:
                    self.assertEqual(json.load(response)["workspace_id"], workspace["id"])
                with self.assertRaises(HTTPError) as no_session:
                    call("GET", "/identity/me")
                self.assertEqual(no_session.exception.code, 401)
                with call("GET", "/workspaces/current/members", cookie=cookie) as response:
                    self.assertEqual(len(json.load(response)["items"]), 1)
                with self.assertRaises(HTTPError) as result:
                    call("GET", "/workspaces/current/members")
                self.assertEqual(result.exception.code, 401)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
