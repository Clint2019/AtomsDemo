"""Integer-unit ledger, reservations and verified payment settlement."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from datetime import datetime

from atoms_demo.contracts.billing import (Money, AccountKind, Plan, Subscription, LedgerEntry,
    Balance, Checkout, UsageReservation, UsageCharge, Invoice)
from atoms_demo.core.errors import DomainError, ErrorCode
from atoms_demo.core.types import EntityId, Mutation, OperationReceipt, Page, PageQuery, RequestContext, Scope
from atoms_demo.core.metering import StorageMetering
from atoms_demo.ports.dependencies import BillingDependencies

GLOBAL = Scope(workspace_id=None)


@dataclass(frozen=True)
class CheckoutRecord:
    id: EntityId
    workspace_id: EntityId
    kind: str
    plan_id: EntityId | None
    amount: Money
    provider_id: EntityId
    url: str
    status: str
    granted_credits: int = 0
    revision: int = 0


@dataclass(frozen=True)
class PaymentEventRecord:
    id: EntityId
    checkout_id: EntityId
    status: str
    recorded_at: datetime
    revision: int = 0


@dataclass(frozen=True)
class LedgerRecord:
    id: EntityId
    workspace_id: EntityId
    account: AccountKind
    units: int
    unit: str
    target_project_id: EntityId | None
    source_event_id: str
    recorded_at: datetime
    revision: int = 0


@dataclass(frozen=True)
class ReservationRecord:
    id: EntityId
    workspace_id: EntityId
    task_id: EntityId
    target_project_id: EntityId | None
    maximum_units: int
    account: AccountKind
    status: str
    consumed_units: int = 0
    source_event_id: str | None = None
    revision: int = 0


@dataclass(frozen=True)
class InvoiceRecord:
    id: EntityId
    workspace_id: EntityId
    amount: Money
    status: str
    download_url: str | None
    revision: int = 0


@dataclass(frozen=True)
class SubscriptionRecord:
    id: EntityId
    workspace_id: EntityId
    plan_id: EntityId
    status: str
    provider_reference: str | None
    revision: int = 0


@dataclass(frozen=True)
class WalletCurrency:
    id: EntityId
    workspace_id: EntityId
    currency: str
    revision: int = 0


@dataclass(frozen=True)
class StorageMeasurement:
    id: EntityId
    workspace_id: EntityId
    target_project_id: EntityId | None
    byte_delta: int
    occurred_at: datetime
    charge_minor_units: int = 0
    currency: str | None = None
    revision: int = 0


def decode_storage_measurement(data: dict) -> StorageMeasurement:
    return StorageMeasurement(**{**data, "occurred_at": datetime.fromisoformat(data["occurred_at"])})


def decode_checkout(data: dict) -> CheckoutRecord:
    return CheckoutRecord(**{**{"granted_credits": 0, **data}, "amount": Money(**data["amount"]),
                             "provider_id": EntityId(data["provider_id"])})


def decode_payment_event(data: dict) -> PaymentEventRecord:
    return PaymentEventRecord(**{**data, "recorded_at": datetime.fromisoformat(data["recorded_at"])})


def decode_ledger(data: dict) -> LedgerRecord:
    return LedgerRecord(**{**data, "account": AccountKind(data["account"]),
                           "recorded_at": datetime.fromisoformat(data["recorded_at"])})


def decode_reservation(data: dict) -> ReservationRecord:
    return ReservationRecord(**{**data, "account": AccountKind(data["account"])})


def decode_invoice(data: dict) -> InvoiceRecord:
    return InvoiceRecord(**{**data, "amount": Money(**data["amount"])})


def _hash(value: str) -> EntityId:
    return EntityId(hashlib.sha256(value.encode()).hexdigest())


def _money(value: Money) -> None:
    if type(value.minor_units) is not int or value.minor_units <= 0 or not re.fullmatch(r"[A-Z]{3}", value.currency):
        raise DomainError(ErrorCode.INVALID_INPUT, "Amount must be positive integer minor units and ISO currency")


class BillingService:
    def __init__(self, dependencies: BillingDependencies) -> None:
        self.dependencies = dependencies

    async def _scope(self, ctx: RequestContext, manage: bool = False) -> Scope:
        if ctx.actor_id is None:
            raise DomainError(ErrorCode.UNAUTHENTICATED, "Sign in required")
        if ctx.workspace_id is None:
            raise DomainError(ErrorCode.FORBIDDEN, "Workspace selection required")
        await self.dependencies.authorization.require(ctx, "workspace:manage" if manage else "workspace:read",
                                                      ctx.workspace_id)
        return Scope(workspace_id=ctx.workspace_id)

    async def _all(self, uow, entity_type, scope: Scope):
        cursor = None
        items = []
        while True:
            page = await uow.repository(entity_type).list(scope, PageQuery(cursor=cursor, limit=100))
            items.extend(page.items)
            if page.next_cursor is None:
                return items
            cursor = page.next_cursor

    async def plans(self, ctx: RequestContext, interval: str) -> tuple[Plan, ...]:
        if interval not in {"month", "year"}:
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid plan interval")
        return tuple(plan for plan in self.dependencies.plans if plan.interval == interval)

    async def subscription(self, ctx: RequestContext) -> Subscription | None:
        scope = await self._scope(ctx)
        async with self.dependencies.uow() as uow:
            record = await uow.repository(SubscriptionRecord).get(scope, ctx.workspace_id)
        return Subscription(id=record.id, workspace_id=record.workspace_id, plan_id=record.plan_id,
                            status=record.status, provider_reference=record.provider_reference) if record else None

    async def _checkout(self, ctx: RequestContext, kind: str, plan_id: EntityId | None,
                        amount: Money, mutation: Mutation, granted_credits: int = 0) -> Checkout:
        scope = await self._scope(ctx, True)
        _money(amount)
        if not mutation.idempotency_key:
            raise DomainError(ErrorCode.INVALID_INPUT, "Idempotency key required")
        reference = _hash(f"checkout:{ctx.workspace_id}:{ctx.actor_id}:{kind}:{mutation.idempotency_key}")
        async with self.dependencies.uow() as uow:
            existing = await uow.repository(CheckoutRecord).get(scope, reference)
            if existing:
                if existing.kind != kind or existing.plan_id != plan_id or existing.amount != amount or existing.granted_credits != granted_credits:
                    raise DomainError(ErrorCode.CONFLICT, "Idempotency key was used with different checkout")
                return Checkout(id=existing.provider_id, url=existing.url, mode=kind)
            await self.dependencies.payments.ensure_configured()
            if kind == "top_up":
                currency_repo = uow.repository(WalletCurrency)
                policy = await currency_repo.get(scope, ctx.workspace_id)
                if policy and policy.currency != amount.currency:
                    raise DomainError(ErrorCode.CONFLICT, "Wallet currency is fixed for this workspace")
                if policy is None:
                    await currency_repo.save(scope, WalletCurrency(id=ctx.workspace_id,
                        workspace_id=ctx.workspace_id, currency=amount.currency), None)
                    await uow.commit()
        remote = await self.dependencies.payments.checkout(str(reference), amount, str(reference))
        if not remote.url.startswith("https://"):
            raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Payment provider returned an invalid checkout URL")
        async with self.dependencies.uow() as uow:
            existing = await uow.repository(CheckoutRecord).get(scope, reference)
            if existing:
                if existing.kind != kind or existing.plan_id != plan_id or existing.amount != amount or existing.granted_credits != granted_credits:
                    raise DomainError(ErrorCode.CONFLICT, "Idempotency key was used with different checkout")
                return Checkout(id=existing.provider_id, url=existing.url, mode=kind)
            await uow.repository(CheckoutRecord).save(scope, CheckoutRecord(
                id=reference, workspace_id=ctx.workspace_id, kind=kind, plan_id=plan_id,
                amount=amount, provider_id=remote.id, url=remote.url, status="pending",
                granted_credits=granted_credits), None)
            await uow.repository(CheckoutLocator).save(GLOBAL, CheckoutLocator(
                id=reference, target_workspace_id=ctx.workspace_id), None)
            await uow.commit()
        return Checkout(id=remote.id, url=remote.url, mode=kind)

    async def checkout(self, ctx: RequestContext, plan_id: EntityId, mutation: Mutation) -> Checkout:
        scope = await self._scope(ctx, True)
        reference = _hash(f"checkout:{ctx.workspace_id}:{ctx.actor_id}:subscription:{mutation.idempotency_key}")
        async with self.dependencies.uow() as uow:
            existing = await uow.repository(CheckoutRecord).get(scope, reference)
        if existing:
            if existing.plan_id != plan_id:
                raise DomainError(ErrorCode.CONFLICT, "Idempotency key was used with another plan")
            return Checkout(id=existing.provider_id, url=existing.url, mode="subscription")
        plan = next((plan for plan in self.dependencies.plans if plan.id == plan_id), None)
        if plan is None:
            raise DomainError(ErrorCode.NOT_FOUND, "Plan not configured")
        credits = [int(value.split(":", 1)[1]) for value in plan.entitlements
                   if re.fullmatch(r"credits:[1-9][0-9]*", value)]
        return await self._checkout(ctx, "subscription", plan.id, plan.price, mutation, sum(credits))

    async def top_up(self, ctx: RequestContext, amount: Money, mutation: Mutation) -> Checkout:
        return await self._checkout(ctx, "top_up", None, amount, mutation)

    async def _balance(self, uow, scope: Scope, account: AccountKind) -> Balance:
        if account == AccountKind.BUILD_CREDITS:
            await self._ensure_initial_credits(uow, scope)
        entries = [item for item in await self._all(uow, LedgerRecord, scope) if item.account == account]
        units = {item.unit for item in entries}
        if len(units) > 1:
            raise DomainError(ErrorCode.CONFLICT, "Account contains incompatible units")
        available_total = sum(item.units for item in entries)
        reservations = [item for item in await self._all(uow, ReservationRecord, scope)
                        if item.account == account and item.status == "reserved"]
        reserved = sum(item.maximum_units for item in reservations)
        return Balance(account=account, available=available_total - reserved,
                       reserved=reserved, unit=next(iter(units), "credit" if account == AccountKind.BUILD_CREDITS else "minor_unit"))

    async def _ensure_initial_credits(self, uow, scope: Scope) -> None:
        amount = self.dependencies.initial_build_credits
        if amount <= 0 or scope.workspace_id is None:
            return
        entry_id = _hash(f"initial-credit:{scope.workspace_id}")
        repository = uow.repository(LedgerRecord)
        if await repository.get(scope, entry_id) is None:
            await repository.save(scope, LedgerRecord(
                id=entry_id, workspace_id=scope.workspace_id,
                account=AccountKind.BUILD_CREDITS, units=amount, unit="credit",
                target_project_id=None, source_event_id=f"initial-credit:{scope.workspace_id}",
                recorded_at=self.dependencies.clock.now()), None)

    async def balances(self, ctx: RequestContext) -> tuple[Balance, ...]:
        scope = await self._scope(ctx)
        async with self.dependencies.uow() as uow:
            result = tuple([await self._balance(uow, scope, kind) for kind in AccountKind])
            await uow.commit()
            return result

    async def ledger(self, ctx: RequestContext, account: AccountKind, page: PageQuery,
                     project_id: EntityId | None = None) -> Page[LedgerEntry]:
        scope = await self._scope(ctx)
        if project_id:
            await self.dependencies.authorization.require(ctx, "project:history", project_id)
        if not 1 <= page.limit <= 100:
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid page limit")
        async with self.dependencies.uow() as uow:
            if account == AccountKind.BUILD_CREDITS:
                await self._ensure_initial_credits(uow, scope)
            all_entries = [item for item in await self._all(uow, LedgerRecord, scope)
                           if item.account == account and (project_id is None or item.target_project_id == project_id)]
            await uow.commit()
        all_entries.sort(key=lambda item: (item.recorded_at, str(item.id)))
        if page.cursor:
            try:
                start = next(i + 1 for i, item in enumerate(all_entries) if str(item.id) == page.cursor)
            except StopIteration:
                raise DomainError(ErrorCode.INVALID_INPUT, "Invalid ledger cursor") from None
        else:
            start = 0
        selected = all_entries[start:start + page.limit]
        return Page(items=tuple(LedgerEntry(id=item.id, workspace_id=item.workspace_id,
                    account=item.account, units=item.units, unit=item.unit,
                    project_id=item.target_project_id, source_event_id=item.source_event_id,
                    recorded_at=item.recorded_at) for item in selected),
                    next_cursor=str(selected[-1].id) if start + page.limit < len(all_entries) else None)

    async def reserve(self, ctx: RequestContext, task_id: EntityId, maximum_units: int,
                      mutation: Mutation, project_id: EntityId | None = None) -> UsageReservation:
        scope = await self._scope(ctx)
        if project_id:
            await self.dependencies.authorization.require(ctx, "project:write", project_id)
        if type(maximum_units) is not int or maximum_units < 0 or not mutation.idempotency_key:
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid usage reservation")
        reservation_id = _hash(f"reserve:{ctx.workspace_id}:{task_id}")
        async with self.dependencies.uow() as uow:
            repo = uow.repository(ReservationRecord)
            prior = await repo.get(scope, reservation_id)
            if prior:
                if prior.maximum_units != maximum_units or prior.target_project_id != project_id:
                    raise DomainError(ErrorCode.CONFLICT, "Task reservation differs")
                return UsageReservation(id=prior.id, task_id=task_id, maximum_units=prior.maximum_units,
                                        account=prior.account)
            balance = await self._balance(uow, scope, AccountKind.BUILD_CREDITS)
            if balance.available < maximum_units:
                raise DomainError(ErrorCode.QUOTA_EXCEEDED, "Insufficient build credits")
            record = ReservationRecord(id=reservation_id, workspace_id=ctx.workspace_id,
                task_id=task_id, target_project_id=project_id, maximum_units=maximum_units,
                account=AccountKind.BUILD_CREDITS, status="reserved")
            await repo.save(scope, record, None)
            await uow.commit()
        return UsageReservation(id=reservation_id, task_id=task_id,
                                maximum_units=maximum_units, account=AccountKind.BUILD_CREDITS)

    async def settle(self, ctx: RequestContext, charge: UsageCharge) -> OperationReceipt:
        scope = await self._scope(ctx)
        return await self._settle(scope, charge)

    async def _settle(self, scope: Scope, charge: UsageCharge) -> OperationReceipt:
        if type(charge.consumed_units) is not int or charge.consumed_units < 0 or not charge.source_event_id:
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid usage charge")
        async with self.dependencies.uow() as uow:
            repo = uow.repository(ReservationRecord)
            reservation = await repo.get(scope, charge.reservation_id)
            if reservation is None:
                raise DomainError(ErrorCode.NOT_FOUND, "Reservation not found")
            if reservation.status == "settled" and reservation.consumed_units == charge.consumed_units and reservation.source_event_id == charge.source_event_id:
                return OperationReceipt(id=reservation.id, revision=reservation.revision,
                                        recorded_at=self.dependencies.clock.now())
            if reservation.status != "reserved" or charge.consumed_units > reservation.maximum_units:
                raise DomainError(ErrorCode.CONFLICT, "Reservation cannot be settled")
            entry = LedgerRecord(id=_hash(f"usage:{charge.source_event_id}"), workspace_id=scope.workspace_id,
                account=reservation.account, units=-charge.consumed_units, unit="credit",
                target_project_id=reservation.target_project_id,
                source_event_id=charge.source_event_id, recorded_at=self.dependencies.clock.now())
            await uow.repository(LedgerRecord).save(scope, entry, None)
            saved = await repo.save(scope, replace(reservation, status="settled",
                consumed_units=charge.consumed_units, source_event_id=charge.source_event_id), reservation.revision)
            await uow.commit()
        return OperationReceipt(id=saved.id, revision=saved.revision, recorded_at=entry.recorded_at)

    async def settle_interrupted(self, workspace_id: EntityId, task_id: EntityId,
                                 consumed_units: int) -> OperationReceipt:
        return await self._settle(Scope(workspace_id=workspace_id), UsageCharge(
            reservation_id=_hash(f"reserve:{workspace_id}:{task_id}"),
            consumed_units=consumed_units, source_event_id=f"task:{task_id}:usage"))

    async def task_reservation_status(self, workspace_id: EntityId,
                                      task_id: EntityId) -> str | None:
        scope = Scope(workspace_id=workspace_id)
        reservation_id = _hash(f"reserve:{workspace_id}:{task_id}")
        async with self.dependencies.uow() as uow:
            record = await uow.repository(ReservationRecord).get(scope, reservation_id)
        return record.status if record else None

    async def release(self, ctx: RequestContext, reservation_id: EntityId) -> None:
        scope = await self._scope(ctx)
        async with self.dependencies.uow() as uow:
            repo = uow.repository(ReservationRecord)
            reservation = await repo.get(scope, reservation_id)
            if reservation is None:
                raise DomainError(ErrorCode.NOT_FOUND, "Reservation not found")
            if reservation.status != "reserved":
                return
            await repo.save(scope, replace(reservation, status="released"), reservation.revision)
            await uow.commit()

    async def release_task(self, ctx: RequestContext, task_id: EntityId) -> None:
        await self._scope(ctx)
        await self.release_interrupted(ctx.workspace_id, task_id)

    async def release_interrupted(self, workspace_id: EntityId, task_id: EntityId) -> None:
        scope = Scope(workspace_id=workspace_id)
        reservation_id = _hash(f"reserve:{workspace_id}:{task_id}")
        async with self.dependencies.uow() as uow:
            repo = uow.repository(ReservationRecord)
            reservation = await repo.get(scope, reservation_id)
            if reservation is None or reservation.status != "reserved":
                return
            await repo.save(scope, replace(reservation, status="released"), reservation.revision)
            await uow.commit()

    async def record_storage_metering(self, ctx: RequestContext, event: StorageMetering,
                                      amount: Money | None) -> OperationReceipt:
        scope = await self._scope(ctx, True)
        if event.workspace_id != ctx.workspace_id or type(event.byte_delta) is not int or event.byte_delta == 0:
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid storage metering event")
        if amount is not None:
            _money(amount)
            if event.byte_delta < 0:
                raise DomainError(ErrorCode.INVALID_INPUT, "Deletion cannot create a charge")
        if event.project_id:
            await self.dependencies.authorization.require(ctx, "project:history", event.project_id)
        async with self.dependencies.uow() as uow:
            repo = uow.repository(StorageMeasurement)
            prior = await repo.get(scope, event.event_id)
            if prior:
                if (prior.target_project_id != event.project_id or prior.byte_delta != event.byte_delta or
                    prior.occurred_at != event.occurred_at or prior.charge_minor_units != (amount.minor_units if amount else 0) or
                    prior.currency != (amount.currency if amount else None)):
                    raise DomainError(ErrorCode.CONFLICT, "Storage metering event changed")
                return OperationReceipt(id=prior.id, revision=prior.revision, recorded_at=prior.occurred_at)
            if amount:
                policy = await uow.repository(WalletCurrency).get(scope, ctx.workspace_id)
                if policy is None or policy.currency != amount.currency:
                    raise DomainError(ErrorCode.CONFLICT, "Wallet currency does not match storage charge")
                balance = await self._balance(uow, scope, AccountKind.CLOUD_WALLET)
                if balance.available < amount.minor_units:
                    raise DomainError(ErrorCode.QUOTA_EXCEEDED, "Insufficient wallet balance")
                await uow.repository(LedgerRecord).save(scope, LedgerRecord(
                    id=_hash(f"storage-charge:{event.event_id}"), workspace_id=ctx.workspace_id,
                    account=AccountKind.CLOUD_WALLET, units=-amount.minor_units,
                    unit=f"{amount.currency}_minor", target_project_id=event.project_id,
                    source_event_id=f"storage:{event.event_id}", recorded_at=self.dependencies.clock.now()), None)
            saved = await repo.save(scope, StorageMeasurement(id=event.event_id,
                workspace_id=event.workspace_id, target_project_id=event.project_id,
                byte_delta=event.byte_delta, occurred_at=event.occurred_at,
                charge_minor_units=amount.minor_units if amount else 0,
                currency=amount.currency if amount else None), None)
            await uow.commit()
        return OperationReceipt(id=saved.id, revision=saved.revision, recorded_at=saved.occurred_at)

    async def storage_metering_receipt(self, ctx: RequestContext,
                                       event: StorageMetering) -> OperationReceipt | None:
        scope = await self._scope(ctx, True)
        if event.workspace_id != ctx.workspace_id:
            raise DomainError(ErrorCode.FORBIDDEN, "Different workspace")
        async with self.dependencies.uow() as uow:
            record = await uow.repository(StorageMeasurement).get(scope, event.event_id)
        if record is None:
            return None
        if (record.target_project_id != event.project_id or record.byte_delta != event.byte_delta or
                record.occurred_at != event.occurred_at):
            raise DomainError(ErrorCode.CONFLICT, "Storage metering event changed")
        return OperationReceipt(id=record.id, revision=record.revision, recorded_at=record.occurred_at)

    async def handle_webhook(self, body: bytes, signature: str) -> OperationReceipt:
        event = await self.dependencies.payments.verify_webhook(body, signature)
        if not event.provider_event_id or event.status not in {"paid", "failed", "pending"}:
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid verified payment event")
        event_id = _hash(f"payment:{event.provider_event_id}")
        checkout_id = EntityId(event.checkout_reference)
        async with self.dependencies.uow() as uow:
            prior = await uow.repository(PaymentEventRecord).get(GLOBAL, event_id)
            if prior:
                if prior.checkout_id != checkout_id or prior.status != event.status:
                    raise DomainError(ErrorCode.CONFLICT, "Payment event changed")
                return OperationReceipt(id=prior.id, revision=prior.revision, recorded_at=prior.recorded_at)
            # Checkout workspace is found through a billing-owned global locator.
            locator = await uow.repository(CheckoutLocator).get(GLOBAL, checkout_id)
            if locator is None:
                raise DomainError(ErrorCode.NOT_FOUND, "Checkout not found")
            scope = Scope(workspace_id=locator.target_workspace_id)
            repo = uow.repository(CheckoutRecord)
            checkout = await repo.get(scope, checkout_id)
            if checkout is None or checkout.amount != event.amount:
                raise DomainError(ErrorCode.INVALID_INPUT, "Payment amount does not match checkout")
            if event.status == "paid" and checkout.status != "paid":
                if checkout.kind == "top_up":
                    previous = [item for item in await self._all(uow, LedgerRecord, scope)
                                if item.account == AccountKind.CLOUD_WALLET]
                    policy = await uow.repository(WalletCurrency).get(scope, locator.target_workspace_id)
                    if policy is None or policy.currency != checkout.amount.currency:
                        raise DomainError(ErrorCode.CONFLICT, "Wallet currency configuration differs")
                    if previous and any(item.unit != f"{checkout.amount.currency}_minor" for item in previous):
                        raise DomainError(ErrorCode.CONFLICT, "Wallet currency differs")
                    await uow.repository(LedgerRecord).save(scope, LedgerRecord(
                        id=_hash(f"topup:{checkout.id}"), workspace_id=locator.target_workspace_id,
                        account=AccountKind.CLOUD_WALLET, units=checkout.amount.minor_units,
                        unit=f"{checkout.amount.currency}_minor", target_project_id=None,
                        source_event_id=event.provider_event_id,
                        recorded_at=self.dependencies.clock.now()), None)
                else:
                    if checkout.granted_credits:
                        await uow.repository(LedgerRecord).save(scope, LedgerRecord(
                            id=_hash(f"plan-credit:{checkout.id}"), workspace_id=locator.target_workspace_id,
                            account=AccountKind.BUILD_CREDITS, units=checkout.granted_credits, unit="credit",
                            target_project_id=None, source_event_id=event.provider_event_id,
                            recorded_at=self.dependencies.clock.now()), None)
                    subrepo = uow.repository(SubscriptionRecord)
                    existing = await subrepo.get(scope, locator.target_workspace_id)
                    await subrepo.save(scope, SubscriptionRecord(id=locator.target_workspace_id,
                        workspace_id=locator.target_workspace_id, plan_id=checkout.plan_id, status="active",
                        provider_reference=event.provider_subscription_reference),
                        existing.revision if existing else None)
                await uow.repository(InvoiceRecord).save(scope, InvoiceRecord(
                    id=_hash(f"invoice:{checkout.id}"), workspace_id=locator.target_workspace_id,
                    amount=checkout.amount, status="paid", download_url=None), None)
            if checkout.status != "paid":
                await repo.save(scope, replace(checkout, status=event.status), checkout.revision)
            recorded_at = self.dependencies.clock.now()
            saved = await uow.repository(PaymentEventRecord).save(GLOBAL, PaymentEventRecord(
                id=event_id, checkout_id=checkout_id, status=event.status,
                recorded_at=recorded_at), None)
            await uow.commit()
        return OperationReceipt(id=saved.id, revision=saved.revision, recorded_at=recorded_at)

    async def invoices(self, ctx: RequestContext, page: PageQuery) -> Page[Invoice]:
        scope = await self._scope(ctx)
        async with self.dependencies.uow() as uow:
            listing = await uow.repository(InvoiceRecord).list(scope, page)
        return Page(items=tuple(Invoice(id=item.id, amount=item.amount, status=item.status,
                    download_url=item.download_url) for item in listing.items),
                    next_cursor=listing.next_cursor)


@dataclass(frozen=True)
class CheckoutLocator:
    id: EntityId
    target_workspace_id: EntityId
    revision: int = 0
