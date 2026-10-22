from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from atoms_demo.core.types import (
    BlobRef, EntityId, Environment, Mutation, OperationReceipt,
    Page, PageQuery, RequestContext, Scope, SecretRef,
)

@dataclass(frozen=True, kw_only=True)
class User:
    id: EntityId
    email: str
    verified: bool
    revision: int

@dataclass(frozen=True, kw_only=True)
class Credentials:
    email: str
    password: str
    captcha_token: str | None
    accepted_terms: bool

@dataclass(frozen=True, kw_only=True)
class Session:
    id: EntityId
    user_id: EntityId
    expires_at: datetime

@dataclass(frozen=True, kw_only=True)
class CurrentSession:
    user_id: EntityId
    workspace_id: EntityId | None

@dataclass(frozen=True, kw_only=True)
class EmailCheck:
    next_step: str

@dataclass(frozen=True, kw_only=True)
class VerificationRequest:
    email: str
    mutation: Mutation

@dataclass(frozen=True, kw_only=True)
class OAuthCallback:
    provider: str
    code: str
    state: str

class IdentityAPI(Protocol):
    async def me(self, ctx: RequestContext) -> CurrentSession: ...
    async def check_email(self, ctx: RequestContext, email: str) -> EmailCheck: ...
    async def register(self, ctx: RequestContext, command: Credentials) -> User: ...
    async def login(self, ctx: RequestContext, command: Credentials) -> Session: ...
    async def request_verification(self, ctx: RequestContext, command: VerificationRequest) -> OperationReceipt: ...
    async def verify(self, ctx: RequestContext, token: str) -> Session: ...
    async def oauth_start(self, ctx: RequestContext, provider: str) -> str: ...
    async def oauth_callback(self, ctx: RequestContext, command: OAuthCallback) -> Session: ...
    async def authenticate(self, session_token: str) -> RequestContext: ...
    async def logout(self, ctx: RequestContext) -> None: ...
    async def change_email(self, ctx: RequestContext, email: str, mutation: Mutation) -> User: ...
