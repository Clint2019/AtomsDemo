"""Replaceable login OAuth boundary; provider selection remains open."""
from atoms_demo.ports.providers import OAuthProvider, OAuthIdentity
from atoms_demo.config import Settings
from atoms_demo.core.errors import DomainError, ErrorCode


class UnconfiguredOAuthProvider:
    async def authorize_url(self, provider: str, state: str, challenge: str) -> str:
        raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "OAuth provider is not configured")

    async def exchange(self, provider: str, code: str, verifier: str) -> OAuthIdentity:
        raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "OAuth provider is not configured")

    async def revoke(self, provider: str, token: str) -> None:
        raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "OAuth provider is not configured")


def create_adapter(settings: Settings) -> OAuthProvider:
    return UnconfiguredOAuthProvider()
