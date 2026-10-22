from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from atoms_demo.core.types import (
    BlobRef, EntityId, Environment, Mutation, OperationReceipt,
    Page, PageQuery, RequestContext, Scope, SecretRef,
)

class Visibility(StrEnum):
    PRIVATE = "private"
    PUBLIC = "public"

@dataclass(frozen=True, kw_only=True)
class Project:
    id: EntityId
    workspace_id: EntityId
    owner_id: EntityId
    name: str
    visibility: Visibility
    revision: int
    source_version_id: EntityId | None = None

@dataclass(frozen=True, kw_only=True)
class CreateProject:
    name: str
    visibility: Visibility
    mutation: Mutation
    source_version_id: EntityId | None = None

@dataclass(frozen=True, kw_only=True)
class ProjectQuery:
    page: PageQuery
    favorites_only: bool = False

class ProjectAPI(Protocol):
    async def create(self, ctx: RequestContext, command: CreateProject) -> Project: ...
    async def get(self, ctx: RequestContext, project_id: EntityId) -> Project: ...
    async def list_projects(self, ctx: RequestContext, query: ProjectQuery) -> Page[Project]: ...
    async def rename(self, ctx: RequestContext, project_id: EntityId, name: str, mutation: Mutation) -> Project: ...
    async def set_visibility(self, ctx: RequestContext, project_id: EntityId, visibility: Visibility, mutation: Mutation) -> Project: ...
    async def set_favorite(self, ctx: RequestContext, project_id: EntityId, favorite: bool, mutation: Mutation) -> None: ...
