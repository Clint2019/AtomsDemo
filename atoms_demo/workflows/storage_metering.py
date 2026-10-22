"""Retryable storage-to-billing outbox consumer; never exposed as a browser route."""
from atoms_demo.contracts.billing import BillingAPI
from atoms_demo.contracts.storage import StorageAPI
from atoms_demo.core.types import RequestContext
from atoms_demo.ports.providers import StoragePricing


class StorageMeteringWorkflow:
    def __init__(self, storage: StorageAPI, billing: BillingAPI, pricing: StoragePricing) -> None:
        self.storage = storage
        self.billing = billing
        self.pricing = pricing

    async def flush(self, ctx: RequestContext) -> int:
        events = await self.storage.pending_metering(ctx)
        count = 0
        for event in events:
            receipt = await self.billing.storage_metering_receipt(ctx, event)
            if receipt is None:
                amount = await self.pricing.quote(event) if event.billable and event.byte_delta > 0 else None
                await self.billing.record_storage_metering(ctx, event, amount)
            await self.storage.mark_metered(ctx, event.event_id)
            count += 1
        return count
