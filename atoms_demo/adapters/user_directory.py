"""Read-only lookup of identity-owned email data."""
import json

from atoms_demo.config import Settings, database_path
from atoms_demo.core.types import EntityId
from atoms_demo.adapters.persistence import connect


class SQLiteUserDirectory:
    def __init__(self, path):
        self.path = path

    async def email_for(self, user_id: EntityId) -> str | None:
        with connect(self.path) as db:
            row = db.execute("SELECT payload FROM aggregate_identity_accounts WHERE id=?", (str(user_id),)).fetchone()
        account = json.loads(row["payload"]) if row else None
        return account["email"] if account and account["verified"] else None


def create_adapter(settings: Settings) -> SQLiteUserDirectory:
    return SQLiteUserDirectory(database_path(settings))
