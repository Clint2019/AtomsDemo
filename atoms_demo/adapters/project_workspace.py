"""Read-only lookup of the project owner workspace for app session creation."""
import json

from atoms_demo.adapters.persistence import connect
from atoms_demo.config import Settings, database_path
from atoms_demo.core.types import EntityId
from atoms_demo.ports.runtime import ProjectWorkspaceLookup


class SQLiteProjectWorkspaceLookup:
    def __init__(self, path):
        self.path = path

    async def workspace_for(self, project_id: EntityId) -> EntityId | None:
        with connect(self.path) as db:
            row = db.execute("SELECT payload FROM aggregate_project_locators WHERE id=?",
                             (str(project_id),)).fetchone()
        return EntityId(json.loads(row["payload"])["target_workspace_id"]) if row else None


def create_adapter(settings: Settings) -> ProjectWorkspaceLookup:
    return SQLiteProjectWorkspaceLookup(database_path(settings))
