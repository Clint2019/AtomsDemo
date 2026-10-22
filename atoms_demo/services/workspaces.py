"""Workspace ownership, membership roles, invitations and session selection."""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import quote

from atoms_demo.contracts.workspaces import Role, Workspace, Membership, Invitation, WorkspaceAPI
from atoms_demo.core.errors import DomainError, ErrorCode
from atoms_demo.core.types import EntityId, Mutation, Page, PageQuery, RequestContext, Scope
from atoms_demo.ports.dependencies import WorkspaceDependencies

_GLOBAL = Scope(workspace_id=None)


@dataclass(frozen=True)
class UserWorkspaceIndex:
    id: EntityId
    workspace_ids: tuple[EntityId, ...]
    revision: int = 0


@dataclass(frozen=True)
class InvitationRecord:
    id: EntityId
    target_workspace_id: EntityId
    email: str
    role: Role
    expires_at: datetime
    revision: int = 0


@dataclass(frozen=True)
class WorkspaceSelection:
    id: EntityId
    selected_workspace_id: EntityId
    revision: int = 0


@dataclass(frozen=True)
class WorkspaceMutation:
    id: EntityId
    result_id: EntityId
    input_digest: EntityId
    revision: int = 0


def decode_invitation(data: dict) -> InvitationRecord:
    return InvitationRecord(**{**data, "role": Role(data["role"]),
                               "expires_at": datetime.fromisoformat(data["expires_at"])})


def _hash(value: str) -> EntityId:
    return EntityId(hashlib.sha256(value.encode("utf-8")).hexdigest())


def _membership_id(workspace_id: EntityId, user_id: EntityId) -> EntityId:
    return _hash(f"{workspace_id}:{user_id}")


