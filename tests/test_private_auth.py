"""Private SSH-only authentication path; captured mail is not external delivery."""
import asyncio
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from cryptography.fernet import Fernet

from atoms_demo.bootstrap import compose_application
from atoms_demo.config import load_settings
from atoms_demo.adapters.captcha_provider import create_adapter as create_captcha_adapter
from atoms_demo.contracts.identity import Credentials, VerificationRequest
from atoms_demo.core.errors import DomainError, ErrorCode
from atoms_demo.core.types import Mutation, RequestContext


class PrivateAuthTests(unittest.TestCase):
    def _environment(self, root: Path) -> dict[str, str]:
        return {
            "ATOMS_DATABASE_URL": f"sqlite:///{root / 'platform.db'}",
            "ATOMS_BLOB_DIRECTORY": str(root / "blobs"),
            "ATOMS_PUBLIC_ORIGIN": "http://app.preview.localhost:8080",
            "ATOMS_PREVIEW_ORIGIN": "http://preview.localhost:8001",
            "ATOMS_HOST": "127.0.0.1",
            "ATOMS_PRIVATE_TEST_MODE": "true",
            "ATOMS_PRIVATE_MAIL_OUTBOX": str(root / "private-mail" / "outbox.jsonl"),
        }

    def test_private_registration_captures_verification_without_sending(self):
        async def run(root: Path):
            settings = load_settings(self._environment(root))
            with patch.dict(os.environ, {"ATOMS_SECRET_ENCRYPTION_KEY": Fernet.generate_key().decode()}):
                app = compose_application(settings)
            identity = app.services.identity
            anonymous = RequestContext(actor_id=None, session_id=None, workspace_id=None, request_id="private-auth")
            user = await identity.register(anonymous, Credentials(
                email="example@example.com", password="long-private-password",
                captcha_token=None, accepted_terms=True))
            self.assertFalse(user.verified)
            with self.assertRaises(DomainError) as invalid:
                await identity.register(anonymous, Credentials(
                    email="other@example.com", password="long-private-password",
                    captcha_token="invented", accepted_terms=True))
            self.assertEqual(invalid.exception.code, ErrorCode.INVALID_INPUT)
            await identity.request_verification(anonymous, VerificationRequest(
                email=user.email, mutation=Mutation(idempotency_key="private-verify")))
            outbox = settings.private_mail_outbox
            assert outbox is not None
            records = [json.loads(line) for line in outbox.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["address"], user.email)
            self.assertEqual(records[0]["kind"], "verification")
            token = parse_qs(urlsplit(records[0]["url"]).fragment)["token"][0]
            await identity.verify(anonymous, token)
            session = await identity.login(anonymous, Credentials(
                email=user.email, password="long-private-password",
                captcha_token=None, accepted_terms=True))
            self.assertEqual(session.user_id, user.id)

        with tempfile.TemporaryDirectory() as temporary:
            asyncio.run(run(Path(temporary)))

    def test_private_mode_rejects_nonlocal_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            env = self._environment(Path(temporary))
            env["ATOMS_PUBLIC_ORIGIN"] = "https://demo.example.com"
            env["ATOMS_PREVIEW_ORIGIN"] = "https://preview.example.com"
            env["ATOMS_PREVIEW_BIND_PORT"] = "8132"
            with self.assertRaisesRegex(ValueError, "Private test mode requires"):
                load_settings(env)
            env = self._environment(Path(temporary))
            env["ATOMS_HOST"] = "0.0.0.0"
            with self.assertRaisesRegex(ValueError, "Private test mode requires"):
                load_settings(env)
            env = self._environment(Path(temporary))
            env["ATOMS_DATABASE_URL"] = f"sqlite:///{Path(__file__).resolve().parents[1] / 'frontend' / 'dist' / 'platform.db'}"
            env["ATOMS_PRIVATE_MAIL_OUTBOX"] = str(Path(__file__).resolve().parents[1] /
                "frontend" / "dist" / "private-mail" / "outbox.jsonl")
            with self.assertRaisesRegex(ValueError, "Private test mode requires"):
                load_settings(env)
            settings = load_settings(self._environment(Path(temporary)))
            with self.assertRaisesRegex(ValueError, "Private test mode requires"):
                create_captcha_adapter(replace(settings, preview_bind_host="0.0.0.0"))

    def test_public_ip_test_origin_is_explicit_and_can_use_separate_preview_port(self):
        with tempfile.TemporaryDirectory() as temporary:
            env = self._environment(Path(temporary))
            env["ATOMS_PUBLIC_ORIGIN"] = "http://1.1.1.1:18080"
            with self.assertRaisesRegex(ValueError, "must use HTTPS"):
                load_settings(env)
            env["ATOMS_PUBLIC_IP_TEST_MODE"] = "true"
            settings = load_settings(env)
            self.assertEqual(settings.public_origin, "http://1.1.1.1:18080")
            self.assertEqual(settings.preview_origin, "http://preview.localhost:8001")
            env["ATOMS_PREVIEW_ORIGIN"] = "http://1.1.1.1:18001"
            env["ATOMS_PREVIEW_BIND_PORT"] = "8132"
            settings = load_settings(env)
            self.assertEqual(settings.preview_origin, "http://1.1.1.1:18001")
            env["ATOMS_PREVIEW_ORIGIN"] = "http://1.1.1.1:18080"
            with self.assertRaisesRegex(ValueError, "must differ"):
                load_settings(env)
            env["ATOMS_PREVIEW_ORIGIN"] = "http://1.1.1.1:18001"
            env["ATOMS_PUBLIC_ORIGIN"] = "http://10.0.0.1:18080"
            with self.assertRaisesRegex(ValueError, "must use HTTPS"):
                load_settings(env)
            env["ATOMS_PUBLIC_ORIGIN"] = "https://example.com"
            with self.assertRaises(ValueError):
                load_settings(env)
            env["ATOMS_PUBLIC_ORIGIN"] = "http://1.1.1.1:18080"
            env["ATOMS_PRIVATE_TEST_MODE"] = "false"
            with self.assertRaisesRegex(ValueError, "requires private test mode"):
                load_settings(env)

    def test_public_ip_test_verification_link_uses_exposed_origin(self):
        async def run(root: Path):
            env = self._environment(root)
            env["ATOMS_PUBLIC_ORIGIN"] = "http://1.1.1.1:18080"
            env["ATOMS_PUBLIC_IP_TEST_MODE"] = "true"
            settings = load_settings(env)
            with patch.dict(os.environ, {"ATOMS_SECRET_ENCRYPTION_KEY": Fernet.generate_key().decode()}):
                app = compose_application(settings)
            anonymous = RequestContext(actor_id=None, session_id=None,
                                       workspace_id=None, request_id="public-ip-test")
            await app.services.identity.register(anonymous, Credentials(
                email="ip-example@example.com", password="long-private-password",
                captcha_token=None, accepted_terms=True))
            await app.services.identity.request_verification(anonymous, VerificationRequest(
                email="ip-example@example.com", mutation=Mutation(idempotency_key="ip-verify")))
            outbox = settings.private_mail_outbox
            assert outbox is not None
            record = json.loads(outbox.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(urlsplit(record["url"]).netloc, "1.1.1.1:18080")

        with tempfile.TemporaryDirectory() as temporary:
            asyncio.run(run(Path(temporary)))

    def test_unconfigured_captcha_returns_provider_error(self):
        async def run(root: Path):
            env = self._environment(root)
            env["ATOMS_PRIVATE_TEST_MODE"] = "false"
            env.pop("ATOMS_PRIVATE_MAIL_OUTBOX")
            settings = load_settings(env)
            with patch.dict(os.environ, {"ATOMS_SECRET_ENCRYPTION_KEY": Fernet.generate_key().decode()}):
                app = compose_application(settings)
            anonymous = RequestContext(actor_id=None, session_id=None, workspace_id=None, request_id="no-captcha")
            with self.assertRaises(DomainError) as failed:
                await app.services.identity.register(anonymous, Credentials(
                    email="example@example.com", password="long-private-password",
                    captcha_token=None, accepted_terms=True))
            self.assertEqual(failed.exception.code, ErrorCode.PROVIDER_UNAVAILABLE)

        with tempfile.TemporaryDirectory() as temporary:
            asyncio.run(run(Path(temporary)))


if __name__ == "__main__":
    unittest.main()
