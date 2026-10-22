"""SQLite transaction and explicit aggregate registration infrastructure."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from types import TracebackType
from typing import Any, Callable, TypeVar

from atoms_demo.config import Settings, database_path
from atoms_demo.core.types import EntityId, Page, PageQuery, Scope

T = TypeVar("T")


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("Persistent timestamps must include timezone")
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"Unsupported persistent value: {type(value).__name__}")


def _dump(value: dict[str, Any]) -> str:
    return json.dumps(value, default=_json_default, ensure_ascii=False, separators=(",", ":"))


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10, isolation_level=None, factory=ClosingConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


def migrate(path: Path) -> None:
    with connect(path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)")
        if db.execute("SELECT 1 FROM schema_migrations WHERE version=1").fetchone():
            return
        db.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS task_events (
                task_id TEXT NOT NULL, sequence INTEGER NOT NULL, event_id TEXT NOT NULL UNIQUE,
                project_id TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL,
                created_at TEXT NOT NULL, version_id TEXT,
                PRIMARY KEY (task_id, sequence));
            CREATE TABLE IF NOT EXISTS jobs (
                task_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL DEFAULT 'pending', worker_id TEXT,
                leased_until TEXT, created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(state, leased_until, created_at);
            CREATE TABLE IF NOT EXISTS secrets (
                id TEXT PRIMARY KEY, workspace_id TEXT, project_id TEXT,
                ciphertext BLOB NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
            INSERT INTO schema_migrations(version) VALUES (1);
            COMMIT;
        """)


@dataclass(frozen=True)
class AggregateBinding:
    table: str
    decode: Callable[[dict[str, Any]], Any]
    encode: Callable[[Any], dict[str, Any]]


class SQLiteRepository:
    def __init__(self, db: sqlite3.Connection, binding: AggregateBinding):
        self.db = db
        self.binding = binding

    @staticmethod
    def _scope(scope: Scope) -> tuple[str | None, str | None]:
        return (str(scope.workspace_id) if scope.workspace_id else None,
                str(scope.project_id) if scope.project_id else None)

    async def get(self, scope: Scope, entity_id: EntityId) -> Any | None:
        workspace, project = self._scope(scope)
        row = self.db.execute(
            f"SELECT payload FROM {self.binding.table} WHERE id=? AND workspace_id IS ? AND project_id IS ?",
            (str(entity_id), workspace, project)).fetchone()
        return self.binding.decode(json.loads(row["payload"])) if row else None

    async def list(self, scope: Scope, page: PageQuery) -> Page[Any]:
        if not 1 <= page.limit <= 100:
            raise ValueError("Page limit must be between 1 and 100")
        workspace, project = self._scope(scope)
        rows = self.db.execute(
            f"SELECT id,payload FROM {self.binding.table} WHERE workspace_id IS ? AND project_id IS ? "
            "AND id > ? ORDER BY id LIMIT ?",
            (workspace, project, page.cursor or "", page.limit + 1)).fetchall()
        next_cursor = rows[page.limit - 1]["id"] if len(rows) > page.limit else None
        return Page(items=tuple(self.binding.decode(json.loads(row["payload"])) for row in rows[:page.limit]),
                    next_cursor=next_cursor)

    async def latest(self, scope: Scope) -> Any | None:
        workspace, project = self._scope(scope)
        row = self.db.execute(
            f"SELECT payload FROM {self.binding.table} WHERE workspace_id IS ? "
            "AND project_id IS ? ORDER BY rowid DESC LIMIT 1",
            (workspace, project),
        ).fetchone()
        return self.binding.decode(json.loads(row["payload"])) if row else None

    async def save(self, scope: Scope, entity: Any, expected_revision: int | None) -> Any:
        entity_id = getattr(entity, "id", None)
        if not entity_id or not is_dataclass(entity):
            raise ValueError("Aggregate must be a dataclass with an id")
        workspace, project = self._scope(scope)
        if hasattr(entity, "workspace_id") and str(getattr(entity, "workspace_id")) != str(scope.workspace_id):
            raise ValueError("Aggregate workspace does not match scope")
        if hasattr(entity, "project_id") and str(getattr(entity, "project_id")) != str(scope.project_id):
            raise ValueError("Aggregate project does not match scope")
        data = self.binding.encode(entity)
        if str(data.get("id")) != str(entity_id):
            raise ValueError("Aggregate codec changed its id")
        if expected_revision is None:
            revision = 1
            updated = replace(entity, revision=revision) if hasattr(entity, "revision") else entity
            data = self.binding.encode(updated)
            try:
                self.db.execute(
                    f"INSERT INTO {self.binding.table}(id,workspace_id,project_id,revision,payload) VALUES (?,?,?,?,?)",
                    (str(entity_id), workspace, project, revision, _dump(data)))
            except sqlite3.IntegrityError as exc:
                raise ValueError("Aggregate already exists") from exc
        else:
            revision = expected_revision + 1
            updated = replace(entity, revision=revision) if hasattr(entity, "revision") else entity
            data = self.binding.encode(updated)
            cursor = self.db.execute(
                f"UPDATE {self.binding.table} SET revision=?,payload=? WHERE id=? AND workspace_id IS ? "
                "AND project_id IS ? AND revision=?",
                (revision, _dump(data), str(entity_id), workspace, project, expected_revision))
            if cursor.rowcount != 1:
                raise ValueError("Aggregate revision conflict or not found")
        return updated

    async def delete(self, scope: Scope, entity_id: EntityId, expected_revision: int) -> None:
        workspace, project = self._scope(scope)
        cursor = self.db.execute(
            f"DELETE FROM {self.binding.table} WHERE id=? AND workspace_id IS ? AND project_id IS ? AND revision=?",
            (str(entity_id), workspace, project, expected_revision))
        if cursor.rowcount != 1:
            raise ValueError("Aggregate revision conflict or not found")


