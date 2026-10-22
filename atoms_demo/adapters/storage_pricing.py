"""Pricing provider placeholder until the storage tariff is specified."""
from atoms_demo.config import Settings
from atoms_demo.core.errors import DomainError, ErrorCode
from atoms_demo.core.metering import StorageMetering
from atoms_demo.contracts.billing import Money
from atoms_demo.ports.providers import StoragePricing


class UnconfiguredStoragePricing:
    async def quote(self, event: StorageMetering) -> Money | None:
        raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Storage pricing is not configured")


def create_adapter(settings: Settings) -> StoragePricing:
    return UnconfiguredStoragePricing()
