"""Replaceable mail boundary with a restricted private-test capture outbox."""
import asyncio
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

from atoms_demo.ports.providers import MailProvider
from atoms_demo.config import Settings, validate_private_test_settings
from atoms_demo.core.errors import DomainError, ErrorCode


class UnconfiguredMailProvider:
    async def send_verification(self, address: str, verification_url: str, idempotency_key: str) -> str:
        raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Mail provider is not configured")

    async def send_invitation(self, address: str, invitation_url: str, idempotency_key: str) -> str:
        raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Mail provider is not configured")


class PrivateCaptureMailProvider:
    """Writes bearer links outside the release and blob trees; never sends mail."""

    def __init__(self, outbox: Path):
        self.outbox = outbox

    def _capture(self, kind: str, address: str, url: str, idempotency_key: str) -> None:
        parent = self.outbox.parent
        parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if parent.is_symlink() or (os.name == "posix" and stat.S_IMODE(parent.stat().st_mode) & 0o077):
            raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Private mail outbox directory is not restricted")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.outbox, flags, 0o600)
            try:
                mode = os.fstat(descriptor).st_mode
                if not stat.S_ISREG(mode) or (os.name == "posix" and stat.S_IMODE(mode) & 0o077):
                    raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Private mail outbox file is not restricted")
                record = {"kind": kind, "address": address, "url": url,
                          "idempotency_key": idempotency_key,
                          "created_at": datetime.now(timezone.utc).isoformat()}
                os.write(descriptor, (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Private mail outbox is unavailable") from exc

    async def send_verification(self, address: str, verification_url: str, idempotency_key: str) -> str:
        await asyncio.to_thread(self._capture, "verification", address, verification_url, idempotency_key)
        return idempotency_key

    async def send_invitation(self, address: str, invitation_url: str, idempotency_key: str) -> str:
        await asyncio.to_thread(self._capture, "invitation", address, invitation_url, idempotency_key)
        return idempotency_key


def create_adapter(settings: Settings) -> MailProvider:
    if settings.private_test_mode:
        validate_private_test_settings(settings)
        assert settings.private_mail_outbox is not None
        return PrivateCaptureMailProvider(settings.private_mail_outbox)
    return UnconfiguredMailProvider()
