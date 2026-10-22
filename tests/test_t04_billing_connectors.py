"""T04 local accounting and connector transaction rules with explicit provider stubs."""
import asyncio
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from atoms_demo.adapters.authorization import membership_id
from atoms_demo.adapters.payment_provider import UnconfiguredPaymentProvider
from atoms_demo.bootstrap import compose_application
from atoms_demo.config import Settings
from atoms_demo.contracts.billing import (AccountKind, Checkout, Money, Plan, UsageCharge)
from atoms_demo.contracts.connectors import ConnectionCallback
from atoms_demo.contracts.projects import CreateProject, Visibility
from atoms_demo.contracts.storage import Upload
from atoms_demo.contracts.workspaces import Membership, Role, Workspace
from atoms_demo.core.errors import DomainError, ErrorCode
from atoms_demo.core.types import EntityId, Mutation, PageQuery, RequestContext, Scope
from atoms_demo.ports.providers import OAuthIdentity, VerifiedPaymentEvent
from atoms_demo.services.billing import WalletCurrency


class PaymentStub:
    def __init__(self):
        self.checkouts = []

    async def ensure_configured(self):
        return None

    async def checkout(self, reference, amount, idempotency_key):
        self.checkouts.append((reference, amount, idempotency_key))
        return Checkout(id=EntityId("provider-" + reference), url="https://pay.example.test/checkout", mode="stub")

    async def verify_webhook(self, body, signature):
        if signature != "valid":
            raise DomainError(ErrorCode.FORBIDDEN, "Invalid signature")
        data = json.loads(body)
        return VerifiedPaymentEvent(provider_event_id=data["event"],
            checkout_reference=data["reference"], status=data["status"],
            amount=Money(minor_units=data["amount"], currency=data["currency"]),
            provider_subscription_reference=data.get("subscription"))


class OAuthStub:
    def __init__(self):
        self.revoked = []

    async def authorize_url(self, provider, state, challenge):
        return f"https://oauth.example.test/?state={state}"

    async def exchange(self, provider, code, verifier):
        return OAuthIdentity(subject="user", email=None, access_token="private-token", refresh_token=None)

    async def revoke(self, provider, token):
        self.revoked.append((provider, token))


class StoragePriceStub:
    async def quote(self, event):
        return Money(minor_units=10, currency="CNY")


class FailingPriceStub:
    async def quote(self, event):
        raise RuntimeError("Pricing changed")


