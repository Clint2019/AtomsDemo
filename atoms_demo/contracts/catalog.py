from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from atoms_demo.core.types import (
    BlobRef, EntityId, Environment, Mutation, OperationReceipt,
    Page, PageQuery, RequestContext, Scope, SecretRef,
)

class ContentKind(StrEnum):
    TEMPLATE = "template"
    PUBLIC_PROJECT = "public_project"

@dataclass(frozen=True, kw_only=True)
class CatalogEntry:
    id: EntityId
    kind: ContentKind
    project_id: EntityId
    version_id: EntityId
    author_id: EntityId
    title: str
    category: str
    description: str
    views: int
    saves: int
    revision: int
    saved: bool = False

@dataclass(frozen=True, kw_only=True)
class CatalogQuery:
    page: PageQuery
    kind: ContentKind | None = None
    category: str | None = None
    author_id: EntityId | None = None
    saved_only: bool = False
    sort: str = "latest"

class CatalogAPI(Protocol):
    async def search(self, ctx: RequestContext, query: CatalogQuery) -> Page[CatalogEntry]: ...
    async def detail(self, ctx: RequestContext, entry_id: EntityId) -> CatalogEntry: ...
    async def categories(self, ctx: RequestContext, kind: ContentKind) -> tuple[str, ...]: ...
    async def save(self, ctx: RequestContext, entry_id: EntityId, saved: bool, mutation: Mutation) -> None: ...
    async def record_view(self, ctx: RequestContext, entry_id: EntityId, mutation: Mutation) -> None: ...
    async def publish_entry(self, ctx: RequestContext, entry: CatalogEntry, mutation: Mutation) -> CatalogEntry: ...
