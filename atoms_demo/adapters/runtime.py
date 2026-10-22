"""System clock and opaque identifier implementations."""
from datetime import datetime, timezone
from uuid import uuid4

from atoms_demo.core.types import EntityId


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class UUIDFactory:
    def new(self) -> EntityId:
        return EntityId(uuid4().hex)
