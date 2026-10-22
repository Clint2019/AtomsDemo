"""Atomic SQLite task and lease fence used by version promotion."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from atoms_demo.core.errors import DomainError, ErrorCode
from atoms_demo.core.types import EntityId
from atoms_demo.ports.persistence import UnitOfWork
from atoms_demo.ports.runtime import CommitFence


class SQLiteCommitFence:
    async def assert_owned_lease(self, uow: UnitOfWork, task_id: EntityId,
                                 lease_owner: str) -> None:
        db = getattr(uow, "db", None)
        if db is None or not lease_owner:
            raise DomainError(ErrorCode.CONFLICT, "Task worker lease is missing")
        row = db.execute("SELECT state,worker_id,leased_until FROM jobs WHERE task_id=?",
                         (str(task_id),)).fetchone()
        if (row is None or row["state"] != "leased" or row["worker_id"] != lease_owner or
                not row["leased_until"] or
                datetime.fromisoformat(row["leased_until"]) <= datetime.now(timezone.utc)):
            raise DomainError(ErrorCode.CONFLICT, "Task worker lease expired")

    async def has_live_lease(self, uow: UnitOfWork, task_id: EntityId) -> bool:
        # Called inside the same write transaction that marks a task interrupted.
        db = getattr(uow, "db", None)
        if db is None:
            raise DomainError(ErrorCode.CONFLICT, "Task recovery transaction is missing")
        row = db.execute("SELECT state,leased_until FROM jobs WHERE task_id=?",
                         (str(task_id),)).fetchone()
        return bool(row and row["state"] == "leased" and row["leased_until"] and
                    datetime.fromisoformat(row["leased_until"]) > datetime.now(timezone.utc))

    async def assert_valid(self, uow: UnitOfWork, workspace_id: EntityId,
                           project_id: EntityId, task_id: EntityId,
                           version_id: EntityId, lease_owner: str) -> None:
        # The caller's UoW owns a BEGIN IMMEDIATE transaction. Reading here
        # serializes heartbeat, interruption and promotion against that write lock.
        await self.assert_owned_lease(uow, task_id, lease_owner)
        db = getattr(uow, "db", None)
        row = db.execute("SELECT payload FROM aggregate_tasks WHERE id=? AND workspace_id=? AND project_id=?",
                         (str(task_id), str(workspace_id), str(project_id))).fetchone()
        task = json.loads(row["payload"]) if row else None
        if (task is None or task["state"] != "committing" or
                task["result_version_id"] != str(version_id)):
            raise DomainError(ErrorCode.CONFLICT, "Task commit checkpoint changed")
        row = db.execute("SELECT payload FROM aggregate_task_active WHERE id=? AND workspace_id=? AND project_id=?",
                         (str(project_id), str(workspace_id), str(project_id))).fetchone()
        active = json.loads(row["payload"]) if row else None
        if active is None or active["task_id"] != str(task_id):
            raise DomainError(ErrorCode.CONFLICT, "Task is no longer active")


def create_adapter() -> CommitFence:
    return SQLiteCommitFence()
