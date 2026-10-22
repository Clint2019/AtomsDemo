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
class StoredObject:
    id: EntityId
    scope: Scope
    blob: BlobRef
    purpose: str
    display_name: str
    created_at: datetime

@dataclass(frozen=True, kw_only=True)
class Upload:
    scope: Scope
    purpose: str
    display_name: str
    media_type: str
    content: bytes
    mutation: Mutation

@dataclass(frozen=True, kw_only=True)
class StorageUsage:
    used_bytes: int
    quota_bytes: int
    pay_as_you_go: bool

class StorageAPI(Protocol):
    async def upload(self, ctx: RequestContext, command: Upload) -> StoredObject: ...
    async def get(self, ctx: RequestContext, object_id: EntityId) -> StoredObject: ...
    async def download(self, ctx: RequestContext, object_id: EntityId) -> bytes: ...
    async def list_objects(self, ctx: RequestContext, scope: Scope, page: PageQuery) -> Page[StoredObject]: ...
    async def delete(self, ctx: RequestContext, object_id: EntityId, mutation: Mutation) -> None: ...
    async def usage(self, ctx: RequestContext, project_id: EntityId | None = None) -> StorageUsage: ...
    async def set_pay_as_you_go(self, ctx: RequestContext, enabled: bool, mutation: Mutation) -> StorageUsage: ...
    async def pending_metering(self, ctx: RequestContext) -> tuple[StorageMetering, ...]: ...
    async def mark_metered(self, ctx: RequestContext, event_id: EntityId) -> None: ...
