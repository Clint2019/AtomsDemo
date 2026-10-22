"""Identity ownership: credentials, sessions, verification and login OAuth state."""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from urllib.parse import quote

from atoms_demo.contracts.identity import (User, Credentials, Session, CurrentSession, EmailCheck,
                                           VerificationRequest, OAuthCallback, IdentityAPI)
from atoms_demo.core.errors import DomainError, ErrorCode
from atoms_demo.core.types import EntityId, Mutation, OperationReceipt, RequestContext, Scope, SecretRef
from atoms_demo.ports.dependencies import IdentityDependencies

_GLOBAL = Scope(workspace_id=None)
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


@dataclass(frozen=True)
class AccountRecord:
    id: EntityId
    email: str
    salt: str
    password_hash: str
    verified: bool
    revision: int = 0


@dataclass(frozen=True)
class CurrentEmailChange:
    id: EntityId  # User ID, unique across pending changes.
    verification_id: EntityId
    revision: int = 0


@dataclass(frozen=True)
class VerificationCooldown:
    id: EntityId  # Hash of the normalized address.
    next_allowed_at: datetime
    revision: int = 0


@dataclass(frozen=True)
class VerificationRequestRecord:
    id: EntityId
    email: str
    token_id: EntityId | None
    receipt_id: EntityId
    recorded_at: datetime
    started_at: datetime
    state: str
    revision: int = 0


@dataclass(frozen=True)
class EmailLookup:
    id: EntityId
    user_id: EntityId
    revision: int = 0


@dataclass(frozen=True)
class SessionRecord:
    id: EntityId
    user_id: EntityId
    workspace_id: EntityId | None
    expires_at: datetime
    revision: int = 0


@dataclass(frozen=True)
class VerificationRecord:
    id: EntityId
    user_id: EntityId
    email: str
    purpose: str
    expires_at: datetime
    revision: int = 0


@dataclass(frozen=True)
class OAuthTransaction:
    id: EntityId
    provider: str
    verifier_secret_id: EntityId
    binding_hash: EntityId
    expires_at: datetime
    revision: int = 0


@dataclass(frozen=True)
class OAuthLookup:
    id: EntityId
    user_id: EntityId
    revision: int = 0


def decode_session(data: dict) -> SessionRecord:
    return SessionRecord(**{**data, "expires_at": datetime.fromisoformat(data["expires_at"])})


def decode_verification(data: dict) -> VerificationRecord:
    return VerificationRecord(**{**data, "expires_at": datetime.fromisoformat(data["expires_at"])})


def decode_verification_cooldown(data: dict) -> VerificationCooldown:
    return VerificationCooldown(**{**data, "next_allowed_at": datetime.fromisoformat(data["next_allowed_at"])})


def decode_verification_request(data: dict) -> VerificationRequestRecord:
    return VerificationRequestRecord(**{**data,
        "recorded_at": datetime.fromisoformat(data["recorded_at"]),
        "started_at": datetime.fromisoformat(data["started_at"])})


def decode_oauth_transaction(data: dict) -> OAuthTransaction:
    return OAuthTransaction(**{**data, "expires_at": datetime.fromisoformat(data["expires_at"])})


def _hash(value: str) -> EntityId:
    return EntityId(hashlib.sha256(value.encode("utf-8")).hexdigest())


def _email(value: str) -> str:
    if not isinstance(value, str):
        raise DomainError(ErrorCode.INVALID_INPUT, "Invalid email address")
    normalized = value.strip().casefold()
    if len(normalized) > 254 or not _EMAIL.fullmatch(normalized):
        raise DomainError(ErrorCode.INVALID_INPUT, "Invalid email address")
    return normalized


def _user(account: AccountRecord) -> User:
    return User(id=account.id, email=account.email, verified=account.verified, revision=account.revision)


