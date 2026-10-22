from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from atoms_demo.core.types import (
    BlobRef, EntityId, Environment, Mutation, OperationReceipt,
    Page, PageQuery, RequestContext, Scope, SecretRef, PreviewGrant,
)

class ResourceKind(StrEnum):
    DATABASE = "database"
    AUTHENTICATION = "authentication"
    STORAGE = "storage"
    SECRET = "secret"

@dataclass(frozen=True, kw_only=True)
class AppResource:
    id: EntityId
    project_id: EntityId
    environment: Environment
    kind: ResourceKind
    name: str
    status: str
    secret_ref: SecretRef | None
    revision: int

@dataclass(frozen=True, kw_only=True)
class AppDataContext:
    """Derived from scoped application session, never the platform user's session cookie."""
    project_id: EntityId
    app_user_id: EntityId
    environment: Environment

@dataclass(frozen=True, kw_only=True)
class AppRecord:
    id: EntityId
    collection: str
    json_document: str
    schema_version: int
    revision: int

@dataclass(frozen=True, kw_only=True)
class AppSession:
    id: EntityId
    project_id: EntityId
    app_user_id: EntityId
    environment: Environment
    expires_at: datetime

class AppResourceAPI(Protocol):
    async def resources(self, ctx: RequestContext, project_id: EntityId, environment: Environment) -> tuple[AppResource, ...]: ...
    async def configure(self, ctx: RequestContext, resource: AppResource, mutation: Mutation) -> AppResource: ...
    async def set_secret(self, ctx: RequestContext, resource_id: EntityId, secret_value: str, mutation: Mutation) -> AppResource: ...
    async def open_app_session(self, grant: PreviewGrant, existing_app_token: str | None,
                               platform_actor_id: EntityId | None = None,
                               platform_session_id: EntityId | None = None) -> AppSession: ...
    async def list_app_users(self, ctx: RequestContext, project_id: EntityId, page: PageQuery) -> Page[EntityId]: ...
    async def authenticate_app(self, app_token: str) -> AppDataContext: ...
    async def records(self, ctx: AppDataContext, collection: str, page: PageQuery) -> Page[AppRecord]: ...
    async def put_record(self, ctx: AppDataContext, record: AppRecord, mutation: Mutation) -> AppRecord: ...
    async def delete_record(self, ctx: AppDataContext, record_id: EntityId, mutation: Mutation) -> None: ...
