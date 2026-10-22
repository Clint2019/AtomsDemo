"""Read-only public project and successful version evidence for catalog entries."""
import json

from atoms_demo.adapters.persistence import connect
from atoms_demo.config import Settings, database_path
from atoms_demo.core.types import EntityId
from atoms_demo.ports.runtime import PublicCatalogSource


class SQLitePublicCatalogSource:
    def __init__(self, path):
        self.path = path

    async def author_for(self, project_id: EntityId, version_id: EntityId) -> EntityId | None:
        with connect(self.path) as db:
            locator = db.execute("SELECT payload FROM aggregate_project_locators WHERE id=?",
                                 (str(project_id),)).fetchone()
            if locator is None:
                return None
            workspace_id = json.loads(locator["payload"])["target_workspace_id"]
            project_row = db.execute("SELECT payload FROM aggregate_projects WHERE id=? AND workspace_id=?",
                                     (str(project_id), workspace_id)).fetchone()
            version_row = db.execute("SELECT payload FROM aggregate_versions WHERE id=? AND workspace_id=? AND project_id=?",
                                     (str(version_id), workspace_id, str(project_id))).fetchone()
            build_row = db.execute("SELECT payload FROM aggregate_version_builds WHERE id=?",
                                   (str(version_id),)).fetchone()
        if project_row is None or version_row is None or build_row is None:
            return None
        project = json.loads(project_row["payload"])
        build = json.loads(build_row["payload"])
        if project["visibility"] != "public" or build["build_status"] != "success":
            return None
        return EntityId(project["owner_id"])


def create_adapter(settings: Settings) -> PublicCatalogSource:
    return SQLitePublicCatalogSource(database_path(settings))