class IdentityService:
    def __init__(self, dependencies: IdentityDependencies) -> None:
        self.dependencies = dependencies

    async def me(self, ctx: RequestContext) -> CurrentSession:
        if ctx.actor_id is None or ctx.session_id is None:
            raise DomainError(ErrorCode.UNAUTHENTICATED, "Sign in required")
        return CurrentSession(user_id=ctx.actor_id, workspace_id=ctx.workspace_id)

    async def _account_for_email(self, uow, email: str) -> AccountRecord | None:
        lookup = await uow.repository(EmailLookup).get(_GLOBAL, _hash(email))
        return await uow.repository(AccountRecord).get(_GLOBAL, lookup.user_id) if lookup else None

    async def _new_session(self, uow, user_id: EntityId) -> Session:
        raw = secrets.token_urlsafe(48)
        expires = self.dependencies.clock.now() + timedelta(days=30)
        await uow.repository(SessionRecord).save(
            _GLOBAL, SessionRecord(id=_hash(raw), user_id=user_id, workspace_id=None, expires_at=expires), None)
        return Session(id=EntityId(raw), user_id=user_id, expires_at=expires)

    async def check_email(self, ctx: RequestContext, email: str) -> EmailCheck:
        normalized = _email(email)
        async with self.dependencies.uow() as uow:
            account = await self._account_for_email(uow, normalized)
        return EmailCheck(next_step="login" if account else "register")

    async def register(self, ctx: RequestContext, command: Credentials) -> User:
        email = _email(command.email)
        if (command.accepted_terms is not True or not isinstance(command.password, str) or
                len(command.password) < 12 or len(command.password) > 1024):
            raise DomainError(ErrorCode.INVALID_INPUT, "Accept terms and use a password of at least 12 characters")
        if (command.captcha_token is not None and not isinstance(command.captcha_token, str)):
            raise DomainError(ErrorCode.INVALID_INPUT, "Captcha token must be text")
        if not await self.dependencies.captcha.verify(command.captcha_token or "", ctx.request_id):
            raise DomainError(ErrorCode.INVALID_INPUT, "Captcha verification failed")
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(command.password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1).hex()
        account = AccountRecord(id=self.dependencies.ids.new(), email=email,
                                salt=salt.hex(), password_hash=digest, verified=False)
        async with self.dependencies.uow() as uow:
            if await self._account_for_email(uow, email):
                raise DomainError(ErrorCode.CONFLICT, "Email is already registered")
            account = await uow.repository(AccountRecord).save(_GLOBAL, account, None)
            await uow.repository(EmailLookup).save(
                _GLOBAL, EmailLookup(id=_hash(email), user_id=account.id), None)
            await uow.commit()
        return _user(account)

    async def login(self, ctx: RequestContext, command: Credentials) -> Session:
        email = _email(command.email)
        if not isinstance(command.password, str) or len(command.password) > 1024:
            raise DomainError(ErrorCode.UNAUTHENTICATED, "Invalid credentials")
        async with self.dependencies.uow() as uow:
            account = await self._account_for_email(uow, email)
            if not account or not account.password_hash:
                raise DomainError(ErrorCode.UNAUTHENTICATED, "Invalid credentials")
            actual = hashlib.scrypt(command.password.encode("utf-8"), salt=bytes.fromhex(account.salt),
                                    n=2**14, r=8, p=1).hex()
            if not hmac.compare_digest(actual, account.password_hash):
                raise DomainError(ErrorCode.UNAUTHENTICATED, "Invalid credentials")
            if not account.verified:
                raise DomainError(ErrorCode.FORBIDDEN, "Email verification required")
            session = await self._new_session(uow, account.id)
            await uow.commit()
        return session

    async def request_verification(self, ctx: RequestContext, command: VerificationRequest) -> OperationReceipt:
        email = _email(command.email)
        key = command.mutation.idempotency_key
        if not key or len(key) > 128:
            raise DomainError(ErrorCode.INVALID_INPUT, "Idempotency key is required")
        now = self.dependencies.clock.now()
        request_id = _hash(f"verification-request:{email}:{key}")
        raw = self.dependencies.secrets.fingerprint(f"verification-token:{email}:{key}")
        token_id = _hash(raw)
        cooldown_id = _hash(email)
        next_allowed_at = now + timedelta(minutes=1)
        async with self.dependencies.uow() as uow:
            request_repo = uow.repository(VerificationRequestRecord)
            cooldown_repo = uow.repository(VerificationCooldown)
            existing = await request_repo.get(_GLOBAL, request_id)
            if existing:
                receipt = OperationReceipt(id=existing.receipt_id, revision=1,
                                           recorded_at=existing.recorded_at)
                if existing.state == "sent":
                    return receipt
                if existing.started_at + timedelta(minutes=1) > now:
                    raise DomainError(ErrorCode.CONFLICT, "Verification request is still processing")
                previous = await cooldown_repo.get(_GLOBAL, cooldown_id)
                if previous and previous.next_allowed_at > now:
                    raise DomainError(ErrorCode.QUOTA_EXCEEDED,
                                      "Wait one minute before requesting another verification link")
                token = await uow.repository(VerificationRecord).get(_GLOBAL, token_id)
                account = await self._account_for_email(uow, email)
                send = bool(token and token.expires_at > now and account and not account.verified)
                await request_repo.save(_GLOBAL, replace(existing, started_at=now,
                    state="pending" if send else "sent"), existing.revision)
                if send:
                    await cooldown_repo.save(_GLOBAL, VerificationCooldown(id=cooldown_id,
                        next_allowed_at=next_allowed_at), previous.revision if previous else None)
            else:
                previous = await cooldown_repo.get(_GLOBAL, cooldown_id)
                if previous and previous.next_allowed_at > now:
                    raise DomainError(ErrorCode.QUOTA_EXCEEDED,
                                      "Wait one minute before requesting another verification link")
                account = await self._account_for_email(uow, email)
                send = bool(account and not account.verified)
                if send:
                    await uow.repository(VerificationRecord).save(
                        _GLOBAL, VerificationRecord(id=token_id, user_id=account.id, email=email,
                                                    purpose="register", expires_at=now + timedelta(hours=1)), None)
                receipt = OperationReceipt(id=self.dependencies.ids.new(), revision=1, recorded_at=now)
                await request_repo.save(_GLOBAL, VerificationRequestRecord(
                    id=request_id, email=email, token_id=token_id if send else None,
                    receipt_id=receipt.id, recorded_at=now, started_at=now,
                    state="pending" if send else "sent"), None)
                await cooldown_repo.save(_GLOBAL, VerificationCooldown(id=cooldown_id,
                    next_allowed_at=next_allowed_at), previous.revision if previous else None)
            await uow.commit()
        if send:
            url = f"{self.dependencies.public_origin}/verify#token={quote(raw)}"
            await self.dependencies.mail.send_verification(email, url, key)
            async with self.dependencies.uow() as uow:
                current = await uow.repository(VerificationRequestRecord).get(_GLOBAL, request_id)
                if current and current.state == "pending" and current.started_at == now:
                    await uow.repository(VerificationRequestRecord).save(
                        _GLOBAL, replace(current, state="sent"), current.revision)
                    await uow.commit()
        return receipt

    async def verify(self, ctx: RequestContext, token: str) -> Session:
        now = self.dependencies.clock.now()
        async with self.dependencies.uow() as uow:
            record = await uow.repository(VerificationRecord).get(_GLOBAL, _hash(token))
            if not record or record.expires_at <= now:
                raise DomainError(ErrorCode.INVALID_INPUT, "Verification token is invalid or expired")
            account = await uow.repository(AccountRecord).get(_GLOBAL, record.user_id)
            if not account:
                raise DomainError(ErrorCode.NOT_FOUND, "Account not found")
            if record.purpose == "email_change":
                current = await uow.repository(CurrentEmailChange).get(_GLOBAL, account.id)
                if current is None or current.verification_id != record.id:
                    raise DomainError(ErrorCode.INVALID_INPUT, "Email change request is no longer current")
                if await self._account_for_email(uow, record.email):
                    raise DomainError(ErrorCode.CONFLICT, "Email is already registered")
                await uow.repository(EmailLookup).delete(_GLOBAL, _hash(account.email), 1)
                await uow.repository(EmailLookup).save(
                    _GLOBAL, EmailLookup(id=_hash(record.email), user_id=account.id), None)
                account = AccountRecord(id=account.id, email=record.email, salt=account.salt,
                                        password_hash=account.password_hash, verified=True,
                                        revision=account.revision)
                await uow.repository(CurrentEmailChange).delete(_GLOBAL, current.id, current.revision)
            else:
                if record.purpose != "register" or account.verified:
                    raise DomainError(ErrorCode.INVALID_INPUT, "Verification token is no longer current")
                account = replace(account, verified=True)
            await uow.repository(AccountRecord).save(_GLOBAL, account, account.revision)
            await uow.repository(VerificationRecord).delete(_GLOBAL, record.id, record.revision)
            session = await self._new_session(uow, account.id)
            await uow.commit()
        return session

    async def oauth_start(self, ctx: RequestContext, provider: str) -> str:
        if not ctx.client_binding:
            raise DomainError(ErrorCode.FORBIDDEN, "OAuth browser binding required")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{1,30}", provider):
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid OAuth provider")
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        url = await self.dependencies.oauth.authorize_url(provider, state, challenge)
        verifier_ref = await self.dependencies.secrets.put(_GLOBAL, verifier)
        async with self.dependencies.uow() as uow:
            await uow.repository(OAuthTransaction).save(
                _GLOBAL, OAuthTransaction(id=_hash(state), provider=provider,
                                          verifier_secret_id=verifier_ref.id,
                                          binding_hash=_hash(ctx.client_binding),
                                          expires_at=self.dependencies.clock.now() + timedelta(minutes=10)), None)
            await uow.commit()
        return url

    async def oauth_callback(self, ctx: RequestContext, command: OAuthCallback) -> Session:
        async with self.dependencies.uow() as uow:
            txn = await uow.repository(OAuthTransaction).get(_GLOBAL, _hash(command.state))
            if (not txn or txn.provider != command.provider or
                    not ctx.client_binding or not hmac.compare_digest(str(txn.binding_hash),
                                                                       str(_hash(ctx.client_binding))) or
                    txn.expires_at <= self.dependencies.clock.now()):
                raise DomainError(ErrorCode.INVALID_INPUT, "OAuth state is invalid or expired")
            await uow.repository(OAuthTransaction).delete(_GLOBAL, txn.id, txn.revision)
            await uow.commit()
        verifier_ref = SecretRef(id=txn.verifier_secret_id)
        try:
            verifier = await self.dependencies.secrets.resolve(verifier_ref)
            identity = await self.dependencies.oauth.exchange(command.provider, command.code, verifier)
        finally:
            await self.dependencies.secrets.revoke(verifier_ref)
        if not identity.email or not identity.subject:
            raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "OAuth provider did not return an email")
        email = _email(identity.email)
        oauth_key = _hash(command.provider + ":" + identity.subject)
        async with self.dependencies.uow() as uow:
            lookup = await uow.repository(OAuthLookup).get(_GLOBAL, oauth_key)
            if lookup:
                account = await uow.repository(AccountRecord).get(_GLOBAL, lookup.user_id)
            else:
                if await self._account_for_email(uow, email):
                    raise DomainError(ErrorCode.CONFLICT, "Email is already linked to another sign-in method")
                account = await uow.repository(AccountRecord).save(
                    _GLOBAL, AccountRecord(id=self.dependencies.ids.new(), email=email,
                                           salt="", password_hash="", verified=False), None)
                await uow.repository(EmailLookup).save(_GLOBAL, EmailLookup(id=_hash(email), user_id=account.id), None)
                await uow.repository(OAuthLookup).save(_GLOBAL, OAuthLookup(id=oauth_key, user_id=account.id), None)
            if account is None:
                raise DomainError(ErrorCode.NOT_FOUND, "Account not found")
            session = await self._new_session(uow, account.id)
            await uow.commit()
        return session

    async def authenticate(self, session_token: str) -> RequestContext:
        async with self.dependencies.uow() as uow:
            record = await uow.repository(SessionRecord).get(_GLOBAL, _hash(session_token))
        if not record or record.expires_at <= self.dependencies.clock.now():
            raise DomainError(ErrorCode.UNAUTHENTICATED, "Session expired or invalid")
        return RequestContext(actor_id=record.user_id, session_id=record.id,
                              workspace_id=record.workspace_id, request_id=secrets.token_hex(16))

    async def logout(self, ctx: RequestContext) -> None:
        if ctx.session_id is None:
            return
        async with self.dependencies.uow() as uow:
            record = await uow.repository(SessionRecord).get(_GLOBAL, ctx.session_id)
            if record:
                await uow.repository(SessionRecord).delete(_GLOBAL, record.id, record.revision)
                await uow.commit()

    async def change_email(self, ctx: RequestContext, email: str, mutation: Mutation) -> User:
        if ctx.actor_id is None:
            raise DomainError(ErrorCode.UNAUTHENTICATED, "Sign in required")
        normalized = _email(email)
        raw = secrets.token_urlsafe(40)
        record_id = _hash(raw)
        async with self.dependencies.uow() as uow:
            account = await uow.repository(AccountRecord).get(_GLOBAL, ctx.actor_id)
            if not account:
                raise DomainError(ErrorCode.NOT_FOUND, "Account not found")
            if await self._account_for_email(uow, normalized):
                raise DomainError(ErrorCode.CONFLICT, "Email is already registered")
            await uow.repository(VerificationRecord).save(
                _GLOBAL, VerificationRecord(id=record_id, user_id=account.id, email=normalized,
                                            purpose="email_change",
                                            expires_at=self.dependencies.clock.now() + timedelta(hours=1)), None)
            change_repo = uow.repository(CurrentEmailChange)
            current = await change_repo.get(_GLOBAL, account.id)
            await change_repo.save(_GLOBAL, CurrentEmailChange(id=account.id,
                verification_id=record_id), current.revision if current else None)
            await uow.commit()
        url = f"{self.dependencies.public_origin}/verify#token={quote(raw)}"
        await self.dependencies.mail.send_verification(normalized, url, mutation.idempotency_key)
        return _user(account)
