from dataclasses import dataclass
from datetime import datetime

from atoms_demo.core.types import EntityId


@dataclass(frozen=True, kw_only=True)
class StorageMetering:
    event_id: EntityId
    workspace_id: EntityId
    project_id: EntityId | None
    byte_delta: int
    occurred_at: datetime
    billable: bool = False
