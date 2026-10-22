"""Version fixed preview tickets, revocable shares and scoped issue reports."""
from __future__ import annotations

import hashlib
import ipaddress
import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from atoms_demo.contracts.preview import PreviewTicket, RuntimeIssue, ShareLink
from atoms_demo.core.errors import DomainError, ErrorCode
from atoms_demo.core.preview_origin import version_preview_origin
from atoms_demo.core.types import (BlobRef, EntityId, Environment, Mutation,
                                   OperationReceipt, PreviewGrant, RequestContext, Scope, SecretRef)
from atoms_demo.ports.dependencies import PreviewDependencies

GLOBAL = Scope(workspace_id=None)
TICKET_LIFETIME = timedelta(minutes=15)


@dataclass(frozen=True)
class TicketRecord:
    id: EntityId
    target_workspace_id: EntityId
    target_project_id: EntityId
    version_id: EntityId
    artifact: BlobRef
    environment: Environment
    expires_at: datetime
    share_id: EntityId | None = None
    revoked: bool = False
    revision: int = 0


@dataclass(frozen=True)
class CatalogTicketMarker:
    id: EntityId
    author_id: EntityId
    revision: int = 0


@dataclass(frozen=True)
class ShareRecord:
    id: EntityId
    target_workspace_id: EntityId
    target_project_id: EntityId
    version_id: EntityId
    artifact: BlobRef
    token_ref: SecretRef
    token_digest: EntityId
    input_digest: EntityId
    expires_at: datetime | None
    revoked: bool = False
    revision: int = 0


@dataclass(frozen=True)
class ShareLookup:
    id: EntityId
    share_id: EntityId
    revision: int = 0


@dataclass(frozen=True)
class IssueRecord:
    id: EntityId
    project_id: EntityId
    version_id: EntityId
    message: str
    stack: str | None
    recorded_at: datetime
    revision: int = 0


def _hash(value: str) -> EntityId:
    return EntityId(hashlib.sha256(value.encode("utf-8")).hexdigest())


def _preview_url(origin: str, project_id: EntityId, version_id: EntityId,
                 environment: Environment, kind: str, token: str) -> str:
    try:
        address = ipaddress.ip_address(urlsplit(origin).hostname or "")
    except ValueError:
        return (f"{version_preview_origin(origin, project_id, version_id, environment)}"
                f"/{kind}/{token}")
    if not (address.version == 4 and address.is_global):
        return (f"{version_preview_origin(origin, project_id, version_id, environment)}"
                f"/{kind}/{token}")
    return f"{origin}/{'p' if kind == 'preview' else 's'}/{token}/"


def _blob(data: dict) -> BlobRef:
    return BlobRef(**data)


def _secret(data: dict) -> SecretRef:
    return SecretRef(**data)


def decode_ticket(data: dict) -> TicketRecord:
    return TicketRecord(**{**data, "artifact": _blob(data["artifact"]),
                           "environment": Environment(data["environment"]),
                           "expires_at": datetime.fromisoformat(data["expires_at"])})


def decode_share(data: dict) -> ShareRecord:
    return ShareRecord(**{**data, "artifact": _blob(data["artifact"]),
                          "token_ref": _secret(data["token_ref"]),
                          "expires_at": datetime.fromisoformat(data["expires_at"])
                          if data["expires_at"] else None})


def decode_issue(data: dict) -> IssueRecord:
    return IssueRecord(**{**data, "recorded_at": datetime.fromisoformat(data["recorded_at"])})


