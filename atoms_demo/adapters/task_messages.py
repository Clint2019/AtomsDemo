"""Conversation persistence for messages tied to task transitions."""
from datetime import datetime, timedelta

from atoms_demo.contracts.conversations import Message, MessageRole
from atoms_demo.core.types import EntityId, Scope
from atoms_demo.ports.persistence import UnitOfWork


class RepositoryTaskMessageWriter:
    async def append(self, uow: UnitOfWork, scope: Scope, message_id: EntityId,
                     project_id: EntityId, task_id: EntityId, role: str,
                     content: str, previous_message_id: EntityId,
                     created_at: datetime) -> None:
        repository = uow.repository(Message)
        previous = await repository.get(scope, previous_message_id)
        if previous and created_at <= previous.created_at:
            created_at = previous.created_at + timedelta(microseconds=1)
        await repository.save(scope, Message(
            id=message_id, project_id=project_id, role=MessageRole(role),
            content=content, attachment_ids=(), created_at=created_at,
            task_id=task_id), None)