class WorkspaceService:
    def __init__(self, dependencies: WorkspaceDependencies) -> None:
        self.dependencies = dependencies

    @staticmethod
    def _actor(ctx: RequestContext) -> EntityId:
        if ctx.actor_id is None:
            raise DomainError(ErrorCode.UNAUTHENTICATED, "Sign in required")
        return ctx.actor_id

    async def _member(self, uow, workspace_id: EntityId, user_id: EntityId) -> Membership | None:
        return await uow.repository(Membership).get(
            Scope(workspace_id=workspace_id), _membership_id(workspace_id, user_id))

    async def _require_role(self, uow, ctx: RequestContext, roles: set[Role]) -> Membership:
        actor = self._actor(ctx)
        if ctx.workspace_id is None:
            raise DomainError(ErrorCode.FORBIDDEN, "Workspace selection required")
        member = await self._member(uow, ctx.workspace_id, actor)
        if member is None or member.role not in roles:
            raise DomainError(ErrorCode.FORBIDDEN, "Workspace access denied")
        return member

    async def _index_add(self, uow, user_id: EntityId, workspace_id: EntityId) -> None:
        repo = uow.repository(UserWorkspaceIndex)
        current = await repo.get(_GLOBAL, user_id)
        if current and workspace_id in current.workspace_ids:
            return
        ids = (*current.workspace_ids, workspace_id) if current else (workspace_id,)
        await repo.save(_GLOBAL, UserWorkspaceIndex(id=user_id, workspace_ids=ids),
                        current.revision if current else None)

    async def _index_remove(self, uow, user_id: EntityId, workspace_id: EntityId) -> None:
        repo = uow.repository(UserWorkspaceIndex)
        current = await repo.get(_GLOBAL, user_id)
        if current and workspace_id in current.workspace_ids:
            await repo.save(_GLOBAL, UserWorkspaceIndex(
                id=user_id, workspace_ids=tuple(item for item in current.workspace_ids if item != workspace_id)),
                current.revision)

    async def create(self, ctx: RequestContext, name: str, mutation: Mutation) -> Workspace:
        actor = self._actor(ctx)
        if not mutation.idempotency_key:
            raise DomainError(ErrorCode.INVALID_INPUT, "Idempotency key is required")
        clean = name.strip()
        if not 1 <= len(clean) <= 100:
            raise DomainError(ErrorCode.INVALID_INPUT, "Workspace name must be 1–100 characters")
        mutation_id = _hash(f"workspace:create:{actor}:{mutation.idempotency_key}")
        async with self.dependencies.uow() as uow:
            previous = await uow.repository(WorkspaceMutation).get(_GLOBAL, mutation_id)
            if previous:
                if previous.input_digest != _hash(clean):
                    raise DomainError(ErrorCode.CONFLICT, "Idempotency key was used with different input")
                workspace = await uow.repository(Workspace).get(
                    Scope(workspace_id=previous.result_id), previous.result_id)
                if workspace is None:
                    raise DomainError(ErrorCode.CONFLICT, "Workspace creation record is inconsistent")
                return workspace
            workspace = Workspace(id=self.dependencies.ids.new(), name=clean, revision=0)
            scope = Scope(workspace_id=workspace.id)
            workspace = await uow.repository(Workspace).save(scope, workspace, None)
            await uow.repository(Membership).save(
                scope, Membership(id=_membership_id(workspace.id, actor), workspace_id=workspace.id,
                                  user_id=actor, role=Role.OWNER, revision=0), None)
            await self._index_add(uow, actor, workspace.id)
            await uow.repository(WorkspaceMutation).save(
                _GLOBAL, WorkspaceMutation(id=mutation_id, result_id=workspace.id, input_digest=_hash(clean)), None)
            await uow.commit()
        return workspace

    async def list_workspaces(self, ctx: RequestContext, page: PageQuery) -> Page[Workspace]:
        actor = self._actor(ctx)
        if not 1 <= page.limit <= 100:
            raise DomainError(ErrorCode.INVALID_INPUT, "Page limit must be 1–100")
        async with self.dependencies.uow() as uow:
            index = await uow.repository(UserWorkspaceIndex).get(_GLOBAL, actor)
            ids = sorted(item for item in (index.workspace_ids if index else ()) if item > (page.cursor or ""))
            selected = ids[:page.limit + 1]
            items = []
            for workspace_id in selected[:page.limit]:
                workspace = await uow.repository(Workspace).get(Scope(workspace_id=workspace_id), workspace_id)
                if workspace:
                    items.append(workspace)
        return Page(items=tuple(items), next_cursor=selected[page.limit - 1] if len(selected) > page.limit else None)

    async def select(self, ctx: RequestContext, workspace_id: EntityId) -> Workspace:
        actor = self._actor(ctx)
        if ctx.session_id is None:
            raise DomainError(ErrorCode.UNAUTHENTICATED, "Session required")
        async with self.dependencies.uow() as uow:
            if not await self._member(uow, workspace_id, actor):
                raise DomainError(ErrorCode.FORBIDDEN, "Workspace access denied")
            workspace = await uow.repository(Workspace).get(Scope(workspace_id=workspace_id), workspace_id)
            if workspace is None:
                raise DomainError(ErrorCode.NOT_FOUND, "Workspace not found")
            repo = uow.repository(WorkspaceSelection)
            current = await repo.get(_GLOBAL, ctx.session_id)
            await repo.save(_GLOBAL, WorkspaceSelection(id=ctx.session_id, selected_workspace_id=workspace_id),
                            current.revision if current else None)
            await uow.commit()
        return workspace

    async def selected_workspace(self, session_id: EntityId) -> EntityId | None:
        async with self.dependencies.uow() as uow:
            selection = await uow.repository(WorkspaceSelection).get(_GLOBAL, session_id)
        return selection.selected_workspace_id if selection else None

    async def members(self, ctx: RequestContext, page: PageQuery) -> Page[Membership]:
        async with self.dependencies.uow() as uow:
            await self._require_role(uow, ctx, set(Role))
            return await uow.repository(Membership).list(Scope(workspace_id=ctx.workspace_id), page)

    async def invite(self, ctx: RequestContext, email: str, role: Role, mutation: Mutation) -> Invitation:
        clean = email.strip().casefold()
        if not clean or "@" not in clean or role is Role.OWNER:
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid invitation")
        raw = secrets.token_urlsafe(40)
        now = self.dependencies.clock.now()
        async with self.dependencies.uow() as uow:
            await self._require_role(uow, ctx, {Role.OWNER, Role.ADMIN})
            record = await uow.repository(InvitationRecord).save(
                _GLOBAL, InvitationRecord(id=_hash(raw), target_workspace_id=ctx.workspace_id,
                                          email=clean, role=role, expires_at=now + timedelta(days=7)), None)
            await uow.commit()
        url = f"{self.dependencies.public_origin}/invitations/accept#token={quote(raw)}"
        await self.dependencies.mail.send_invitation(clean, url, mutation.idempotency_key)
        return Invitation(id=record.id, workspace_id=record.target_workspace_id, email=record.email,
                          role=record.role, expires_at=record.expires_at)

    async def accept_invitation(self, ctx: RequestContext, token: str) -> Membership:
        actor = self._actor(ctx)
        actual_email = await self.dependencies.users.email_for(actor)
        if actual_email is None:
            raise DomainError(ErrorCode.FORBIDDEN, "Verified email required")
        async with self.dependencies.uow() as uow:
            repo = uow.repository(InvitationRecord)
            record = await repo.get(_GLOBAL, _hash(token))
            if not record or record.expires_at <= self.dependencies.clock.now():
                raise DomainError(ErrorCode.INVALID_INPUT, "Invitation is invalid or expired")
            if record.email != actual_email:
                raise DomainError(ErrorCode.FORBIDDEN, "Invitation belongs to another email")
            if await self._member(uow, record.target_workspace_id, actor):
                raise DomainError(ErrorCode.CONFLICT, "Already a workspace member")
            membership = await uow.repository(Membership).save(
                Scope(workspace_id=record.target_workspace_id),
                Membership(id=_membership_id(record.target_workspace_id, actor),
                           workspace_id=record.target_workspace_id, user_id=actor,
                           role=record.role, revision=0), None)
            await self._index_add(uow, actor, record.target_workspace_id)
            await repo.delete(_GLOBAL, record.id, record.revision)
            await uow.commit()
        return membership

    async def _owner_count(self, uow, workspace_id: EntityId) -> int:
        count = 0
        cursor = None
        while True:
            page = await uow.repository(Membership).list(Scope(workspace_id=workspace_id),
                                                         PageQuery(cursor=cursor, limit=100))
            count += sum(member.role is Role.OWNER for member in page.items)
            if page.next_cursor is None:
                return count
            cursor = page.next_cursor

    async def change_role(self, ctx: RequestContext, member_id: EntityId, role: Role,
                          mutation: Mutation) -> Membership:
        async with self.dependencies.uow() as uow:
            await self._require_role(uow, ctx, {Role.OWNER})
            scope = Scope(workspace_id=ctx.workspace_id)
            current = await uow.repository(Membership).get(scope, member_id)
            if current is None:
                raise DomainError(ErrorCode.NOT_FOUND, "Member not found")
            if current.role is Role.OWNER and role is not Role.OWNER and await self._owner_count(uow, ctx.workspace_id) <= 1:
                raise DomainError(ErrorCode.CONFLICT, "Cannot remove the last owner")
            if mutation.expected_revision is not None and mutation.expected_revision != current.revision:
                raise DomainError(ErrorCode.CONFLICT, "Member revision changed")
            member = await uow.repository(Membership).save(
                scope, Membership(id=current.id, workspace_id=current.workspace_id,
                                  user_id=current.user_id, role=role, revision=current.revision), current.revision)
            await uow.commit()
        return member

    async def remove_member(self, ctx: RequestContext, member_id: EntityId, mutation: Mutation) -> None:
        async with self.dependencies.uow() as uow:
            await self._require_role(uow, ctx, {Role.OWNER, Role.ADMIN})
            scope = Scope(workspace_id=ctx.workspace_id)
            current = await uow.repository(Membership).get(scope, member_id)
            if current is None:
                raise DomainError(ErrorCode.NOT_FOUND, "Member not found")
            if current.role is Role.OWNER:
                if await self._owner_count(uow, ctx.workspace_id) <= 1:
                    raise DomainError(ErrorCode.CONFLICT, "Cannot remove the last owner")
                await self._require_role(uow, ctx, {Role.OWNER})
            if mutation.expected_revision is not None and mutation.expected_revision != current.revision:
                raise DomainError(ErrorCode.CONFLICT, "Member revision changed")
            await uow.repository(Membership).delete(scope, member_id, current.revision)
            await self._index_remove(uow, current.user_id, ctx.workspace_id)
            await uow.commit()