class T04Tests(unittest.TestCase):
    def test_failed_top_up_does_not_lock_wallet_currency(self):
        async def run():
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                settings = Settings(database_url=f"sqlite:///{root / 'db.sqlite'}",
                    blob_directory=root / "blobs", public_origin="http://localhost:5173",
                    preview_origin="http://localhost:8001", model_provider="",
                    model_secret_name="", task_timeout_seconds=180)
                with patch.dict(os.environ, {"ATOMS_SECRET_ENCRYPTION_KEY":
                                               Fernet.generate_key().decode()}):
                    app = compose_application(settings)
                workspace = EntityId("top-up-failure-workspace")
                owner = EntityId("top-up-failure-owner")
                scope = Scope(workspace_id=workspace)
                ctx = RequestContext(actor_id=owner, session_id=EntityId("top-up-failure-session"),
                    workspace_id=workspace, request_id="top-up-failure", client_binding="browser")
                billing = app.services.billing
                async with billing.dependencies.uow() as uow:
                    await uow.repository(Workspace).save(scope, Workspace(
                        id=workspace, name="Top-up failure", revision=0), None)
                    await uow.repository(Membership).save(scope, Membership(
                        id=membership_id(workspace, owner), workspace_id=workspace,
                        user_id=owner, role=Role.OWNER, revision=0), None)
                    await uow.commit()
                self.assertIsInstance(billing.dependencies.payments, UnconfiguredPaymentProvider)
                with self.assertRaises(DomainError) as unavailable:
                    await billing.top_up(ctx, Money(minor_units=500, currency="CNY"),
                                         Mutation(idempotency_key="failed-cny"))
                self.assertEqual(unavailable.exception.code, ErrorCode.PROVIDER_UNAVAILABLE)
                async with billing.dependencies.uow() as uow:
                    self.assertIsNone(await uow.repository(WalletCurrency).get(scope, workspace))
                payments = PaymentStub()
                billing.dependencies = replace(billing.dependencies, payments=payments)
                checkout = await billing.top_up(ctx, Money(minor_units=500, currency="USD"),
                                                Mutation(idempotency_key="successful-usd"))
                self.assertEqual(checkout.mode, "top_up")
                async with billing.dependencies.uow() as uow:
                    policy = await uow.repository(WalletCurrency).get(scope, workspace)
                self.assertEqual(policy.currency, "USD")
                self.assertEqual(len(payments.checkouts), 1)
        asyncio.run(run())

    def test_verified_payment_reservation_and_connector_scope(self):
        async def run():
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                settings = Settings(database_url=f"sqlite:///{root / 'db.sqlite'}", blob_directory=root / "blobs",
                    public_origin="http://localhost:5173", preview_origin="http://localhost:8001",
                    model_provider="", model_secret_name="", task_timeout_seconds=180)
                encryption_key = Fernet.generate_key().decode()
                with patch.dict(os.environ, {"ATOMS_SECRET_ENCRYPTION_KEY": encryption_key}):
                    app = compose_application(settings)
                workspace = EntityId("workspace-a")
                owner = EntityId("owner-a")
                ctx = RequestContext(actor_id=owner, session_id=EntityId("session-a"),
                                     workspace_id=workspace, request_id="r", client_binding="browser-a")
                async with app.services.billing.dependencies.uow() as uow:
                    await uow.repository(Workspace).save(Scope(workspace_id=workspace), Workspace(
                        id=workspace, name="Team", revision=0), None)
                    await uow.repository(Membership).save(Scope(workspace_id=workspace), Membership(
                        id=membership_id(workspace, owner), workspace_id=workspace,
                        user_id=owner, role=Role.OWNER, revision=0), None)
                    await uow.commit()
                project = await app.services.projects.create(ctx, CreateProject(
                    name="Project", visibility=Visibility.PRIVATE, mutation=Mutation(idempotency_key="p")))
                plan = Plan(id=EntityId("plan-a"), name="Test plan", interval="month",
                            price=Money(minor_units=1000, currency="CNY"), entitlements=("credits:100",))
                billing = app.services.billing
                payments = PaymentStub()
                billing.dependencies = replace(billing.dependencies, payments=payments, plans=(plan,))
                checkout = await billing.checkout(ctx, plan.id, Mutation(idempotency_key="checkout"))
                self.assertEqual((await billing.checkout(ctx, plan.id, Mutation(idempotency_key="checkout"))).id,
                                 checkout.id)
                billing.dependencies = replace(billing.dependencies, plans=())
                self.assertEqual((await billing.checkout(ctx, plan.id, Mutation(idempotency_key="checkout"))).id,
                                 checkout.id)
                reference = str(checkout.id).removeprefix("provider-")
                event = json.dumps({"event": "event-1", "reference": reference, "status": "paid",
                                    "amount": 1000, "currency": "CNY", "subscription": "sub-1"}).encode()
                with self.assertRaises(DomainError):
                    await billing.handle_webhook(event, "invalid")
                first = await billing.handle_webhook(event, "valid")
                self.assertEqual((await billing.handle_webhook(event, "valid")).id, first.id)
                self.assertEqual((await billing.subscription(ctx)).plan_id, plan.id)
                self.assertEqual((await billing.balances(ctx))[0].available, 100)
                reservation = await billing.reserve(ctx, EntityId("task-1"), 40,
                    Mutation(idempotency_key="reserve"), project.id)
                self.assertEqual((await billing.balances(ctx))[0].reserved, 40)
                receipt = await billing.settle(ctx, UsageCharge(reservation_id=reservation.id,
                    consumed_units=25, source_event_id="usage-1"))
                self.assertEqual((await billing.settle(ctx, UsageCharge(reservation_id=reservation.id,
                    consumed_units=25, source_event_id="usage-1"))).id, receipt.id)
                self.assertEqual((await billing.balances(ctx))[0].available, 75)
                self.assertEqual(payments.checkouts[0][0], payments.checkouts[0][2])
                topup = await billing.top_up(ctx, Money(minor_units=500, currency="CNY"),
                                            Mutation(idempotency_key="topup"))
                with self.assertRaises(DomainError) as mismatch:
                    await billing.top_up(ctx, Money(minor_units=500, currency="USD"),
                                         Mutation(idempotency_key="usd"))
                self.assertEqual(mismatch.exception.code, ErrorCode.CONFLICT)
                self.assertEqual(len(payments.checkouts), 2)
                topup_event = json.dumps({"event": "topup-1",
                    "reference": str(topup.id).removeprefix("provider-"), "status": "paid",
                    "amount": 500, "currency": "CNY"}).encode()
                await billing.handle_webhook(topup_event, "valid")
                self.assertEqual((await billing.balances(ctx))[1].available, 500)
                await app.services.storage.set_pay_as_you_go(ctx, True,
                    Mutation(idempotency_key="pay-as-you-go"))
                app.metering.pricing = StoragePriceStub()
                await app.services.storage.upload(ctx, Upload(
                    scope=Scope(workspace_id=workspace, project_id=project.id),
                    purpose="attachment", display_name="note.txt", media_type="text/plain",
                    content=b"meter", mutation=Mutation(idempotency_key="metered-upload")))
                pending = await app.services.storage.pending_metering(ctx)
                self.assertEqual(len(pending), 1)
                # Simulate a crash after billing commit but before outbox acknowledgement.
                await billing.record_storage_metering(ctx, pending[0], Money(minor_units=10, currency="CNY"))
                app.metering.pricing = FailingPriceStub()
                self.assertEqual(await app.metering.flush(ctx), 1)
                self.assertEqual(await app.metering.flush(ctx), 0)
                self.assertEqual((await billing.balances(ctx))[1].available, 490)
                await app.services.storage.upload(ctx, Upload(
                    scope=Scope(workspace_id=workspace, project_id=project.id),
                    purpose="attachment", display_name="empty.txt", media_type="text/plain",
                    content=b"", mutation=Mutation(idempotency_key="empty-upload")))
                self.assertEqual(await app.metering.flush(ctx), 0)
                self.assertEqual((await billing.ledger(ctx, AccountKind.CLOUD_WALLET,
                    PageQuery(), project.id)).items[0].units, -10)
                released = await billing.reserve(ctx, EntityId("task-release"), 20,
                    Mutation(idempotency_key="release"), project.id)
                await billing.release(ctx, released.id)
                await billing.release(ctx, released.id)
                self.assertEqual((await billing.balances(ctx))[0].reserved, 0)
                self.assertEqual((await billing.ledger(ctx, AccountKind.BUILD_CREDITS,
                    PageQuery(), project.id)).items[0].units, -25)
                oauth = OAuthStub()
                connectors = app.services.connectors
                connectors.dependencies = replace(connectors.dependencies, oauth=oauth)
                url = await connectors.authorize(ctx, "github")
                state = url.split("state=", 1)[1]
                with self.assertRaises(DomainError):
                    await connectors.callback(replace(ctx, client_binding="other"), ConnectionCallback(
                        provider="github", code="ok", state=state))
                connection = await connectors.callback(ctx, ConnectionCallback(
                    provider="github", code="ok", state=state))
                self.assertIsNotNone(connection.credential)
                with self.assertRaises(DomainError):
                    await connectors.callback(ctx, ConnectionCallback(provider="github", code="ok", state=state))
                connection = await connectors.bind(ctx, connection.id, project.id, Mutation(idempotency_key="bind"))
                self.assertEqual(connection.project_ids, (project.id,))
                await connectors.revoke(ctx, connection.id, Mutation(idempotency_key="revoke"))
                self.assertEqual(oauth.revoked, [("github", "private-token")])
                with self.assertRaises(DomainError):
                    await connectors.bind(ctx, connection.id, project.id, Mutation(idempotency_key="after-revoke"))
                with patch.dict(os.environ, {"ATOMS_SECRET_ENCRYPTION_KEY": encryption_key}):
                    restarted = compose_application(settings)
                self.assertEqual((await restarted.services.billing.balances(ctx))[0].available, 75)
                self.assertEqual((await restarted.services.connectors.list_connections(ctx,
                    PageQuery())).items[0].status, "revoked")
        asyncio.run(run())
