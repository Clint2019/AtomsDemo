from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Generic, NewType, TypeVar

EntityId = NewType("EntityId", str)
T = TypeVar("T")


@dataclass(frozen=True, kw_only=True)
class RequestContext:
    """Constructed by trusted transport after authentication, never from body fields."""
    actor_id: EntityId | None
    session_id: EntityId | None
    workspace_id: EntityId | None
    request_id: str
    client_binding: str | None = None


@dataclass(frozen=True, kw_only=True)
class Scope:
    workspace_id: EntityId | None
    project_id: EntityId | None = None


@dataclass(frozen=True, kw_only=True)
class PageQuery:
    cursor: str | None = None
    limit: int = 20


@dataclass(frozen=True, kw_only=True)
class Page(Generic[T]):
    items: tuple[T, ...]
    next_cursor: str | None


@dataclass(frozen=True, kw_only=True)
class Mutation:
    """Idempotency key is scoped by actor + operation; revision enables optimistic locking."""
    idempotency_key: str
    expected_revision: int | None = None


class Environment(StrEnum):
    DEVELOPMENT = "development"
    PRODUCTION = "production"
    PUBLIC_PREVIEW = "public_preview"


@dataclass(frozen=True, kw_only=True)
class BlobRef:
    id: EntityId
    sha256: str
    size_bytes: int
    media_type: str


@dataclass(frozen=True, kw_only=True)
class SecretRef:
    """Reference only; API serializers must never resolve it into secret material."""
    id: EntityId


@dataclass(frozen=True, kw_only=True)
class OperationReceipt:
    id: EntityId
    revision: int
    recorded_at: datetime


@dataclass(frozen=True, kw_only=True)
class PreviewGrant:
    """Internal, validated capability. Never accepted from an HTTP request body."""
    project_id: EntityId
    version_id: EntityId
    artifact: BlobRef
    environment: Environment
    expires_at: datetime
    public_author_id: EntityId | None = None
