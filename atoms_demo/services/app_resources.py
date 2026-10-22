"""Project resources and application data isolated from platform identity."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from atoms_demo.contracts.app_resources import (AppDataContext, AppRecord, AppResource,
    AppSession, ResourceKind)
from atoms_demo.core.errors import DomainError, ErrorCode
from atoms_demo.core.types import (EntityId, Environment, Mutation, Page, PageQuery,
    PreviewGrant, RequestContext, Scope, SecretRef)
from atoms_demo.ports.dependencies import AppResourceDependencies

GLOBAL = Scope(workspace_id=None)
APP_SESSION_LIFETIME = timedelta(days=30)


@dataclass(frozen=True)
class ResourceLocator:
    id: EntityId
    target_workspace_id: EntityId
    target_project_id: EntityId
    revision: int = 0


@dataclass(frozen=True)
class AppUserRecord:
    id: EntityId
    project_id: EntityId
    created_at: datetime
    environment: Environment
    revision: int = 0


@dataclass(frozen=True)
class AppUserLocator:
    id: EntityId
    target_workspace_id: EntityId
    target_project_id: EntityId
    environment: Environment
    revision: int = 0


@dataclass(frozen=True)
class AppSessionRecord:
    id: EntityId
    target_workspace_id: EntityId
    target_project_id: EntityId
    app_user_id: EntityId
    environment: Environment
    expires_at: datetime
    revision: int = 0


@dataclass(frozen=True)
class PublicAppSessionMarker:
    id: EntityId
    version_id: EntityId
    author_id: EntityId
    revision: int = 0


@dataclass(frozen=True)
class DataRecord:
    id: EntityId
    project_id: EntityId
    app_user_id: EntityId
    environment: Environment
    collection: str
    json_document: str
    schema_version: int
    deleted: bool = False
    revision: int = 0


@dataclass(frozen=True)
class DataMutation:
    id: EntityId
    input_digest: EntityId
    result: AppRecord
    revision: int = 0


@dataclass(frozen=True)
class ResourceMutation:
    id: EntityId
    input_digest: EntityId
    result: AppResource
    revision: int = 0


@dataclass(frozen=True)
class ResourceSecretMutation:
    id: EntityId
    input_digest: EntityId
    result: AppResource
    revision: int = 0


def _hash(value: str) -> EntityId:
    return EntityId(hashlib.sha256(value.encode("utf-8")).hexdigest())


def decode_resource(data: dict) -> AppResource:
    return AppResource(**{**data, "environment": Environment(data["environment"]),
                          "kind": ResourceKind(data["kind"]),
                          "secret_ref": SecretRef(**data["secret_ref"])
                          if data["secret_ref"] else None})


def decode_app_user(data: dict) -> AppUserRecord:
    return AppUserRecord(**{**data, "created_at": datetime.fromisoformat(data["created_at"]),
                            "environment": Environment(data.get("environment", "development"))})


def decode_resource_mutation(data: dict) -> ResourceMutation:
    return ResourceMutation(**{**data, "result": decode_resource(data["result"])})


def decode_resource_secret_mutation(data: dict) -> ResourceSecretMutation:
    return ResourceSecretMutation(**{**data,
        "result": decode_resource(data["result"])})


def decode_data_mutation(data: dict) -> DataMutation:
    return DataMutation(**{**data, "result": AppRecord(**data["result"])})


def decode_app_session(data: dict) -> AppSessionRecord:
    return AppSessionRecord(**{**data, "environment": Environment(data["environment"]),
                               "expires_at": datetime.fromisoformat(data["expires_at"])})


def _collection(value: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", value):
        raise DomainError(ErrorCode.INVALID_INPUT, "Invalid collection name")
    return value


class AppResourceService:
    def __init__(self, dependencies: AppResourceDependencies) -> None:
        self.dependencies = dependencies

    async def _workspace(self, ctx: AppDataContext) -> EntityId:
        async with self.dependencies.uow() as uow:
            locator = await uow.repository(AppUserLocator).get(GLOBAL, ctx.app_user_id)
        if (locator is None or locator.target_project_id != ctx.project_id or
                locator.environment != ctx.environment):
            raise DomainError(ErrorCode.FORBIDDEN, "Application identity is outside project")
        return locator.target_workspace_id

    async def resources(self, ctx: RequestContext, project_id: EntityId,
                        environment: Environment) -> tuple[AppResource, ...]:
        await self.dependencies.authorization.require(ctx, "project:history", project_id)
        if ctx.workspace_id is None:
            raise DomainError(ErrorCode.FORBIDDEN, "Workspace selection required")
        scope = Scope(workspace_id=ctx.workspace_id, project_id=project_id)
        cursor = None
        result = []
        while True:
            async with self.dependencies.uow() as uow:
                page = await uow.repository(AppResource).list(scope,
                    PageQuery(cursor=cursor, limit=100))
            result.extend(item for item in page.items if item.environment == environment)
            if page.next_cursor is None:
                return tuple(result)
            cursor = page.next_cursor

    async def configure(self, ctx: RequestContext, resource: AppResource,
                        mutation: Mutation) -> AppResource:
        await self.dependencies.authorization.require(ctx, "project:write", resource.project_id)
        if (ctx.workspace_id is None or not mutation.idempotency_key or
                not re.fullmatch(r"[A-Za-z][A-Za-z0-9 _.-]{0,79}", resource.name) or
                resource.status not in {"configured", "disabled"}):
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid resource configuration")
        scope = Scope(workspace_id=ctx.workspace_id, project_id=resource.project_id)
        mutation_id = _hash(f"resource-configure:{ctx.actor_id}:{resource.project_id}:{mutation.idempotency_key}")
        digest = _hash(json.dumps({"id": resource.id, "project_id": resource.project_id,
            "environment": resource.environment, "kind": resource.kind,
            "name": resource.name, "status": resource.status,
            "secret_ref": resource.secret_ref.id if resource.secret_ref else None,
            "revision": resource.revision}, sort_keys=True))
        async with self.dependencies.uow() as uow:
            prior = await uow.repository(ResourceMutation).get(scope, mutation_id)
            if prior:
                if prior.input_digest != digest:
                    raise DomainError(ErrorCode.CONFLICT, "Resource idempotency key changed input")
                return prior.result
            repo = uow.repository(AppResource)
            existing = await repo.get(scope, resource.id)
            if existing:
                if resource.revision != existing.revision:
                    raise DomainError(ErrorCode.CONFLICT, "Resource revision changed")
                if existing.kind != resource.kind or existing.environment != resource.environment:
                    raise DomainError(ErrorCode.CONFLICT, "Resource identity cannot change")
                saved = await repo.save(scope, replace(resource,
                    secret_ref=existing.secret_ref), existing.revision)
            else:
                if resource.revision != 0 or resource.secret_ref is not None:
                    raise DomainError(ErrorCode.INVALID_INPUT, "New resource cannot contain a secret reference")
                saved = await repo.save(scope, resource, None)
                await uow.repository(ResourceLocator).save(GLOBAL, ResourceLocator(
                    id=resource.id, target_workspace_id=ctx.workspace_id,
                    target_project_id=resource.project_id), None)
            await uow.repository(ResourceMutation).save(scope, ResourceMutation(
                id=mutation_id, input_digest=digest, result=saved), None)
            await uow.commit()
        return saved

    async def set_secret(self, ctx: RequestContext, resource_id: EntityId,
                         secret_value: str, mutation: Mutation) -> AppResource:
        if not mutation.idempotency_key or not secret_value or len(secret_value) > 65_536:
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid resource secret")
        async with self.dependencies.uow() as uow:
            locator = await uow.repository(ResourceLocator).get(GLOBAL, resource_id)
        if locator is None:
            raise DomainError(ErrorCode.NOT_FOUND, "Resource not found")
        await self.dependencies.authorization.require(ctx, "project:write", locator.target_project_id)
        if ctx.workspace_id != locator.target_workspace_id:
            raise DomainError(ErrorCode.FORBIDDEN, "Different workspace")
        scope = Scope(workspace_id=locator.target_workspace_id,
                      project_id=locator.target_project_id)
        mutation_id = _hash(f"resource-secret:{ctx.actor_id}:{resource_id}:{mutation.idempotency_key}")
        digest = EntityId(self.dependencies.secrets.fingerprint(
            f"{resource_id}\0{secret_value}"))
        async with self.dependencies.uow() as uow:
            prior = await uow.repository(ResourceSecretMutation).get(scope, mutation_id)
        if prior:
            if not hmac.compare_digest(prior.input_digest, digest):
                raise DomainError(ErrorCode.CONFLICT, "Resource secret idempotency key changed input")
            return prior.result
        new_ref = await self.dependencies.secrets.put(scope, secret_value)
        try:
            replay = None
            async with self.dependencies.uow() as uow:
                prior = await uow.repository(ResourceSecretMutation).get(scope, mutation_id)
                if prior:
                    if not hmac.compare_digest(prior.input_digest, digest):
                        raise DomainError(ErrorCode.CONFLICT, "Resource secret idempotency key changed input")
                    replay = prior.result
                else:
                    repo = uow.repository(AppResource)
                    current = await repo.get(scope, resource_id)
                    if current is None:
                        raise DomainError(ErrorCode.NOT_FOUND, "Resource not found")
                    saved = await repo.save(scope, replace(current, secret_ref=new_ref),
                                            current.revision)
                    await uow.repository(ResourceSecretMutation).save(scope, ResourceSecretMutation(
                        id=mutation_id, input_digest=digest, result=saved), None)
                    await uow.commit()
        except Exception:
            await self.dependencies.secrets.revoke(new_ref)
            raise
        if replay is not None:
            await self.dependencies.secrets.revoke(new_ref)
            return replay
        if current.secret_ref:
            await self.dependencies.secrets.revoke(current.secret_ref)
        return saved

    async def open_app_session(self, grant: PreviewGrant,
                               existing_app_token: str | None) -> AppSession:
        if grant.expires_at <= self.dependencies.clock.now():
            raise DomainError(ErrorCode.FORBIDDEN, "Preview grant expired")
        if grant.environment == Environment.PUBLIC_PREVIEW and (
                grant.public_author_id is None or
                await self.dependencies.source.author_for(
                    grant.project_id, grant.version_id) != grant.public_author_id):
            raise DomainError(ErrorCode.FORBIDDEN, "Public work is unavailable")
        if existing_app_token:
            try:
                existing = await self._session(existing_app_token)
            except DomainError:
                existing = None
            if (existing and existing.target_project_id == grant.project_id and
                    existing.environment == grant.environment):
                public_match = True
                if grant.environment == Environment.PUBLIC_PREVIEW:
                    async with self.dependencies.uow() as uow:
                        marker = await uow.repository(PublicAppSessionMarker).get(GLOBAL, existing.id)
                    public_match = bool(marker and marker.version_id == grant.version_id and
                                        marker.author_id == grant.public_author_id)
                if public_match:
                    return AppSession(id=EntityId(existing_app_token),
                        project_id=grant.project_id, app_user_id=existing.app_user_id,
                        environment=existing.environment, expires_at=existing.expires_at)
        # Tickets are created by the preview owner, so find its project workspace
        # through the authorization adapter's project locator via a scoped record.
        workspace_id = await self._project_workspace(grant.project_id)
        user_id = self.dependencies.ids.new()
        token = secrets.token_urlsafe(32)
        expires_at = self.dependencies.clock.now() + APP_SESSION_LIFETIME
        scope = Scope(workspace_id=workspace_id, project_id=grant.project_id)
        async with self.dependencies.uow() as uow:
            await uow.repository(AppUserRecord).save(scope, AppUserRecord(
                id=user_id, project_id=grant.project_id,
                created_at=self.dependencies.clock.now(), environment=grant.environment), None)
            await uow.repository(AppUserLocator).save(GLOBAL, AppUserLocator(
                id=user_id, target_workspace_id=workspace_id,
                target_project_id=grant.project_id, environment=grant.environment), None)
            await uow.repository(AppSessionRecord).save(GLOBAL, AppSessionRecord(
                id=_hash(token), target_workspace_id=workspace_id,
                target_project_id=grant.project_id, app_user_id=user_id,
                environment=grant.environment, expires_at=expires_at), None)
            if grant.environment == Environment.PUBLIC_PREVIEW:
                await uow.repository(PublicAppSessionMarker).save(GLOBAL,
                    PublicAppSessionMarker(id=_hash(token), version_id=grant.version_id,
                        author_id=grant.public_author_id), None)
            await uow.commit()
        return AppSession(id=EntityId(token), project_id=grant.project_id,
            app_user_id=user_id, environment=grant.environment, expires_at=expires_at)

    async def _project_workspace(self, project_id: EntityId) -> EntityId:
        workspace_id = await self.dependencies.projects.workspace_for(project_id)
        if workspace_id is None:
            raise DomainError(ErrorCode.NOT_FOUND, "Project not found")
        return workspace_id

    async def _session(self, token: str) -> AppSessionRecord:
        if not token or len(token) > 128:
            raise DomainError(ErrorCode.UNAUTHENTICATED, "Application session invalid")
        async with self.dependencies.uow() as uow:
            record = await uow.repository(AppSessionRecord).get(GLOBAL, _hash(token))
        if record is None or record.expires_at <= self.dependencies.clock.now():
            raise DomainError(ErrorCode.UNAUTHENTICATED, "Application session expired")
        return record

    async def list_app_users(self, ctx: RequestContext, project_id: EntityId,
                             page: PageQuery) -> Page[EntityId]:
        await self.dependencies.authorization.require(ctx, "project:write", project_id)
        if ctx.workspace_id is None:
            raise DomainError(ErrorCode.FORBIDDEN, "Workspace selection required")
        async with self.dependencies.uow() as uow:
            found = await uow.repository(AppUserRecord).list(
                Scope(workspace_id=ctx.workspace_id, project_id=project_id), page)
        return Page(items=tuple(item.id for item in found.items), next_cursor=found.next_cursor)

    async def authenticate_app(self, app_token: str) -> AppDataContext:
        record = await self._session(app_token)
        if record.environment == Environment.PUBLIC_PREVIEW:
            async with self.dependencies.uow() as uow:
                marker = await uow.repository(PublicAppSessionMarker).get(GLOBAL, record.id)
            if (marker is None or await self.dependencies.source.author_for(
                    record.target_project_id, marker.version_id) != marker.author_id):
                raise DomainError(ErrorCode.UNAUTHENTICATED, "Public work is unavailable")
        return AppDataContext(project_id=record.target_project_id,
            app_user_id=record.app_user_id, environment=record.environment)

    async def records(self, ctx: AppDataContext, collection: str,
                      page: PageQuery) -> Page[AppRecord]:
        _collection(collection)
        if not 1 <= page.limit <= 100:
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid page limit")
        workspace_id = await self._workspace(ctx)
        scope = Scope(workspace_id=workspace_id, project_id=ctx.project_id)
        matched: list[AppRecord] = []
        cursor = page.cursor
        while True:
            async with self.dependencies.uow() as uow:
                found = await uow.repository(DataRecord).list(scope,
                    PageQuery(cursor=cursor, limit=100))
            for item in found.items:
                if (not item.deleted and item.app_user_id == ctx.app_user_id and
                        item.collection == collection and item.environment == ctx.environment):
                    if len(matched) == page.limit:
                        return Page(items=tuple(matched), next_cursor=str(matched[-1].id))
                    matched.append(AppRecord(id=item.id, collection=item.collection,
                        json_document=item.json_document, schema_version=item.schema_version,
                        revision=item.revision))
            if found.next_cursor is None:
                return Page(items=tuple(matched), next_cursor=None)
            cursor = found.next_cursor

    async def put_record(self, ctx: AppDataContext, record: AppRecord,
                         mutation: Mutation) -> AppRecord:
        _collection(record.collection)
        if (not mutation.idempotency_key or type(record.schema_version) is not int or
                not 1 <= record.schema_version <= 1000 or
                not isinstance(record.json_document, str) or
                len(record.json_document.encode("utf-8")) > 131_072):
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid application record")
        try:
            document = json.loads(record.json_document)
        except (ValueError, TypeError):
            raise DomainError(ErrorCode.INVALID_INPUT, "Application record must be valid JSON") from None
        if not isinstance(document, dict):
            raise DomainError(ErrorCode.INVALID_INPUT, "Application record must be an object")
        normalized = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        workspace_id = await self._workspace(ctx)
        scope = Scope(workspace_id=workspace_id, project_id=ctx.project_id)
        mutation_id = _hash(f"data:{ctx.app_user_id}:{ctx.project_id}:{ctx.environment}:{mutation.idempotency_key}")
        digest = _hash(f"{record.id}:{record.collection}:{record.schema_version}:{record.revision}:{normalized}")
        async with self.dependencies.uow() as uow:
            prior = await uow.repository(DataMutation).get(scope, mutation_id)
            repo = uow.repository(DataRecord)
            if prior:
                if prior.input_digest != digest:
                    raise DomainError(ErrorCode.CONFLICT, "Data idempotency key changed input")
                return prior.result
            existing = await repo.get(scope, record.id)
            if existing and (existing.app_user_id != ctx.app_user_id or
                    existing.environment != ctx.environment or
                    existing.collection != record.collection or existing.deleted):
                raise DomainError(ErrorCode.FORBIDDEN, "Application record scope differs")
            if existing and (record.revision != existing.revision or
                             record.schema_version < existing.schema_version):
                raise DomainError(ErrorCode.CONFLICT, "Application record revision changed")
            if not existing and record.revision != 0:
                raise DomainError(ErrorCode.CONFLICT, "New record revision must be zero")
            saved = await repo.save(scope, DataRecord(id=record.id, project_id=ctx.project_id,
                app_user_id=ctx.app_user_id, environment=ctx.environment,
                collection=record.collection,
                json_document=normalized, schema_version=record.schema_version),
                existing.revision if existing else None)
            result = AppRecord(id=saved.id, collection=saved.collection,
                json_document=saved.json_document, schema_version=saved.schema_version,
                revision=saved.revision)
            await uow.repository(DataMutation).save(scope, DataMutation(
                id=mutation_id, input_digest=digest, result=result), None)
            await uow.commit()
        return result

    async def delete_record(self, ctx: AppDataContext, record_id: EntityId,
                            mutation: Mutation) -> None:
        if not mutation.idempotency_key:
            raise DomainError(ErrorCode.INVALID_INPUT, "Idempotency key required")
        workspace_id = await self._workspace(ctx)
        scope = Scope(workspace_id=workspace_id, project_id=ctx.project_id)
        async with self.dependencies.uow() as uow:
            repo = uow.repository(DataRecord)
            record = await repo.get(scope, record_id)
            if (record is None or record.app_user_id != ctx.app_user_id or
                    record.environment != ctx.environment):
                raise DomainError(ErrorCode.NOT_FOUND, "Application record not found")
            if record.deleted:
                return
            await repo.save(scope, replace(record, deleted=True), record.revision)
            await uow.commit()
