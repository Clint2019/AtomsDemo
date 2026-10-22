from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from atoms_demo.core.types import (
    BlobRef, EntityId, Environment, Mutation, OperationReceipt,
    Page, PageQuery, RequestContext, Scope, SecretRef,
)

@dataclass(frozen=True, kw_only=True)
class Connector:
    id: EntityId
    workspace_id: EntityId
    provider: str
    status: str
    credential: SecretRef | None
    project_ids: tuple[EntityId, ...]
    revision: int

@dataclass(frozen=True, kw_only=True)
class ConnectionCallback:
    provider: str
    code: str
    state: str

class ConnectorAPI(Protocol):
    async def list_connections(self, ctx: RequestContext, page: PageQuery) -> Page[Connector]: ...
    async def authorize(self, ctx: RequestContext, provider: str) -> str: ...
    async def callback(self, ctx: RequestContext, command: ConnectionCallback) -> Connector: ...
    async def bind(self, ctx: RequestContext, connector_id: EntityId, project_id: EntityId, mutation: Mutation) -> Connector: ...
    async def revoke(self, ctx: RequestContext, connector_id: EntityId, mutation: Mutation) -> None: ...
