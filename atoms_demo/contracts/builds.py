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
class BuildInput:
    task_id: EntityId
    version_id: EntityId
    source_archive: BlobRef
    template_id: str

@dataclass(frozen=True, kw_only=True)
class BuildResult:
    version_id: EntityId
    success: bool
    artifact: BlobRef | None
    diagnostics: tuple[str, ...]
    log: BlobRef | None

@dataclass(frozen=True, kw_only=True)
class ScanResult:
    version_id: EntityId
    passed: bool
    findings: tuple[str, ...]

class BuildAPI(Protocol):
    async def build(self, ctx: RequestContext, command: BuildInput) -> BuildResult: ...
    async def scan(self, ctx: RequestContext, version_id: EntityId, artifact: BlobRef) -> ScanResult: ...
