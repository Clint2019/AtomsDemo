"""Identity module rules with an explicit mail/captcha test substitute."""
import asyncio
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from cryptography.fernet import Fernet

from atoms_demo.bootstrap import compose_application
from atoms_demo.config import Settings
from atoms_demo.contracts.identity import Credentials, VerificationRequest, OAuthCallback
from atoms_demo.ports.providers import OAuthIdentity
from atoms_demo.core.errors import DomainError, ErrorCode
from atoms_demo.core.types import Mutation, RequestContext


class CaptchaStub:
    async def verify(self, token, request_id):
        return token == "good"


class MailStub:
    def __init__(self):
        self.urls = []

    async def send_verification(self, address, url, idempotency_key):
        self.urls.append((address, url))
        return "test-message"


class FailingMail:
    async def send_verification(self, address, url, idempotency_key):
        raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Mail failed")


class DelayedFailMail:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def send_verification(self, address, url, idempotency_key):
        self.started.set()
        await self.release.wait()
        raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Delayed mail failure")


class TestClock:
    def __init__(self):
        self.current = datetime(2026, 10, 21, tzinfo=timezone.utc)

    def now(self):
        return self.current

    def advance(self, seconds):
        self.current += timedelta(seconds=seconds)


class OAuthStub:
    async def authorize_url(self, provider, state, challenge):
        return f"https://provider.example/authorize?state={state}&code_challenge={challenge}"

    async def exchange(self, provider, code, verifier):
        return OAuthIdentity(subject="subject-1", email="oauth@example.com",
                             access_token="test-access", refresh_token=None)

    async def revoke(self, provider, token):
        return None


