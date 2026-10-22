from atoms_demo.ports.providers import PaymentProvider
from atoms_demo.config import Settings
from atoms_demo.core.errors import DomainError, ErrorCode
from atoms_demo.contracts.billing import Money, Checkout
from atoms_demo.ports.providers import VerifiedPaymentEvent


class UnconfiguredPaymentProvider:
    async def ensure_configured(self) -> None:
        raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Payment provider is not configured")

    async def checkout(self, reference: str, amount: Money, idempotency_key: str) -> Checkout:
        raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Payment provider is not configured")

    async def verify_webhook(self, body: bytes, signature: str) -> VerifiedPaymentEvent:
        raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Payment provider is not configured")


def create_adapter(settings: Settings) -> PaymentProvider:
    return UnconfiguredPaymentProvider()
