"""Stable project-specific hosts for untrusted preview code."""
from __future__ import annotations

import hashlib
from urllib.parse import urlsplit

from atoms_demo.core.types import EntityId, Environment


def project_preview_origin(base_origin: str, project_id: EntityId) -> str:
    parsed = urlsplit(base_origin)
    label = hashlib.sha256(str(project_id).encode("utf-8")).hexdigest()[:32]
    host = f"p-{label}.{parsed.hostname}"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port}"


def version_preview_origin(base_origin: str, project_id: EntityId,
                           version_id: EntityId,
                           environment: Environment = Environment.DEVELOPMENT) -> str:
    project = urlsplit(project_preview_origin(base_origin, project_id))
    label = hashlib.sha256(str(version_id).encode("utf-8")).hexdigest()[:32]
    port = f":{project.port}" if project.port else ""
    prefix = "c" if environment == Environment.PUBLIC_PREVIEW else "v"
    return f"{project.scheme}://{prefix}-{label}.{project.hostname}{port}"
