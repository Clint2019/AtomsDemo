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
class FileEntry:
    path: str
    blob: BlobRef

@dataclass(frozen=True, kw_only=True)
class Version:
    id: EntityId
    project_id: EntityId
    parent_id: EntityId | None
    files: tuple[FileEntry, ...]
    source_archive: BlobRef
    artifact: BlobRef | None
    build_status: str
    summary: str
    created_at: datetime

@dataclass(frozen=True, kw_only=True)
class ProjectHead:
    project_id: EntityId
    version_id: EntityId | None
    revision: int

@dataclass(frozen=True, kw_only=True)
class CreateVersion:
    project_id: EntityId
    base_version_id: EntityId | None
    files: tuple[FileEntry, ...]
    summary: str
    mutation: Mutation

@dataclass(frozen=True, kw_only=True)
class PromoteVersion:
    project_id: EntityId
    version_id: EntityId
    expected_head_revision: int
    task_id: EntityId | None
    mutation: Mutation
    lease_owner: str | None = None

@dataclass(frozen=True, kw_only=True)
class FileEdit:
    path: str
    content: str | None

@dataclass(frozen=True, kw_only=True)
class EditVersion:
    project_id: EntityId
    base_version_id: EntityId
    edits: tuple[FileEdit, ...]
    summary: str
    mutation: Mutation

class VersionAPI(Protocol):
    async def head(self, ctx: RequestContext, project_id: EntityId) -> ProjectHead: ...
    async def get(self, ctx: RequestContext, version_id: EntityId) -> Version: ...
    async def clone_source(self, ctx: RequestContext, version_id: EntityId) -> Version: ...
    async def read_file(self, ctx: RequestContext, version_id: EntityId, path: str) -> str: ...
    async def list_versions(self, ctx: RequestContext, project_id: EntityId, page: PageQuery) -> Page[Version]: ...
    async def create_candidate(self, ctx: RequestContext, command: CreateVersion) -> Version: ...
    async def edit_candidate(self, ctx: RequestContext, command: EditVersion) -> Version: ...
    async def record_build(self, ctx: RequestContext, version_id: EntityId, artifact: BlobRef | None, success: bool) -> Version: ...
    async def promote(self, ctx: RequestContext, command: PromoteVersion) -> ProjectHead: ...
    async def export(self, ctx: RequestContext, version_id: EntityId) -> BlobRef: ...
    async def download_export(self, ctx: RequestContext, version_id: EntityId) -> bytes: ...