class PreviewService:
    def __init__(self, dependencies: PreviewDependencies) -> None:
        self.dependencies = dependencies

    def _check_grant(self, grant: PreviewGrant) -> None:
        now = self.dependencies.clock.now()
        if (grant.environment not in {Environment.DEVELOPMENT, Environment.PUBLIC_PREVIEW} or
                (grant.environment == Environment.PUBLIC_PREVIEW) != (grant.public_author_id is not None) or
                grant.expires_at <= now or
                grant.expires_at > now + TICKET_LIFETIME):
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid preview grant")

    async def _issue(self, workspace_id: EntityId, grant: PreviewGrant,
                     share_id: EntityId | None = None) -> PreviewTicket:
        self._check_grant(grant)
        await self.dependencies.blobs.read(grant.artifact)
        token = secrets.token_urlsafe(32)
        record = TicketRecord(id=_hash(token), target_workspace_id=workspace_id,
            target_project_id=grant.project_id, version_id=grant.version_id,
            artifact=grant.artifact, environment=grant.environment,
            expires_at=grant.expires_at, share_id=share_id)
        async with self.dependencies.uow() as uow:
            await uow.repository(TicketRecord).save(GLOBAL, record, None)
            if grant.public_author_id is not None:
                await uow.repository(CatalogTicketMarker).save(GLOBAL,
                    CatalogTicketMarker(id=record.id, author_id=grant.public_author_id), None)
            await uow.commit()
        return PreviewTicket(id=record.id, project_id=grant.project_id,
            version_id=grant.version_id,
            url=_preview_url(self.dependencies.preview_origin, grant.project_id,
                grant.version_id, grant.environment, "preview", token),
            expires_at=grant.expires_at)

    async def issue_ticket(self, ctx: RequestContext, grant: PreviewGrant) -> PreviewTicket:
        if grant.environment != Environment.DEVELOPMENT:
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid owner preview grant")
        await self.dependencies.authorization.require(ctx, "project:history", grant.project_id)
        if ctx.workspace_id is None:
            raise DomainError(ErrorCode.FORBIDDEN, "Workspace selection required")
        return await self._issue(ctx.workspace_id, grant)

    async def issue_catalog_ticket(self, grant: PreviewGrant,
                                   author_id: EntityId) -> PreviewTicket:
        if (grant.environment != Environment.PUBLIC_PREVIEW or grant.public_author_id != author_id or
                await self.dependencies.source.author_for(grant.project_id, grant.version_id) != author_id):
            raise DomainError(ErrorCode.NOT_FOUND, "Public work is unavailable")
        workspace_id = await self.dependencies.projects.workspace_for(grant.project_id)
        if workspace_id is None:
            raise DomainError(ErrorCode.NOT_FOUND, "Public work is unavailable")
        return await self._issue(workspace_id, grant)

    async def revoke_ticket(self, ctx: RequestContext, ticket_id: EntityId,
                            mutation: Mutation) -> None:
        if not mutation.idempotency_key:
            raise DomainError(ErrorCode.INVALID_INPUT, "Idempotency key required")
        async with self.dependencies.uow() as uow:
            record = await uow.repository(TicketRecord).get(GLOBAL, ticket_id)
        if record is None:
            raise DomainError(ErrorCode.NOT_FOUND, "Preview ticket not found")
        await self.dependencies.authorization.require(ctx, "project:write", record.target_project_id)
        if ctx.workspace_id != record.target_workspace_id:
            raise DomainError(ErrorCode.FORBIDDEN, "Different workspace")
        async with self.dependencies.uow() as uow:
            repo = uow.repository(TicketRecord)
            current = await repo.get(GLOBAL, ticket_id)
            if current and not current.revoked:
                await repo.save(GLOBAL, replace(current, revoked=True), current.revision)
                await uow.commit()

    async def resolve_ticket(self, token: str) -> PreviewGrant:
        if not token or len(token) > 128:
            raise DomainError(ErrorCode.NOT_FOUND, "Preview ticket not found")
        async with self.dependencies.uow() as uow:
            record = await uow.repository(TicketRecord).get(GLOBAL, _hash(token))
            share = await uow.repository(ShareRecord).get(GLOBAL, record.share_id) if record and record.share_id else None
            public_marker = await uow.repository(CatalogTicketMarker).get(GLOBAL, record.id) if record and record.environment == Environment.PUBLIC_PREVIEW else None
        if record is None or record.revoked or record.expires_at <= self.dependencies.clock.now() or (
                record.share_id and (share is None or share.revoked or
                (share.expires_at and share.expires_at <= self.dependencies.clock.now()))):
            raise DomainError(ErrorCode.NOT_FOUND, "Preview ticket is unavailable")
        if (record.environment == Environment.PUBLIC_PREVIEW and
                (public_marker is None or await self.dependencies.source.author_for(
                    record.target_project_id, record.version_id) != public_marker.author_id)):
            raise DomainError(ErrorCode.NOT_FOUND, "Public work is unavailable")
        return PreviewGrant(project_id=record.target_project_id, version_id=record.version_id,
                            artifact=record.artifact, environment=record.environment,
                            expires_at=record.expires_at,
                            public_author_id=public_marker.author_id if public_marker else None)

    async def serve_artifact(self, token: str) -> bytes:
        grant = await self.resolve_ticket(token)
        return await self.dependencies.blobs.read(grant.artifact)

    async def report_issue(self, ctx: RequestContext, issue: RuntimeIssue) -> OperationReceipt:
        if (not issue.message.strip() or len(issue.message) > 4000 or
                (issue.stack is not None and len(issue.stack) > 20_000)):
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid preview issue")
        grant = await self.resolve_ticket(issue.preview_token)
        if issue.source_origin != version_preview_origin(self.dependencies.preview_origin,
                issue.project_id, issue.version_id, grant.environment):
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid preview issue")
        if grant.project_id != issue.project_id or grant.version_id != issue.version_id:
            raise DomainError(ErrorCode.FORBIDDEN, "Preview issue scope differs")
        record = IssueRecord(id=self.dependencies.ids.new(), project_id=issue.project_id,
            version_id=issue.version_id, message=issue.message,
            stack=issue.stack, recorded_at=self.dependencies.clock.now())
        async with self.dependencies.uow() as uow:
            saved = await uow.repository(IssueRecord).save(
                Scope(workspace_id=None, project_id=issue.project_id), record, None)
            await uow.commit()
        return OperationReceipt(id=saved.id, revision=saved.revision,
                                recorded_at=record.recorded_at)

    async def create_share(self, ctx: RequestContext, grant: PreviewGrant,
                           mutation: Mutation) -> ShareLink:
        if grant.environment != Environment.DEVELOPMENT:
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid share grant")
        await self.dependencies.authorization.require(ctx, "project:write", grant.project_id)
        self._check_grant(grant)
        if ctx.actor_id is None or ctx.workspace_id is None or not mutation.idempotency_key:
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid share request")
        await self.dependencies.blobs.read(grant.artifact)
        share_id = _hash(f"share:{ctx.actor_id}:{grant.project_id}:{mutation.idempotency_key}")
        fingerprint = _hash(f"{grant.version_id}:{grant.artifact.id}:{grant.environment}")
        async with self.dependencies.uow() as uow:
            existing = await uow.repository(ShareRecord).get(GLOBAL, share_id)
        if existing:
            if existing.input_digest != fingerprint:
                raise DomainError(ErrorCode.CONFLICT, "Share idempotency key changed input")
            token = "" if existing.revoked else await self.dependencies.secrets.resolve(existing.token_ref)
            return ShareLink(id=existing.id, project_id=existing.target_project_id,
                version_id=existing.version_id,
                url=_preview_url(self.dependencies.preview_origin, existing.target_project_id,
                    existing.version_id, Environment.DEVELOPMENT, "share", token) if token else "",
                expires_at=existing.expires_at, revoked=existing.revoked)
        token = secrets.token_urlsafe(32)
        token_ref = await self.dependencies.secrets.put(
            Scope(workspace_id=ctx.workspace_id, project_id=grant.project_id), token)
        record = ShareRecord(id=share_id, target_workspace_id=ctx.workspace_id,
            target_project_id=grant.project_id, version_id=grant.version_id,
            artifact=grant.artifact, token_ref=token_ref, token_digest=_hash(token),
            input_digest=fingerprint, expires_at=None)
        try:
            replay = None
            async with self.dependencies.uow() as uow:
                existing = await uow.repository(ShareRecord).get(GLOBAL, share_id)
                if existing:
                    if existing.input_digest != fingerprint:
                        raise DomainError(ErrorCode.CONFLICT, "Share idempotency key changed input")
                    replay = existing
                else:
                    await uow.repository(ShareRecord).save(GLOBAL, record, None)
                    await uow.repository(ShareLookup).save(GLOBAL, ShareLookup(
                        id=record.token_digest, share_id=record.id), None)
                    await uow.commit()
        except Exception:
            await self.dependencies.secrets.revoke(token_ref)
            raise
        if replay is not None:
            await self.dependencies.secrets.revoke(token_ref)
            replay_token = "" if replay.revoked else await self.dependencies.secrets.resolve(replay.token_ref)
            return ShareLink(id=replay.id, project_id=replay.target_project_id,
                version_id=replay.version_id,
                url=_preview_url(self.dependencies.preview_origin, replay.target_project_id,
                    replay.version_id, Environment.DEVELOPMENT, "share", replay_token) if replay_token else "",
                expires_at=replay.expires_at, revoked=replay.revoked)
        return ShareLink(id=record.id, project_id=grant.project_id,
            version_id=grant.version_id,
            url=_preview_url(self.dependencies.preview_origin, grant.project_id,
                grant.version_id, grant.environment, "share", token),
            expires_at=None, revoked=False)

    async def revoke_share(self, ctx: RequestContext, share_id: EntityId,
                           mutation: Mutation) -> None:
        if not mutation.idempotency_key:
            raise DomainError(ErrorCode.INVALID_INPUT, "Idempotency key required")
        async with self.dependencies.uow() as uow:
            record = await uow.repository(ShareRecord).get(GLOBAL, share_id)
        if record is None:
            raise DomainError(ErrorCode.NOT_FOUND, "Share not found")
        await self.dependencies.authorization.require(ctx, "project:write", record.target_project_id)
        if ctx.workspace_id != record.target_workspace_id:
            raise DomainError(ErrorCode.FORBIDDEN, "Different workspace")
        if record.revoked:
            return
        async with self.dependencies.uow() as uow:
            repo = uow.repository(ShareRecord)
            current = await repo.get(GLOBAL, share_id)
            if current and not current.revoked:
                await repo.save(GLOBAL, replace(current, revoked=True), current.revision)
                await uow.commit()
        await self.dependencies.secrets.revoke(record.token_ref)

    async def resolve_share(self, share_token: str) -> PreviewTicket:
        if not share_token or len(share_token) > 128:
            raise DomainError(ErrorCode.NOT_FOUND, "Share not found")
        async with self.dependencies.uow() as uow:
            lookup = await uow.repository(ShareLookup).get(GLOBAL, _hash(share_token))
            record = await uow.repository(ShareRecord).get(GLOBAL, lookup.share_id) if lookup else None
        if record is None or record.revoked or (
                record.expires_at and record.expires_at <= self.dependencies.clock.now()):
            raise DomainError(ErrorCode.NOT_FOUND, "Share is unavailable")
        grant = PreviewGrant(project_id=record.target_project_id,
            version_id=record.version_id, artifact=record.artifact,
            environment=Environment.DEVELOPMENT,
            expires_at=self.dependencies.clock.now() + TICKET_LIFETIME)
        return await self._issue(record.target_workspace_id, grant, record.id)
