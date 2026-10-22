from dataclasses import dataclass
from enum import StrEnum


class ErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    UNAUTHENTICATED = "unauthenticated"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    QUOTA_EXCEEDED = "quota_exceeded"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    NOT_IMPLEMENTED = "not_implemented"
    INTERNAL = "internal_error"


@dataclass(frozen=True)
class ErrorResponse:
    code: ErrorCode
    message: str
    request_id: str
    retryable: bool = False


class DomainError(Exception):
    """Expected business failure with a transport-safe message."""

    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class MeteredProviderError(DomainError):
    """A provider call failed after reporting billable token usage."""

    def __init__(self, message: str, input_tokens: int, output_tokens: int):
        super().__init__(ErrorCode.PROVIDER_UNAVAILABLE, message)
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class UnsentModelError(DomainError):
    """Local validation failed before any model request was dispatched."""


class FeatureNotImplemented(NotImplementedError):
    """Scaffold boundary; must not become a fake successful API response."""
