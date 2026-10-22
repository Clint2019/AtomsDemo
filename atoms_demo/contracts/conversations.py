from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from atoms_demo.core.types import (
    BlobRef, EntityId, Environment, Mutation, OperationReceipt,
    Page, PageQuery, RequestContext, Scope, SecretRef,
)

class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"

@dataclass(frozen=True, kw_only=True)
class Message:
    id: EntityId
    project_id: EntityId
    role: MessageRole
    content: str
    attachment_ids: tuple[EntityId, ...]
    created_at: datetime
    task_id: EntityId | None

@dataclass(frozen=True, kw_only=True)
class AppendMessage:
    project_id: EntityId
    role: MessageRole
    content: str
    attachment_ids: tuple[EntityId, ...]
    task_id: EntityId | None
    mutation: Mutation

class ConversationAPI(Protocol):
    async def append(self, ctx: RequestContext, command: AppendMessage) -> Message: ...
    async def history(self, ctx: RequestContext, project_id: EntityId, page: PageQuery) -> Page[Message]: ...
    async def history_recent(self, ctx: RequestContext, project_id: EntityId, page: PageQuery) -> Page[Message]: ...
    async def search(self, ctx: RequestContext, project_id: EntityId, query: str,
                     page: PageQuery) -> Page[Message]: ...
    async def summarize(self, ctx: RequestContext, project_id: EntityId) -> str: ...
