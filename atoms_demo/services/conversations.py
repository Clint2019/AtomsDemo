"""Project conversation history and deterministic persisted summaries."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from atoms_demo.contracts.conversations import MessageRole, Message, AppendMessage, ConversationAPI
from atoms_demo.core.errors import DomainError, ErrorCode
from atoms_demo.core.types import EntityId, Page, PageQuery, RequestContext, Scope
from atoms_demo.ports.dependencies import ConversationDependencies


@dataclass(frozen=True)
class ConversationSummary:
    id: EntityId
    project_id: EntityId
    text: str
    revision: int = 0


@dataclass(frozen=True)
class MessageMutation:
    id: EntityId
    project_id: EntityId
    message_id: EntityId
    input_digest: EntityId
    revision: int = 0


def decode_message(data: dict) -> Message:
    return Message(**{**data, "role": MessageRole(data["role"]),
                      "attachment_ids": tuple(data["attachment_ids"]),
                      "created_at": datetime.fromisoformat(data["created_at"])})


def _hash(value: str) -> EntityId:
    return EntityId(hashlib.sha256(value.encode("utf-8")).hexdigest())


class ConversationService:
    def __init__(self, dependencies: ConversationDependencies) -> None:
        self.dependencies = dependencies

    @staticmethod
    def _scope(ctx: RequestContext, project_id: EntityId) -> Scope:
        if ctx.workspace_id is None:
            raise DomainError(ErrorCode.FORBIDDEN, "Workspace selection required")
        return Scope(workspace_id=ctx.workspace_id, project_id=project_id)

    async def append(self, ctx: RequestContext, command: AppendMessage) -> Message:
        await self.dependencies.authorization.require(ctx, "project:write", command.project_id)
        scope = self._scope(ctx, command.project_id)
        if not command.content.strip() or len(command.content) > 50_000:
            raise DomainError(ErrorCode.INVALID_INPUT, "Message content is invalid")
        if not command.mutation.idempotency_key:
            raise DomainError(ErrorCode.INVALID_INPUT, "Idempotency key required")
        if len(command.attachment_ids) > 10 or len(set(command.attachment_ids)) != len(command.attachment_ids):
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid attachments")
        for object_id in command.attachment_ids:
            if not await self.dependencies.attachments.exists(ctx.workspace_id, command.project_id, object_id):
                raise DomainError(ErrorCode.FORBIDDEN, "Attachment is unavailable to this project")
        mutation_id = _hash(f"message:{ctx.actor_id}:{command.project_id}:{command.mutation.idempotency_key}")
        input_digest = _hash(f"{command.role}:{command.content}:{command.attachment_ids}:{command.task_id}")
        async with self.dependencies.uow() as uow:
            prior = await uow.repository(MessageMutation).get(scope, mutation_id)
            if prior:
                if prior.input_digest != input_digest:
                    raise DomainError(ErrorCode.CONFLICT, "Idempotency key was used with different input")
                existing = await uow.repository(Message).get(scope, prior.message_id)
                if existing is None:
                    raise DomainError(ErrorCode.CONFLICT, "Message record is inconsistent")
                return existing
            message = Message(id=self.dependencies.ids.new(), project_id=command.project_id,
                              role=command.role, content=command.content,
                              attachment_ids=command.attachment_ids,
                              created_at=self.dependencies.clock.now(), task_id=command.task_id)
            await uow.repository(Message).save(scope, message, None)
            await uow.repository(MessageMutation).save(scope, MessageMutation(
                id=mutation_id, project_id=command.project_id, message_id=message.id,
                input_digest=input_digest), None)
            await uow.commit()
        return message

    async def history(self, ctx: RequestContext, project_id: EntityId, page: PageQuery) -> Page[Message]:
        await self.dependencies.authorization.require(ctx, "project:history", project_id)
        if not 1 <= page.limit <= 100:
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid page limit")
        scope = self._scope(ctx, project_id)
        async with self.dependencies.uow() as uow:
            cursor = None
            messages = []
            while True:
                listing = await uow.repository(Message).list(scope, PageQuery(cursor=cursor, limit=100))
                messages.extend(listing.items)
                if listing.next_cursor is None:
                    break
                cursor = listing.next_cursor
        messages.sort(key=lambda item: (item.created_at, str(item.id)))
        if page.cursor:
            try:
                start = next(index + 1 for index, item in enumerate(messages) if str(item.id) == page.cursor)
            except StopIteration:
                raise DomainError(ErrorCode.INVALID_INPUT, "Invalid message cursor") from None
        else:
            start = 0
        selected = messages[start:start + page.limit]
        next_cursor = str(selected[-1].id) if start + page.limit < len(messages) else None
        return Page(items=tuple(selected), next_cursor=next_cursor)

    async def history_recent(self, ctx: RequestContext, project_id: EntityId,
                             page: PageQuery) -> Page[Message]:
        await self.dependencies.authorization.require(ctx, "project:history", project_id)
        if not 1 <= page.limit <= 100:
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid page limit")
        scope = self._scope(ctx, project_id)
        async with self.dependencies.uow() as uow:
            cursor = None
            messages = []
            while True:
                listing = await uow.repository(Message).list(scope, PageQuery(cursor=cursor, limit=100))
                messages.extend(listing.items)
                if listing.next_cursor is None:
                    break
                cursor = listing.next_cursor
        messages.sort(key=lambda item: (item.created_at, str(item.id)), reverse=True)
        if page.cursor:
            try:
                start = next(index + 1 for index, item in enumerate(messages) if str(item.id) == page.cursor)
            except StopIteration:
                raise DomainError(ErrorCode.INVALID_INPUT, "Invalid message cursor") from None
        else:
            start = 0
        selected = messages[start:start + page.limit]
        next_cursor = str(selected[-1].id) if start + page.limit < len(messages) else None
        return Page(items=tuple(reversed(selected)), next_cursor=next_cursor)

    async def summarize(self, ctx: RequestContext, project_id: EntityId) -> str:
        await self.dependencies.authorization.require(ctx, "project:history", project_id)
        scope = self._scope(ctx, project_id)
        async with self.dependencies.uow() as uow:
            cursor = None
            messages: list[Message] = []
            while True:
                page = await uow.repository(Message).list(scope, PageQuery(cursor=cursor, limit=100))
                messages.extend(page.items)
                if page.next_cursor is None:
                    break
                cursor = page.next_cursor
            latest = sorted(messages, key=lambda item: (item.created_at, str(item.id)))[-20:]
            text = "\n".join(f"{message.role}: {message.content[:300]}" for message in latest)[-6000:]
            current = await uow.repository(ConversationSummary).get(scope, project_id)
            await uow.repository(ConversationSummary).save(
                scope, ConversationSummary(id=project_id, project_id=project_id, text=text),
                current.revision if current else None)
            await uow.commit()
        return text
