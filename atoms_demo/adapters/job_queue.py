"""SQLite job queue with expiring leases and idempotent enqueue."""
from datetime import datetime, timedelta, timezone

from atoms_demo.ports.runtime import JobQueue
from atoms_demo.config import Settings, database_path
from atoms_demo.core.types import EntityId
from atoms_demo.adapters.persistence import connect, migrate


class SQLiteJobQueue:
    def __init__(self, path, lease_seconds: int):
        self.path = path
        self.lease_seconds = lease_seconds
        migrate(path)

    async def enqueue(self, task_id: EntityId, idempotency_key: str) -> None:
        if not idempotency_key:
            raise ValueError("Idempotency key is required")
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT task_id,idempotency_key FROM jobs WHERE task_id=? OR idempotency_key=?",
                                  (str(task_id), idempotency_key)).fetchone()
            if existing:
                if existing["task_id"] != str(task_id) or existing["idempotency_key"] != idempotency_key:
                    raise ValueError("Idempotency key or task id conflict")
                return
            db.execute("INSERT INTO jobs(task_id,idempotency_key,created_at) VALUES (?,?,?)",
                       (str(task_id), idempotency_key, datetime.now(timezone.utc).isoformat()))
            db.commit()

    async def lease(self, worker_id: str) -> EntityId | None:
        if not worker_id:
            raise ValueError("Worker id is required")
        now = datetime.now(timezone.utc)
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT task_id FROM jobs WHERE state='pending' OR "
                             "(state='leased' AND leased_until<=?) ORDER BY created_at LIMIT 1",
                             (now.isoformat(),)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET state='leased',worker_id=?,leased_until=? WHERE task_id=?",
                       (worker_id, (now + timedelta(seconds=self.lease_seconds)).isoformat(), row["task_id"]))
            db.commit()
            return EntityId(row["task_id"])

    async def acknowledge(self, task_id: EntityId, worker_id: str) -> None:
        with connect(self.path) as db:
            cursor = db.execute("UPDATE jobs SET state='done',leased_until=NULL WHERE task_id=? AND "
                                "worker_id=? AND state='leased' AND leased_until>?",
                                (str(task_id), worker_id, datetime.now(timezone.utc).isoformat()))
            if cursor.rowcount != 1:
                raise ValueError("Job lease is not owned by this worker")

    async def heartbeat(self, task_id: EntityId, worker_id: str) -> None:
        now = datetime.now(timezone.utc)
        with connect(self.path) as db:
            cursor = db.execute("UPDATE jobs SET leased_until=? WHERE task_id=? AND worker_id=? "
                                "AND state='leased' AND leased_until>?",
                                ((now + timedelta(seconds=self.lease_seconds)).isoformat(),
                                 str(task_id), worker_id, now.isoformat()))
            if cursor.rowcount != 1:
                raise ValueError("Job lease is not owned by this worker")


def create_adapter(settings: Settings) -> JobQueue:
    return SQLiteJobQueue(database_path(settings), settings.queue_lease_seconds)
