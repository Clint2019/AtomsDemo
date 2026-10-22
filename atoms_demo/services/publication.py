"""Manual publication state with independent live-version verification."""
from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit
from dataclasses import dataclass, replace

from atoms_demo.contracts.publication import (ManualDeploymentResult, Publication,
    PublicationConfig, PublicationState)
from atoms_demo.core.errors import DomainError, ErrorCode
from atoms_demo.core.types import EntityId, Environment, Mutation, RequestContext, Scope
from atoms_demo.ports.dependencies import PublicationDependencies

GLOBAL = Scope(workspace_id=None)


@dataclass(frozen=True)
class PublicationLocator:
    id: EntityId
    target_workspace_id: EntityId
    target_project_id: EntityId
    revision: int = 0


@dataclass(frozen=True)
class PublicationIndex:
    id: EntityId
    publication_id: EntityId
    revision: int = 0


@dataclass(frozen=True)
class PublicationMutation:
    id: EntityId
    input_digest: EntityId
    result: Publication
    revision: int = 0


def decode_publication(data: dict) -> Publication:
    return Publication(**{**data, "environment": Environment(data["environment"]),
        "state": PublicationState(data["state"])})


def decode_publication_mutation(data: dict) -> PublicationMutation:
    return PublicationMutation(**{**data, "result": decode_publication(data["result"])})


def _hash(value: str) -> EntityId:
    return EntityId(hashlib.sha256(value.encode("utf-8")).hexdigest())


def _domain(value: str | None) -> str | None:
    if value is None:
        return None
    clean = value.strip().lower()
    if (len(clean) > 253 or not re.fullmatch(r"[a-z0-9.-]+", clean) or
            "." not in clean or any(not part or len(part) > 63 or
            part.startswith("-") or part.endswith("-") for part in clean.split("."))):
        raise DomainError(ErrorCode.INVALID_INPUT, "Invalid publication domain")
    return clean


def _public_endpoint(value: str) -> tuple[str, str | None, int | None, str, str, str]:
    """Compare deployment endpoints without treating a cosmetic root slash as a new site."""
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise DomainError(ErrorCode.INVALID_INPUT, "Invalid deployment URL") from None
    return (parsed.scheme.lower(), parsed.hostname, port or 443,
            parsed.path or "/", parsed.query, parsed.fragment)


