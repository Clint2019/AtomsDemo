"""Durable ordered task events."""
import sqlite3
from datetime import datetime

from atoms_demo.ports.runtime import EventStore
from atoms_demo.config import Settings, database_path
from atoms_demo.core.events import TaskEvent
from atoms_demo.core.types import EntityId
from atoms_demo.adapters.persistence import connect, migrate


class SQLiteEventStore:
    def __init__(self, path):
        self.path = path
        migrate(path)

    async def append(self, event: TaskEvent) -> None:
        if event.sequence < 1:
            raise ValueError("Event sequence must be positive")
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            last = db.execute("SELECT MAX(sequence) AS n FROM task_events WHERE task_id=?", (str(event.task_id),)).fetchone()["n"]
            if event.sequence != (last or 0) + 1:
                raise ValueError("Event sequence gap or duplicate")
            db.execute("INSERT INTO task_events VALUES (?,?,?,?,?,?,?,?)",
                       (str(event.task_id), event.sequence, str(event.event_id), str(event.project_id),
                        event.kind, event.message, event.created_at.isoformat(),
                        str(event.version_id) if event.version_id else None))
            db.commit()

    async def read(self, task_id: EntityId, after_sequence: int) -> tuple[TaskEvent, ...]:
        if after_sequence < 0:
            raise ValueError("Event cursor must be nonnegative")
        with connect(self.path) as db:
            rows = db.execute("SELECT * FROM task_events WHERE task_id=? AND sequence>? ORDER BY sequence LIMIT 1000",
                              (str(task_id), after_sequence)).fetchall()
        return tuple(TaskEvent(event_id=EntityId(row["event_id"]), task_id=EntityId(row["task_id"]),
                               project_id=EntityId(row["project_id"]), sequence=row["sequence"],
                               kind=row["kind"], message=row["message"],
                               created_at=datetime.fromisoformat(row["created_at"]),
                               version_id=EntityId(row["version_id"]) if row["version_id"] else None)
                     for row in rows)


def create_adapter(settings: Settings) -> EventStore:
    return SQLiteEventStore(database_path(settings))
