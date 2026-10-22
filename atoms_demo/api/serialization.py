"""JSON serialization for public immutable DTOs."""
import json
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum

from atoms_demo.core.errors import ErrorResponse
from atoms_demo.core.types import SecretRef

_SENSITIVE = {"password", "secret", "token", "credential", "ciphertext", "authorization", "api_key"}


def _is_sensitive(name: str) -> bool:
    lower = name.lower()
    return lower in _SENSITIVE or any(lower.endswith("_" + item) for item in _SENSITIVE)


def _public(value: object) -> object:
    if isinstance(value, SecretRef):
        return None
    if is_dataclass(value):
        module = type(value).__module__
        if not module.startswith(("atoms_demo.contracts.", "atoms_demo.core.", "atoms_demo.workflows.")):
            raise TypeError("Only public DTOs can be serialized")
        return {field.name: _public(getattr(value, field.name)) for field in fields(value)
                if not _is_sensitive(field.name)}
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Response timestamps must include timezone")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (tuple, list)):
        return [_public(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _public(item) for key, item in value.items()
                if not _is_sensitive(str(key))}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Unsupported public response type: {type(value).__name__}")


def encode_response(value: object) -> bytes:
    return json.dumps(_public(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def encode_error(error: ErrorResponse) -> bytes:
    return json.dumps({"code": error.code.value, "message": error.message,
                       "request_id": error.request_id, "retryable": error.retryable},
                      separators=(",", ":")).encode("utf-8")