class IdentityTests(unittest.TestCase):
    def test_old_failed_request_cannot_bypass_newer_cooldown(self):
        async def run():
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                settings = Settings(database_url=f"sqlite:///{root / 'db.sqlite'}",
                    blob_directory=root / "blobs", public_origin="http://localhost:5173",
                    preview_origin="http://localhost:8001", model_provider="",
                    model_secret_name="", task_timeout_seconds=180)
                with patch.dict(os.environ, {"ATOMS_SECRET_ENCRYPTION_KEY": Fernet.generate_key().decode()}):
                    app = compose_application(settings)
                identity = app.services.identity
                clock = TestClock()
                identity.dependencies = replace(identity.dependencies, captcha=CaptchaStub(),
                    mail=FailingMail(), clock=clock)
                anonymous = RequestContext(actor_id=None, session_id=None,
                    workspace_id=None, request_id="old-request")
                await identity.register(anonymous, Credentials(email="cooldown@example.com",
                    password="long-and-private-password", captcha_token="good", accepted_terms=True))
                first = VerificationRequest(email="cooldown@example.com",
                    mutation=Mutation(idempotency_key="failed-a"))
                with self.assertRaises(DomainError):
                    await identity.request_verification(anonymous, first)
                clock.advance(61)
                mail = MailStub()
                identity.dependencies = replace(identity.dependencies, mail=mail)
                await identity.request_verification(anonymous, VerificationRequest(
                    email="cooldown@example.com", mutation=Mutation(idempotency_key="new-b")))
                clock.advance(1)
                with self.assertRaises(DomainError) as throttled:
                    await identity.request_verification(anonymous, first)
                self.assertEqual(throttled.exception.code, ErrorCode.QUOTA_EXCEEDED)
                self.assertEqual(len(mail.urls), 1)
        asyncio.run(run())

    def test_late_mail_failure_cannot_remove_retried_link(self):
        async def run():
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                settings = Settings(database_url=f"sqlite:///{root / 'db.sqlite'}",
                    blob_directory=root / "blobs", public_origin="http://localhost:5173",
                    preview_origin="http://localhost:8001", model_provider="",
                    model_secret_name="", task_timeout_seconds=180)
                with patch.dict(os.environ, {"ATOMS_SECRET_ENCRYPTION_KEY": Fernet.generate_key().decode()}):
                    app = compose_application(settings)
                identity = app.services.identity
                clock = TestClock()
                delayed = DelayedFailMail()
                identity.dependencies = replace(identity.dependencies, captcha=CaptchaStub(),
                    mail=delayed, clock=clock)
                anonymous = RequestContext(actor_id=None, session_id=None,
                    workspace_id=None, request_id="late-mail")
                await identity.register(anonymous, Credentials(email="late@example.com",
                    password="long-and-private-password", captcha_token="good", accepted_terms=True))
                command = VerificationRequest(email="late@example.com",
                    mutation=Mutation(idempotency_key="same-request"))
                first = asyncio.create_task(identity.request_verification(anonymous, command))
                await delayed.started.wait()
                clock.advance(61)
                good = MailStub()
                identity.dependencies = replace(identity.dependencies, mail=good)
                receipt = await identity.request_verification(anonymous, command)
                delayed.release.set()
                with self.assertRaises(DomainError):
                    await first
                self.assertEqual(len(good.urls), 1)
                self.assertEqual(await identity.request_verification(anonymous, command), receipt)
                token = parse_qs(urlsplit(good.urls[0][1]).fragment)["token"][0]
                self.assertIsNotNone((await identity.verify(anonymous, token)).user_id)
        asyncio.run(run())

    def test_oauth_state_is_bound_to_browser_context(self):
        async def run():
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                settings = Settings(database_url=f"sqlite:///{root / 'db.sqlite'}", blob_directory=root / "blobs",
                                    public_origin="http://localhost:5173", preview_origin="http://localhost:8001",
                                    model_provider="", model_secret_name="", task_timeout_seconds=180)
                with patch.dict(os.environ, {"ATOMS_SECRET_ENCRYPTION_KEY": Fernet.generate_key().decode()}):
                    app = compose_application(settings)
                identity = app.services.identity
                identity.dependencies = replace(identity.dependencies, oauth=OAuthStub())
                browser_a = RequestContext(actor_id=None, session_id=None, workspace_id=None,
                                           request_id="a", client_binding="browser-a-secret")
                browser_b = replace(browser_a, request_id="b", client_binding="browser-b-secret")
                url = await identity.oauth_start(browser_a, "example")
                state = parse_qs(urlsplit(url).query)["state"][0]
                with self.assertRaises(DomainError) as crossed:
                    await identity.oauth_callback(browser_b, OAuthCallback(
                        provider="example", code="code", state=state))
                self.assertEqual(crossed.exception.code, ErrorCode.INVALID_INPUT)
                session = await identity.oauth_callback(browser_a, OAuthCallback(
                    provider="example", code="code", state=state))
                self.assertIsNotNone(session.user_id)
                with self.assertRaises(DomainError):
                    await identity.oauth_callback(browser_a, OAuthCallback(
                        provider="example", code="code", state=state))
        asyncio.run(run())

    def test_registration_verification_session_and_email_change(self):
        async def run():
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                settings = Settings(database_url=f"sqlite:///{root / 'db.sqlite'}",
                                    blob_directory=root / "blobs", public_origin="http://localhost:5173",
                                    preview_origin="http://localhost:8001", model_provider="",
                                    model_secret_name="", task_timeout_seconds=180)
                with patch.dict(os.environ, {"ATOMS_SECRET_ENCRYPTION_KEY": Fernet.generate_key().decode()}):
                    app = compose_application(settings)
                identity = app.services.identity
                mail = MailStub()
                clock = TestClock()
                identity.dependencies = replace(identity.dependencies, captcha=CaptchaStub(), mail=mail,
                                                clock=clock)
                anonymous = RequestContext(actor_id=None, session_id=None, workspace_id=None, request_id="test")
                user = await identity.register(anonymous, Credentials(
                    email=" Test@Example.com ", password="long-and-private-password",
                    captcha_token="good", accepted_terms=True))
                self.assertEqual(user.email, "test@example.com")
                self.assertFalse(user.verified)
                with self.assertRaises(DomainError) as unverified:
                    await identity.login(anonymous, Credentials(
                        email=user.email, password="long-and-private-password",
                        captcha_token=None, accepted_terms=True))
                self.assertEqual(unverified.exception.code, ErrorCode.FORBIDDEN)
                identity.dependencies = replace(identity.dependencies, mail=FailingMail())
                with self.assertRaises(DomainError) as mail_failure:
                    await identity.request_verification(anonymous, VerificationRequest(
                        email=user.email, mutation=Mutation(idempotency_key="failed-mail")))
                self.assertEqual(mail_failure.exception.code, ErrorCode.PROVIDER_UNAVAILABLE)
                with self.assertRaises(DomainError) as pending:
                    await identity.request_verification(anonymous, VerificationRequest(
                        email=user.email, mutation=Mutation(idempotency_key="failed-mail")))
                self.assertEqual(pending.exception.code, ErrorCode.CONFLICT)
                clock.advance(61)
                identity.dependencies = replace(identity.dependencies, mail=mail)
                receipt = await identity.request_verification(anonymous, VerificationRequest(
                    email=user.email, mutation=Mutation(idempotency_key="failed-mail")))
                self.assertEqual(len(mail.urls), 1)
                token = parse_qs(urlsplit(mail.urls[-1][1]).fragment)["token"][0]
                repeated = await identity.request_verification(anonymous, VerificationRequest(
                    email=user.email, mutation=Mutation(idempotency_key="failed-mail")))
                self.assertEqual(repeated, receipt)
                self.assertEqual(len(mail.urls), 1)
                with self.assertRaises(DomainError) as cooldown:
                    await identity.request_verification(anonymous, VerificationRequest(
                        email=user.email, mutation=Mutation(idempotency_key="verify-too-soon")))
                self.assertEqual(cooldown.exception.code, ErrorCode.QUOTA_EXCEEDED)
                unknown_receipt = await identity.request_verification(anonymous, VerificationRequest(
                    email="unknown@example.com", mutation=Mutation(idempotency_key="unknown-1")))
                self.assertEqual(await identity.request_verification(anonymous, VerificationRequest(
                    email="unknown@example.com", mutation=Mutation(idempotency_key="unknown-1"))),
                    unknown_receipt)
                with self.assertRaises(DomainError) as unknown_cooldown:
                    await identity.request_verification(anonymous, VerificationRequest(
                        email="unknown@example.com", mutation=Mutation(idempotency_key="unknown-2")))
                self.assertEqual(unknown_cooldown.exception.code, ErrorCode.QUOTA_EXCEEDED)
                clock.advance(61)
                self.assertEqual(await identity.request_verification(anonymous, VerificationRequest(
                    email=user.email, mutation=Mutation(idempotency_key="failed-mail"))), receipt)
                self.assertEqual(len(mail.urls), 1)
                await identity.request_verification(anonymous, VerificationRequest(
                    email=user.email, mutation=Mutation(idempotency_key="verify-2")))
                duplicate_registration_token = parse_qs(urlsplit(mail.urls[-1][1]).fragment)["token"][0]
                session = await identity.verify(anonymous, token)
                with self.assertRaises(DomainError):
                    await identity.verify(anonymous, token)
                with self.assertRaises(DomainError) as obsolete_registration:
                    await identity.verify(anonymous, duplicate_registration_token)
                self.assertEqual(obsolete_registration.exception.code, ErrorCode.INVALID_INPUT)
                authenticated = await identity.authenticate(str(session.id))
                self.assertEqual(authenticated.actor_id, user.id)
                with self.assertRaises(DomainError) as wrong:
                    await identity.login(anonymous, Credentials(
                        email=user.email, password="incorrect-password", captcha_token=None,
                        accepted_terms=True))
                self.assertEqual(wrong.exception.code, ErrorCode.UNAUTHENTICATED)
                before = await identity.change_email(authenticated, "old-request@example.com", Mutation(idempotency_key="change-1"))
                self.assertEqual(before.email, "test@example.com")
                with self.assertRaises(DomainError):
                    await identity.verify(anonymous, duplicate_registration_token)
                old_token = parse_qs(urlsplit(mail.urls[-1][1]).fragment)["token"][0]
                await identity.change_email(authenticated, "new@example.com", Mutation(idempotency_key="change-2"))
                token = parse_qs(urlsplit(mail.urls[-1][1]).fragment)["token"][0]
                with self.assertRaises(DomainError) as stale:
                    await identity.verify(anonymous, old_token)
                self.assertEqual(stale.exception.code, ErrorCode.INVALID_INPUT)
                await identity.verify(anonymous, token)
                with self.assertRaises(DomainError):
                    await identity.verify(anonymous, old_token)
                self.assertEqual((await identity.check_email(anonymous, "new@example.com")).next_step, "login")
                self.assertEqual((await identity.check_email(anonymous, "test@example.com")).next_step, "register")
                self.assertEqual((await identity.check_email(anonymous, "old-request@example.com")).next_step, "register")
                await identity.logout(authenticated)
                with self.assertRaises(DomainError):
                    await identity.authenticate(str(session.id))
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