class SQLiteUnitOfWork:
    def __init__(self, factory: SQLiteUnitOfWorkFactory):
        self.factory = factory
        self.db: sqlite3.Connection | None = None
        self.finished = False

    async def __aenter__(self) -> SQLiteUnitOfWork:
        self.db = connect(self.factory.path)
        self.db.execute("BEGIN IMMEDIATE")
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None,
                        exc: BaseException | None, tb: TracebackType | None) -> bool:
        try:
            if not self.finished:
                await self.rollback()
        finally:
            assert self.db is not None
            self.db.close()
        return False

    def repository(self, entity_type: type[T]) -> SQLiteRepository:
        if self.db is None or self.finished:
            raise RuntimeError("Unit of work is not active")
        return SQLiteRepository(self.db, self.factory.bindings[entity_type])

    async def commit(self) -> None:
        if self.db is None or self.finished:
            raise RuntimeError("Unit of work is not active")
        try:
            self.db.commit()
        finally:
            self.finished = True
            self.db.close()

    async def rollback(self) -> None:
        if self.db is None or self.finished:
            raise RuntimeError("Unit of work is not active")
        try:
            self.db.rollback()
        finally:
            self.finished = True
            self.db.close()


class SQLiteUnitOfWorkFactory:
    def __init__(self, path: Path):
        self.path = path
        self.bindings: dict[type[Any], AggregateBinding] = {}
        migrate(path)

    def register(self, entity_type: type[T], table: str,
                 decode: Callable[[dict[str, Any]], T],
                 encode: Callable[[T], dict[str, Any]] | None = None) -> None:
        if not table.isidentifier() or not table.startswith("aggregate_"):
            raise ValueError("Aggregate table must be a fixed aggregate_* identifier")
        if entity_type in self.bindings or any(item.table == table for item in self.bindings.values()):
            raise ValueError("Aggregate is already registered")
        self.bindings[entity_type] = AggregateBinding(table, decode, encode or asdict)
        with connect(self.path) as db:
            db.execute(f"CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY, workspace_id TEXT, "
                       "project_id TEXT, revision INTEGER NOT NULL, payload TEXT NOT NULL)")
            db.execute(f"CREATE INDEX IF NOT EXISTS {table}_scope ON {table}(workspace_id,project_id,id)")

    def __call__(self) -> SQLiteUnitOfWork:
        return SQLiteUnitOfWork(self)


def create_adapter(settings: Settings) -> SQLiteUnitOfWorkFactory:
    return SQLiteUnitOfWorkFactory(database_path(settings))
