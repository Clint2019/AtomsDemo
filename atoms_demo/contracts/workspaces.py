from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from atoms_demo.core.types import (
    BlobRef, EntityId, Environment, Mutation, OperationReceipt,
    Page, PageQuery, RequestContext, Scope, SecretRef,
)

class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"

@dataclass(frozen=True, kw_only=True)
class Workspace:
    id: EntityId
    name: str
    revision: int

@dataclass(frozen=True, kw_only=True)
class Membership:
    id: EntityId
    workspace_id: EntityId
    user_id: EntityId
    role: Role
    revision: int

@dataclass(frozen=True, kw_only=True)
class Invitation:
    id: EntityId
    workspace_id: EntityId
    email: str
    role: Role
    expires_at: datetime

class WorkspaceAPI(Protocol):
    async def create(self, ctx: RequestContext, name: str, mutation: Mutation) -> Workspace: ...
    async def list_workspaces(self, ctx: RequestContext, page: PageQuery) -> Page[Workspace]: ...
    async def select(self, ctx: RequestContext, workspace_id: EntityId) -> Workspace: ...
    async def selected_workspace(self, session_id: EntityId) -> EntityId | None: ...
    async def members(self, ctx: RequestContext, page: PageQuery) -> Page[Membership]: ...
    async def invite(self, ctx: RequestContext, email: str, role: Role, mutation: Mutation) -> Invitation: ...
    async def accept_invitation(self, ctx: RequestContext, token: str) -> Membership: ...
    async def change_role(self, ctx: RequestContext, member_id: EntityId, role: Role, mutation: Mutation) -> Membership: ...
    async def remove_member(self, ctx: RequestContext, member_id: EntityId, mutation: Mutation) -> None: ...
