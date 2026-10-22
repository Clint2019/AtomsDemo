from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from atoms_demo.core.types import (
    BlobRef, EntityId, Environment, Mutation, OperationReceipt,
    Page, PageQuery, RequestContext, Scope, SecretRef,
)
from atoms_demo.core.metering import StorageMetering

@dataclass(frozen=True, kw_only=True)
class Money:
    minor_units: int
    currency: str

class AccountKind(StrEnum):
    BUILD_CREDITS = "build_credits"
    CLOUD_WALLET = "cloud_wallet"

@dataclass(frozen=True, kw_only=True)
class Plan:
    id: EntityId
    name: str
    interval: str
    price: Money
    entitlements: tuple[str, ...]

@dataclass(frozen=True, kw_only=True)
class Subscription:
    id: EntityId
    workspace_id: EntityId
    plan_id: EntityId
    status: str
    provider_reference: str | None

@dataclass(frozen=True, kw_only=True)
class LedgerEntry:
    id: EntityId
    workspace_id: EntityId
    account: AccountKind
    units: int
    unit: str
    project_id: EntityId | None
    source_event_id: str
    recorded_at: datetime

@dataclass(frozen=True, kw_only=True)
class Balance:
    account: AccountKind
    available: int
    reserved: int
    unit: str

@dataclass(frozen=True, kw_only=True)
class Checkout:
    id: EntityId
    url: str
    mode: str

@dataclass(frozen=True, kw_only=True)
class UsageReservation:
    id: EntityId
    task_id: EntityId
    maximum_units: int
    account: AccountKind

@dataclass(frozen=True, kw_only=True)
class UsageCharge:
    reservation_id: EntityId
    consumed_units: int
    source_event_id: str

@dataclass(frozen=True, kw_only=True)
class Invoice:
    id: EntityId
    amount: Money
    status: str
    download_url: str | None

class BillingAPI(Protocol):
    async def plans(self, ctx: RequestContext, interval: str) -> tuple[Plan, ...]: ...
    async def subscription(self, ctx: RequestContext) -> Subscription | None: ...
    async def checkout(self, ctx: RequestContext, plan_id: EntityId, mutation: Mutation) -> Checkout: ...
    async def top_up(self, ctx: RequestContext, amount: Money, mutation: Mutation) -> Checkout: ...
    async def balances(self, ctx: RequestContext) -> tuple[Balance, ...]: ...
    async def ledger(self, ctx: RequestContext, account: AccountKind, page: PageQuery, project_id: EntityId | None = None) -> Page[LedgerEntry]: ...
    async def reserve(self, ctx: RequestContext, task_id: EntityId, maximum_units: int, mutation: Mutation, project_id: EntityId | None = None) -> UsageReservation: ...
    async def settle(self, ctx: RequestContext, charge: UsageCharge) -> OperationReceipt: ...
    async def release(self, ctx: RequestContext, reservation_id: EntityId) -> None: ...
    async def release_task(self, ctx: RequestContext, task_id: EntityId) -> None: ...
    async def release_interrupted(self, workspace_id: EntityId, task_id: EntityId) -> None: ...
    async def settle_interrupted(self, workspace_id: EntityId, task_id: EntityId,
                                 consumed_units: int) -> OperationReceipt: ...
    async def task_reservation_status(self, workspace_id: EntityId,
                                      task_id: EntityId) -> str | None: ...
    async def handle_webhook(self, body: bytes, signature: str) -> OperationReceipt: ...
    async def invoices(self, ctx: RequestContext, page: PageQuery) -> Page[Invoice]: ...
    async def record_storage_metering(self, ctx: RequestContext, event: StorageMetering,
                                      amount: Money | None) -> OperationReceipt: ...
    async def storage_metering_receipt(self, ctx: RequestContext, event: StorageMetering) -> OperationReceipt | None: ...
