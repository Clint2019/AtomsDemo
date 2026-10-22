"""Read-only platform session validation for scoped application data."""
from __future__ import annotations

import json
from datetime import datetime

from atoms_demo.adapters.persistence import connect
from atoms_demo.config import Settings, database_path
from atoms_demo.core.types import EntityId
from atoms_demo.ports.runtime import PlatformSessionLookup


class SQLitePlatformSessionLookup:
    def __init__(self, path):
        self.path = path

    async def actor_for(self, session_id: EntityId, now: datetime) -> EntityId | None:
        with connect(self.path) as db:
            row = db.execute(
                "SELECT payload FROM aggregate_identity_sessions "
                "WHERE id=? AND workspace_id IS NULL AND project_id IS NULL",
                (str(session_id),)).fetchone()
        if row is None:
            return None
        record = json.loads(row["payload"])
        if datetime.fromisoformat(record["expires_at"]) <= now:
            return None
        return EntityId(record["user_id"])


def create_adapter(settings: Settings) -> PlatformSessionLookup:
    return SQLitePlatformSessionLookup(database_path(settings))
