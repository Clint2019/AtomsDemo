"""Read-only attachment ownership check at the persistence boundary."""
import json

from atoms_demo.adapters.persistence import connect
from atoms_demo.config import Settings, database_path
from atoms_demo.core.types import EntityId


class SQLiteAttachmentAccess:
    def __init__(self, path):
        self.path = path

    async def exists(self, workspace_id: EntityId, project_id: EntityId, object_id: EntityId) -> bool:
        with connect(self.path) as db:
            row = db.execute(
                "SELECT payload FROM aggregate_storage_objects WHERE id=? AND workspace_id=? AND project_id=?",
                (str(object_id), str(workspace_id), str(project_id))).fetchone()
        return bool(row and not json.loads(row["payload"])["deleted"])


def create_adapter(settings: Settings) -> SQLiteAttachmentAccess:
    return SQLiteAttachmentAccess(database_path(settings))
