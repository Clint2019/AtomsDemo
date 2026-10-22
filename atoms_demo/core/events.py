from dataclasses import dataclass
from datetime import datetime
from atoms_demo.core.types import EntityId


@dataclass(frozen=True, kw_only=True)
class TaskEvent:
    event_id: EntityId
    task_id: EntityId
    project_id: EntityId
    sequence: int
    kind: str
    message: str
    created_at: datetime
    version_id: EntityId | None = None
