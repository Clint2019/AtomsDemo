from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from atoms_demo.core.types import (
    BlobRef, EntityId, Environment, Mutation, OperationReceipt,
    Page, PageQuery, RequestContext, Scope, SecretRef,
)

class PublicationState(StrEnum):
    DRAFT = "draft"
    AWAITING_MANUAL = "awaiting_manual"
    LIVE = "live"
    PAUSED = "paused"
    UNPUBLISHED = "unpublished"
    FAILED = "failed"

@dataclass(frozen=True, kw_only=True)
class Publication:
    id: EntityId
    project_id: EntityId
    version_id: EntityId
    environment: Environment
    domain: str | None
    sync_db: bool
    migrate_data: bool
    state: PublicationState
    execution_mode: str
    revision: int
    live_version_id: EntityId | None = None
    public_url: str | None = None
    pending_action: str | None = None

@dataclass(frozen=True, kw_only=True)
class PublicationConfig:
    project_id: EntityId
    version_id: EntityId
    environment: Environment
    domain: str | None
    sync_db: bool
    migrate_data: bool
    mutation: Mutation

@dataclass(frozen=True, kw_only=True)
class ManualDeploymentResult:
    publication_id: EntityId
    public_url: str
    evidence: BlobRef
    operator_id: EntityId
    mutation: Mutation

class PublicationAPI(Protocol):
    async def configured_by_mutation(self, ctx: RequestContext, command: PublicationConfig) -> Publication | None: ...
    async def configure(self, ctx: RequestContext, command: PublicationConfig) -> Publication: ...
    async def get(self, ctx: RequestContext, project_id: EntityId) -> Publication | None: ...
    async def request_manual(self, ctx: RequestContext, publication_id: EntityId, mutation: Mutation) -> Publication: ...
    async def verify_manual_result(self, ctx: RequestContext, command: ManualDeploymentResult) -> Publication: ...
    async def change_state(self, ctx: RequestContext, publication_id: EntityId, target: PublicationState, mutation: Mutation) -> Publication: ...
