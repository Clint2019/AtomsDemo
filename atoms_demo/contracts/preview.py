from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from atoms_demo.core.types import (
    BlobRef, EntityId, Environment, Mutation, OperationReceipt,
    Page, PageQuery, RequestContext, Scope, SecretRef, PreviewGrant,
)

@dataclass(frozen=True, kw_only=True)
class PreviewTicket:
    id: EntityId
    project_id: EntityId
    version_id: EntityId
    url: str
    expires_at: datetime

@dataclass(frozen=True, kw_only=True)
class RuntimeIssue:
    preview_token: str
    project_id: EntityId
    version_id: EntityId
    message: str
    stack: str | None
    source_origin: str

@dataclass(frozen=True, kw_only=True)
class ShareLink:
    id: EntityId
    project_id: EntityId
    version_id: EntityId
    url: str
    expires_at: datetime | None
    revoked: bool

class PreviewAPI(Protocol):
    async def issue_ticket(self, ctx: RequestContext, grant: PreviewGrant) -> PreviewTicket: ...
    async def issue_catalog_ticket(self, grant: PreviewGrant, author_id: EntityId) -> PreviewTicket: ...
    async def revoke_ticket(self, ctx: RequestContext, ticket_id: EntityId, mutation: Mutation) -> None: ...
    async def resolve_ticket(self, token: str) -> PreviewGrant: ...
    async def serve_artifact(self, token: str) -> bytes: ...
    async def report_issue(self, ctx: RequestContext, issue: RuntimeIssue) -> OperationReceipt: ...
    async def create_share(self, ctx: RequestContext, grant: PreviewGrant, mutation: Mutation) -> ShareLink: ...
    async def revoke_share(self, ctx: RequestContext, share_id: EntityId, mutation: Mutation) -> None: ...
    async def resolve_share(self, share_token: str) -> PreviewTicket: ...
