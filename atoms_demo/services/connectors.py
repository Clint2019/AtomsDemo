"""Workspace connector authorization, scoped credentials and project bindings."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from atoms_demo.contracts.connectors import Connector, ConnectionCallback
from atoms_demo.core.errors import DomainError, ErrorCode
from atoms_demo.core.types import EntityId, Mutation, Page, PageQuery, RequestContext, Scope, SecretRef
from atoms_demo.ports.dependencies import ConnectorDependencies

GLOBAL = Scope(workspace_id=None)


@dataclass(frozen=True)
class ConnectionTransaction:
    id: EntityId
    target_workspace_id: EntityId
    actor_id: EntityId
    provider: str
    verifier_secret_id: EntityId
    binding_hash: EntityId
    expires_at: datetime
    revision: int = 0


def decode_connection_transaction(data: dict) -> ConnectionTransaction:
    return ConnectionTransaction(**{**data, "expires_at": datetime.fromisoformat(data["expires_at"])})


def decode_connector(data: dict) -> Connector:
    return Connector(**{**data, "credential": SecretRef(**data["credential"]) if data["credential"] else None,
                        "project_ids": tuple(data["project_ids"])})


def _hash(value: str) -> EntityId:
    return EntityId(hashlib.sha256(value.encode()).hexdigest())


class ConnectorService:
    def __init__(self, dependencies: ConnectorDependencies) -> None:
        self.dependencies = dependencies

    @staticmethod
    def _scope(ctx: RequestContext) -> Scope:
        if ctx.actor_id is None:
            raise DomainError(ErrorCode.UNAUTHENTICATED, "Sign in required")
        if ctx.workspace_id is None:
            raise DomainError(ErrorCode.FORBIDDEN, "Workspace selection required")
        return Scope(workspace_id=ctx.workspace_id)

    async def list_connections(self, ctx: RequestContext, page: PageQuery) -> Page[Connector]:
        scope = self._scope(ctx)
        await self.dependencies.authorization.require(ctx, "workspace:read", ctx.workspace_id)
        async with self.dependencies.uow() as uow:
            return await uow.repository(Connector).list(scope, page)

    async def authorize(self, ctx: RequestContext, provider: str) -> str:
        self._scope(ctx)
        await self.dependencies.authorization.require(ctx, "workspace:manage", ctx.workspace_id)
        if not ctx.client_binding:
            raise DomainError(ErrorCode.FORBIDDEN, "OAuth browser binding required")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{1,30}", provider):
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid connector provider")
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        url = await self.dependencies.oauth.authorize_url(provider, state, challenge)
        verifier_ref = await self.dependencies.secrets.put(Scope(workspace_id=ctx.workspace_id), verifier)
        async with self.dependencies.uow() as uow:
            await uow.repository(ConnectionTransaction).save(GLOBAL, ConnectionTransaction(
                id=_hash(state), target_workspace_id=ctx.workspace_id, actor_id=ctx.actor_id,
                provider=provider, verifier_secret_id=verifier_ref.id,
                binding_hash=_hash(ctx.client_binding),
                expires_at=self.dependencies.clock.now() + timedelta(minutes=10)), None)
            await uow.commit()
        return url

    async def callback(self, ctx: RequestContext, command: ConnectionCallback) -> Connector:
        scope = self._scope(ctx)
        await self.dependencies.authorization.require(ctx, "workspace:manage", ctx.workspace_id)
        async with self.dependencies.uow() as uow:
            txn = await uow.repository(ConnectionTransaction).get(GLOBAL, _hash(command.state))
            if (not txn or txn.provider != command.provider or txn.target_workspace_id != ctx.workspace_id or
                    txn.actor_id != ctx.actor_id or not ctx.client_binding or
                    not hmac.compare_digest(str(txn.binding_hash), str(_hash(ctx.client_binding))) or
                    txn.expires_at <= self.dependencies.clock.now()):
                raise DomainError(ErrorCode.INVALID_INPUT, "Connector state is invalid or expired")
            await uow.repository(ConnectionTransaction).delete(GLOBAL, txn.id, txn.revision)
            await uow.commit()
        verifier_ref = SecretRef(id=txn.verifier_secret_id)
        try:
            verifier = await self.dependencies.secrets.resolve(verifier_ref)
            identity = await self.dependencies.oauth.exchange(command.provider, command.code, verifier)
        finally:
            await self.dependencies.secrets.revoke(verifier_ref)
        if not identity.access_token:
            raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Provider returned no access token")
        credential = await self.dependencies.secrets.put(scope, json.dumps({
            "access_token": identity.access_token, "refresh_token": identity.refresh_token}))
        connector = Connector(id=self.dependencies.ids.new(), workspace_id=ctx.workspace_id,
                              provider=command.provider, status="active", credential=credential,
                              project_ids=(), revision=0)
        async with self.dependencies.uow() as uow:
            connector = await uow.repository(Connector).save(scope, connector, None)
            await uow.commit()
        return connector

    async def bind(self, ctx: RequestContext, connector_id: EntityId, project_id: EntityId,
                   mutation: Mutation) -> Connector:
        scope = self._scope(ctx)
        await self.dependencies.authorization.require(ctx, "workspace:manage", ctx.workspace_id)
        await self.dependencies.authorization.require(ctx, "project:write", project_id)
        async with self.dependencies.uow() as uow:
            repo = uow.repository(Connector)
            connector = await repo.get(scope, connector_id)
            if connector is None or connector.status != "active":
                raise DomainError(ErrorCode.NOT_FOUND, "Active connector not found")
            if mutation.expected_revision is not None and mutation.expected_revision != connector.revision:
                raise DomainError(ErrorCode.CONFLICT, "Connector revision changed")
            if project_id in connector.project_ids:
                return connector
            connector = await repo.save(scope, replace(connector,
                project_ids=(*connector.project_ids, project_id)), connector.revision)
            await uow.commit()
        return connector

    async def revoke(self, ctx: RequestContext, connector_id: EntityId, mutation: Mutation) -> None:
        scope = self._scope(ctx)
        await self.dependencies.authorization.require(ctx, "workspace:manage", ctx.workspace_id)
        async with self.dependencies.uow() as uow:
            repo = uow.repository(Connector)
            connector = await repo.get(scope, connector_id)
            if connector is None:
                raise DomainError(ErrorCode.NOT_FOUND, "Connector not found")
            if connector.status == "revoked":
                return
            if mutation.expected_revision is not None and mutation.expected_revision != connector.revision:
                raise DomainError(ErrorCode.CONFLICT, "Connector revision changed")
        if connector.credential:
            try:
                raw = await self.dependencies.secrets.resolve(connector.credential)
            except KeyError:
                # A previous revoke may have stopped after secret destruction.
                raw = None
            if raw is not None:
                token = json.loads(raw)["access_token"]
                await self.dependencies.oauth.revoke(connector.provider, token)
            await self.dependencies.secrets.revoke(connector.credential)
        async with self.dependencies.uow() as uow:
            current = await uow.repository(Connector).get(scope, connector_id)
            if current and current.status != "revoked":
                await uow.repository(Connector).save(scope, replace(current, status="revoked",
                    credential=None, project_ids=()), current.revision)
                await uow.commit()