class PublicationService:
    def __init__(self, dependencies: PublicationDependencies) -> None:
        self.dependencies = dependencies

    @staticmethod
    def _scope(ctx: RequestContext, project_id: EntityId) -> Scope:
        if ctx.workspace_id is None:
            raise DomainError(ErrorCode.FORBIDDEN, "Workspace selection required")
        return Scope(workspace_id=ctx.workspace_id, project_id=project_id)

    async def _authorized(self, ctx: RequestContext, publication_id: EntityId) -> Scope:
        async with self.dependencies.uow() as uow:
            locator = await uow.repository(PublicationLocator).get(GLOBAL, publication_id)
        if locator is None:
            raise DomainError(ErrorCode.NOT_FOUND, "Publication not found")
        await self.dependencies.authorization.require(ctx, "project:write", locator.target_project_id)
        if ctx.workspace_id != locator.target_workspace_id:
            raise DomainError(ErrorCode.FORBIDDEN, "Different workspace")
        return Scope(workspace_id=locator.target_workspace_id,
                     project_id=locator.target_project_id)

    async def _current(self, uow, scope: Scope, project_id: EntityId) -> Publication | None:
        index = await uow.repository(PublicationIndex).get(scope, project_id)
        return await uow.repository(Publication).get(scope, index.publication_id) if index else None

    async def configured_by_mutation(self, ctx: RequestContext,
                                     command: PublicationConfig) -> Publication | None:
        await self.dependencies.authorization.require(ctx, "project:write", command.project_id)
        if ctx.actor_id is None or not command.mutation.idempotency_key:
            raise DomainError(ErrorCode.INVALID_INPUT, "Idempotency key required")
        scope = self._scope(ctx, command.project_id)
        domain = _domain(command.domain)
        key = _hash(f"publication:configure:{ctx.actor_id}:{command.project_id}:{command.mutation.idempotency_key}")
        digest = _hash(f"{command.version_id}:{command.environment}:{domain}:{command.sync_db}:{command.migrate_data}")
        async with self.dependencies.uow() as uow:
            prior = await uow.repository(PublicationMutation).get(scope, key)
        if prior and prior.input_digest != digest:
            raise DomainError(ErrorCode.CONFLICT, "Publication key changed input")
        return prior.result if prior else None

    async def configure(self, ctx: RequestContext, command: PublicationConfig) -> Publication:
        await self.dependencies.authorization.require(ctx, "project:write", command.project_id)
        if (ctx.actor_id is None or command.environment != Environment.PRODUCTION or
                not command.mutation.idempotency_key or
                type(command.sync_db) is not bool or type(command.migrate_data) is not bool or
                (command.migrate_data and not command.sync_db)):
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid publication configuration")
        domain = _domain(command.domain)
        scope = self._scope(ctx, command.project_id)
        key = _hash(f"publication:configure:{ctx.actor_id}:{command.project_id}:{command.mutation.idempotency_key}")
        digest = _hash(f"{command.version_id}:{command.environment}:{domain}:{command.sync_db}:{command.migrate_data}")
        async with self.dependencies.uow() as uow:
            prior = await uow.repository(PublicationMutation).get(scope, key)
            if prior:
                if prior.input_digest != digest:
                    raise DomainError(ErrorCode.CONFLICT, "Publication key changed input")
                return prior.result
            current = await self._current(uow, scope, command.project_id)
            if current and current.state == PublicationState.AWAITING_MANUAL:
                raise DomainError(ErrorCode.CONFLICT, "Manual action is already pending")
            if current:
                candidate = replace(current, version_id=command.version_id,
                    environment=command.environment, domain=domain,
                    sync_db=command.sync_db, migrate_data=command.migrate_data,
                    state=PublicationState.DRAFT, pending_action=None)
                saved = await uow.repository(Publication).save(scope, candidate, current.revision)
            else:
                candidate = Publication(id=self.dependencies.ids.new(), project_id=command.project_id,
                    version_id=command.version_id, environment=command.environment,
                    domain=domain, sync_db=command.sync_db, migrate_data=command.migrate_data,
                    state=PublicationState.DRAFT, execution_mode="manual", revision=0)
                saved = await uow.repository(Publication).save(scope, candidate, None)
                await uow.repository(PublicationIndex).save(scope, PublicationIndex(
                    id=command.project_id, publication_id=saved.id), None)
                await uow.repository(PublicationLocator).save(GLOBAL, PublicationLocator(
                    id=saved.id, target_workspace_id=ctx.workspace_id,
                    target_project_id=command.project_id), None)
            await uow.repository(PublicationMutation).save(scope, PublicationMutation(
                id=key, input_digest=digest, result=saved), None)
            await uow.commit()
        return saved

    async def get(self, ctx: RequestContext, project_id: EntityId) -> Publication | None:
        await self.dependencies.authorization.require(ctx, "project:history", project_id)
        scope = self._scope(ctx, project_id)
        async with self.dependencies.uow() as uow:
            return await self._current(uow, scope, project_id)

    async def request_manual(self, ctx: RequestContext, publication_id: EntityId,
                             mutation: Mutation) -> Publication:
        scope = await self._authorized(ctx, publication_id)
        if not mutation.idempotency_key:
            raise DomainError(ErrorCode.INVALID_INPUT, "Idempotency key required")
        key = _hash(f"publication:manual:{ctx.actor_id}:{publication_id}:{mutation.idempotency_key}")
        async with self.dependencies.uow() as uow:
            prior = await uow.repository(PublicationMutation).get(scope, key)
            if prior:
                return prior.result
            repo = uow.repository(Publication)
            current = await repo.get(scope, publication_id)
            if current is None or current.state not in {PublicationState.DRAFT, PublicationState.FAILED}:
                raise DomainError(ErrorCode.CONFLICT, "Publication is not ready for manual deployment")
            action = "update" if current.live_version_id else "deploy"
            saved = await repo.save(scope, replace(current,
                state=PublicationState.AWAITING_MANUAL, pending_action=action), current.revision)
            await uow.repository(PublicationMutation).save(scope, PublicationMutation(
                id=key, input_digest=_hash(action), result=saved), None)
            await uow.commit()
        return saved

    async def verify_manual_result(self, ctx: RequestContext,
                                   command: ManualDeploymentResult) -> Publication:
        scope = await self._authorized(ctx, command.publication_id)
        if ctx.actor_id != command.operator_id or not command.mutation.idempotency_key:
            raise DomainError(ErrorCode.FORBIDDEN, "Deployment operator mismatch")
        key = _hash(f"publication:verify:{ctx.actor_id}:{command.publication_id}:{command.mutation.idempotency_key}")
        digest = _hash(f"{command.public_url}:{command.evidence.id}:{command.evidence.sha256}")
        async with self.dependencies.uow() as uow:
            prior = await uow.repository(PublicationMutation).get(scope, key)
            if prior:
                if prior.input_digest != digest:
                    raise DomainError(ErrorCode.CONFLICT, "Deployment result key changed input")
                return prior.result
            current = await uow.repository(Publication).get(scope, command.publication_id)
        if current is None or current.state != PublicationState.AWAITING_MANUAL:
            raise DomainError(ErrorCode.CONFLICT, "No manual deployment is pending")
        if current.domain and urlsplit(command.public_url).hostname != current.domain:
            raise DomainError(ErrorCode.INVALID_INPUT, "Deployment URL differs from configured domain")
        if (current.pending_action in {"paused", "unpublished", "resume"} and
                (current.public_url is None or
                 _public_endpoint(command.public_url) != _public_endpoint(current.public_url))):
            raise DomainError(ErrorCode.INVALID_INPUT,
                "State change must verify the existing public URL")
        evidence = await self.dependencies.blobs.read(command.evidence)
        if not evidence:
            raise DomainError(ErrorCode.INVALID_INPUT, "Deployment evidence is empty")
        if current.pending_action in {"paused", "unpublished"}:
            if current.live_version_id is None:
                raise DomainError(ErrorCode.CONFLICT, "No live version can be changed")
            verification = await self.dependencies.verifier.verify_state(command,
                current.live_version_id, current.pending_action)
        else:
            verification = await self.dependencies.verifier.verify(command, current.version_id)
        if not verification.reachable or not verification.version_matches:
            raise DomainError(ErrorCode.CONFLICT, "Deployed version could not be verified")
        async with self.dependencies.uow() as uow:
            prior = await uow.repository(PublicationMutation).get(scope, key)
            if prior:
                if prior.input_digest != digest:
                    raise DomainError(ErrorCode.CONFLICT, "Deployment result key changed input")
                return prior.result
            repo = uow.repository(Publication)
            latest = await repo.get(scope, command.publication_id)
            if latest is None or latest.revision != current.revision or latest.state != PublicationState.AWAITING_MANUAL:
                raise DomainError(ErrorCode.CONFLICT, "Publication changed during verification")
            if latest.pending_action in {"paused", "unpublished"}:
                state = PublicationState(latest.pending_action)
                saved = await repo.save(scope, replace(latest, state=state,
                    live_version_id=None if state == PublicationState.UNPUBLISHED
                    else latest.live_version_id, pending_action=None), latest.revision)
            else:
                saved = await repo.save(scope, replace(latest, state=PublicationState.LIVE,
                    live_version_id=latest.version_id, public_url=command.public_url,
                    pending_action=None), latest.revision)
            await uow.repository(PublicationMutation).save(scope, PublicationMutation(
                id=key, input_digest=digest, result=saved), None)
            await uow.commit()
        return saved

    async def change_state(self, ctx: RequestContext, publication_id: EntityId,
                           target: PublicationState, mutation: Mutation) -> Publication:
        scope = await self._authorized(ctx, publication_id)
        if not mutation.idempotency_key or target not in {
                PublicationState.PAUSED, PublicationState.UNPUBLISHED, PublicationState.LIVE}:
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid publication transition")
        key = _hash(f"publication:state:{ctx.actor_id}:{publication_id}:{mutation.idempotency_key}")
        async with self.dependencies.uow() as uow:
            prior = await uow.repository(PublicationMutation).get(scope, key)
            if prior:
                if prior.input_digest != _hash(target.value):
                    raise DomainError(ErrorCode.CONFLICT, "Publication transition key changed input")
                return prior.result
            repo = uow.repository(Publication)
            current = await repo.get(scope, publication_id)
            if current is None or current.state not in {PublicationState.LIVE, PublicationState.PAUSED}:
                raise DomainError(ErrorCode.CONFLICT, "Publication cannot transition now")
            if target == current.state:
                await uow.repository(PublicationMutation).save(scope, PublicationMutation(
                    id=key, input_digest=_hash(target.value), result=current), None)
                await uow.commit()
                return current
            if target == PublicationState.LIVE and current.state != PublicationState.PAUSED:
                raise DomainError(ErrorCode.CONFLICT, "Only paused publication can resume")
            saved = await repo.save(scope, replace(current,
                state=PublicationState.AWAITING_MANUAL,
                pending_action="resume" if target == PublicationState.LIVE else target.value),
                current.revision)
            await uow.repository(PublicationMutation).save(scope, PublicationMutation(
                id=key, input_digest=_hash(target.value), result=saved), None)
            await uow.commit()
        return saved
