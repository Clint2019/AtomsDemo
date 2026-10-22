"""CAPTCHA boundary. Private bypass is only for loopback staging."""
import asyncio
import json
from urllib.error import URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from atoms_demo.ports.providers import CaptchaProvider
from atoms_demo.config import (
    Settings, TURNSTILE_TEST_SECRET_KEYS, TURNSTILE_TEST_SITE_KEYS,
    validate_private_test_settings,
)
from atoms_demo.core.errors import DomainError, ErrorCode


class UnconfiguredCaptchaProvider:
    async def verify(self, token: str, request_id: str) -> bool:
        raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Captcha provider is not configured")


class PrivateTestCaptchaProvider:
    async def verify(self, token: str, request_id: str) -> bool:
        # Staging has no external CAPTCHA challenge. Only the explicit empty value is accepted.
        return token == ""


class TurnstileCaptchaProvider:
    """Validate each registration token with Cloudflare Siteverify."""

    SITEVERIFY = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

    def __init__(self, secret_key: str, expected_hostname: str, test_mode: bool = False):
        self.secret_key = secret_key
        self.expected_hostname = expected_hostname
        self.test_mode = test_mode

    def _verify(self, token: str) -> bool:
        data = urlencode({"secret": self.secret_key, "response": token}).encode("ascii")
        request = Request(self.SITEVERIFY, data=data,
                          headers={"Content-Type": "application/x-www-form-urlencoded"},
                          method="POST")
        try:
            with urlopen(request, timeout=8) as response:
                if response.status != 200:
                    raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE,
                                      "Captcha provider is unavailable")
                raw = response.read(16_385)
            if len(raw) > 16_384:
                raise ValueError("Captcha provider response is too large")
            result = json.loads(raw)
        except (URLError, OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE,
                              "Captcha provider is unavailable") from exc
        if not isinstance(result, dict) or result.get("success") is not True:
            return False
        if self.test_mode:
            metadata = result.get("metadata")
            return isinstance(metadata, dict) and metadata.get("result_with_testing_key") is True
        return (result.get("hostname") == self.expected_hostname and
                result.get("action") == "register")

    async def verify(self, token: str, request_id: str) -> bool:
        if not token or len(token) > 2_048:
            return False
        return await asyncio.to_thread(self._verify, token)


def create_adapter(settings: Settings) -> CaptchaProvider:
    if settings.private_test_mode:
        validate_private_test_settings(settings)
    if settings.captcha_provider == "turnstile":
        if not settings.turnstile_site_key or not settings.turnstile_secret_key:
            raise ValueError("Turnstile requires a site key and secret key")
        test_keys = (settings.turnstile_site_key in TURNSTILE_TEST_SITE_KEYS and
                     settings.turnstile_secret_key in TURNSTILE_TEST_SECRET_KEYS)
        if settings.private_test_mode != test_keys:
            raise ValueError("Turnstile test keys require private test mode")
        return TurnstileCaptchaProvider(
            settings.turnstile_secret_key, urlsplit(settings.public_origin).hostname or "",
            test_mode=test_keys)
    if settings.private_test_mode:
        return PrivateTestCaptchaProvider()
    return UnconfiguredCaptchaProvider()
