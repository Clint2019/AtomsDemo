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
class SourceFile:
    path: str
    content: str

@dataclass(frozen=True, kw_only=True)
class ModelOption:
    key: str
    label: str
    available: bool

@dataclass(frozen=True, kw_only=True)
class GenerationInput:
    task_id: EntityId
    prompt: str
    context_summary: str
    files: tuple[SourceFile, ...]
    attachments: tuple[BlobRef, ...]
    model: str
    agent_mode: str

@dataclass(frozen=True, kw_only=True)
class FileChangeSet:
    summary: str
    upserts: tuple[SourceFile, ...]
    deleted_paths: tuple[str, ...]
    input_tokens: int
    output_tokens: int

@dataclass(frozen=True, kw_only=True)
class RepairInput:
    generation: GenerationInput
    diagnostics: tuple[str, ...]
    repair_number: int

class GenerationAPI(Protocol):
    async def models(self, ctx: RequestContext) -> tuple[ModelOption, ...]: ...
    async def generate(self, ctx: RequestContext, command: GenerationInput) -> FileChangeSet: ...
    async def repair(self, ctx: RequestContext, command: RepairInput) -> FileChangeSet: ...
