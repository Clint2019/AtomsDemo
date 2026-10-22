"""Permission check against workspace-owned membership records."""
import hashlib
import json

from atoms_demo.ports.runtime import Authorization
from atoms_demo.config import Settings, database_path
from atoms_demo.core.errors import DomainError, ErrorCode
from atoms_demo.core.types import EntityId, RequestContext
from atoms_demo.adapters.persistence import connect


def membership_id(workspace_id: EntityId, user_id: EntityId) -> EntityId:
    return EntityId(hashlib.sha256(f"{workspace_id}:{user_id}".encode("utf-8")).hexdigest())


class SQLiteAuthorization:
    def __init__(self, path):
        self.path = path

    async def require(self, ctx: RequestContext, action: str, resource_id: EntityId | None) -> None:
        project = None
        workspace_id = resource_id or ctx.workspace_id
        if action.startswith("project:"):
            if resource_id is None:
                raise DomainError(ErrorCode.INVALID_INPUT, "Project id required")
            with connect(self.path) as db:
                locator = db.execute("SELECT payload FROM aggregate_project_locators WHERE id=?",
                                     (str(resource_id),)).fetchone()
                if locator is not None:
                    project_workspace = json.loads(locator["payload"])["target_workspace_id"]
                    project_row = db.execute("SELECT payload FROM aggregate_projects WHERE id=? AND workspace_id=?",
                                             (str(resource_id), project_workspace)).fetchone()
                    project = json.loads(project_row["payload"]) if project_row else None
            if project is None:
                raise DomainError(ErrorCode.NOT_FOUND, "Project not found")
            workspace_id = EntityId(project["workspace_id"])
            if action == "project:read" and project["visibility"] == "public":
                return
        if ctx.actor_id is None:
            raise DomainError(ErrorCode.UNAUTHENTICATED, "Sign in required")
        if workspace_id is None:
            raise DomainError(ErrorCode.FORBIDDEN, "Workspace selection required")
        if ctx.workspace_id is not None and workspace_id != ctx.workspace_id:
            raise DomainError(ErrorCode.FORBIDDEN, "Different workspace")
        member_id = membership_id(workspace_id, ctx.actor_id)
        with connect(self.path) as db:
            row = db.execute("SELECT payload FROM aggregate_workspace_memberships WHERE id=? AND workspace_id=?",
                             (str(member_id), str(workspace_id))).fetchone()
        if row is None:
            raise DomainError(ErrorCode.FORBIDDEN, "Workspace access denied")
        role = json.loads(row["payload"])["role"]
        allowed = {
            "workspace:read": {"owner", "admin", "member", "viewer"},
            "workspace:manage": {"owner", "admin"},
            "workspace:owner": {"owner"},
            "workspace:write": {"owner", "admin", "member"},
            "project:read": {"owner", "admin", "member", "viewer"},
            "project:history": {"owner", "admin", "member", "viewer"},
            "project:write": {"owner", "admin", "member"},
        }.get(action, set())
        if role not in allowed:
            raise DomainError(ErrorCode.FORBIDDEN, "Action is not allowed")


def create_adapter(settings: Settings) -> Authorization:
    return SQLiteAuthorization(database_path(settings))
