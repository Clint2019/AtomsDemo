"""Workspace upload metadata; shared content-addressed blobs are retained on deletion."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime

from atoms_demo.contracts.storage import StoredObject, Upload, StorageUsage
from atoms_demo.core.errors import DomainError, ErrorCode
from atoms_demo.core.types import BlobRef, EntityId, Mutation, Page, PageQuery, RequestContext, Scope
from atoms_demo.core.metering import StorageMetering
from atoms_demo.ports.dependencies import StorageDependencies

GLOBAL = Scope(workspace_id=None)


@dataclass(frozen=True)
class StorageObjectRecord:
    id: EntityId
    workspace_id: EntityId
    project_id: EntityId | None
    blob: BlobRef
    purpose: str
    display_name: str
    created_at: datetime
    deleted: bool = False
    revision: int = 0


@dataclass(frozen=True)
class StorageLocator:
    id: EntityId
    target_workspace_id: EntityId
    target_project_id: EntityId | None
    revision: int = 0


@dataclass(frozen=True)
class StorageMutation:
    id: EntityId
    object_id: EntityId
    input_digest: EntityId
    revision: int = 0


@dataclass(frozen=True)
class StoragePolicy:
    id: EntityId
    pay_as_you_go: bool
    revision: int = 0


@dataclass(frozen=True)
class StorageMeteringRecord:
    id: EntityId
    workspace_id: EntityId
    target_project_id: EntityId | None
    byte_delta: int
    occurred_at: datetime
    billable: bool = False
    processed: bool = False
    revision: int = 0


def decode_storage_metering(data: dict) -> StorageMeteringRecord:
    return StorageMeteringRecord(**{**{"billable": False, **data},
                                    "occurred_at": datetime.fromisoformat(data["occurred_at"])})


def decode_storage_object(data: dict) -> StorageObjectRecord:
    return StorageObjectRecord(**{**data, "blob": BlobRef(**data["blob"]),
                                  "created_at": datetime.fromisoformat(data["created_at"])})


def _hash(value: str) -> EntityId:
    return EntityId(hashlib.sha256(value.encode()).hexdigest())


class StorageService:
    def __init__(self, dependencies: StorageDependencies) -> None:
        self.dependencies = dependencies

    async def _authorize(self, ctx: RequestContext, scope: Scope, write: bool) -> None:
        if ctx.actor_id is None:
            raise DomainError(ErrorCode.UNAUTHENTICATED, "Sign in required")
        if ctx.workspace_id is None or scope.workspace_id != ctx.workspace_id:
            raise DomainError(ErrorCode.FORBIDDEN, "Different workspace")
        if scope.project_id:
            await self.dependencies.authorization.require(ctx, "project:write" if write else "project:history",
                                                          scope.project_id)
        else:
            await self.dependencies.authorization.require(ctx, "workspace:write" if write else "workspace:read",
                                                          ctx.workspace_id)

    @staticmethod
    def _dto(record: StorageObjectRecord) -> StoredObject:
        return StoredObject(id=record.id, scope=Scope(workspace_id=record.workspace_id,
                             project_id=record.project_id), blob=record.blob, purpose=record.purpose,
                             display_name=record.display_name, created_at=record.created_at)

    async def _located(self, uow, object_id: EntityId) -> tuple[StorageObjectRecord, Scope] | None:
        locator = await uow.repository(StorageLocator).get(GLOBAL, object_id)
        if locator is None:
            return None
        scope = Scope(workspace_id=locator.target_workspace_id, project_id=locator.target_project_id)
        record = await uow.repository(StorageObjectRecord).get(scope, object_id)
        return (record, scope) if record else None

    async def _used(self, uow, workspace: EntityId, project: EntityId | None) -> int:
        cursor = None
        total = 0
        while True:
            page = await uow.repository(StorageLocator).list(GLOBAL, PageQuery(cursor=cursor, limit=100))
            for locator in page.items:
                if locator.target_workspace_id == workspace and (project is None or locator.target_project_id == project):
                    scope = Scope(workspace_id=workspace, project_id=locator.target_project_id)
                    item = await uow.repository(StorageObjectRecord).get(scope, locator.id)
                    if item and not item.deleted:
                        total += item.blob.size_bytes
            if page.next_cursor is None:
                return total
            cursor = page.next_cursor

    async def upload(self, ctx: RequestContext, command: Upload) -> StoredObject:
        await self._authorize(ctx, command.scope, True)
        if (not command.purpose or len(command.purpose) > 80 or not command.display_name or
                len(command.display_name) > 255 or not isinstance(command.content, bytes) or
                not command.mutation.idempotency_key):
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid upload")
        digest = _hash(f"{command.scope}:{command.purpose}:{command.display_name}:"
                       f"{command.media_type}:{hashlib.sha256(command.content).hexdigest()}")
        mutation_id = _hash(f"storage:upload:{ctx.actor_id}:{command.mutation.idempotency_key}")
        async with self.dependencies.uow() as uow:
            prior = await uow.repository(StorageMutation).get(GLOBAL, mutation_id)
            if prior:
                if prior.input_digest != digest:
                    raise DomainError(ErrorCode.CONFLICT, "Idempotency key was used with different input")
                located = await self._located(uow, prior.object_id)
                if located is None or located[0].deleted:
                    raise DomainError(ErrorCode.CONFLICT, "Upload was previously deleted")
                return self._dto(located[0])
            if await self._used(uow, ctx.workspace_id, None) + len(command.content) > self.dependencies.quota_bytes:
                raise DomainError(ErrorCode.QUOTA_EXCEEDED, "Storage quota exceeded")
            blob = await self.dependencies.blobs.put(command.content, command.media_type)
            record = StorageObjectRecord(id=self.dependencies.ids.new(), workspace_id=ctx.workspace_id,
                                         project_id=command.scope.project_id, blob=blob,
                                         purpose=command.purpose, display_name=command.display_name,
                                         created_at=self.dependencies.clock.now())
            await uow.repository(StorageObjectRecord).save(command.scope, record, None)
            await uow.repository(StorageLocator).save(GLOBAL, StorageLocator(
                id=record.id, target_workspace_id=ctx.workspace_id,
                target_project_id=command.scope.project_id), None)
            await uow.repository(StorageMutation).save(GLOBAL, StorageMutation(
                id=mutation_id, object_id=record.id, input_digest=digest), None)
            if blob.size_bytes:
                policy = await uow.repository(StoragePolicy).get(GLOBAL, ctx.workspace_id)
                await uow.repository(StorageMeteringRecord).save(Scope(workspace_id=ctx.workspace_id),
                    StorageMeteringRecord(id=_hash(f"meter:upload:{record.id}"), workspace_id=ctx.workspace_id,
                        target_project_id=record.project_id, byte_delta=blob.size_bytes,
                        occurred_at=record.created_at, billable=policy.pay_as_you_go if policy else False), None)
            await uow.commit()
        return self._dto(record)

    async def get(self, ctx: RequestContext, object_id: EntityId) -> StoredObject:
        async with self.dependencies.uow() as uow:
            located = await self._located(uow, object_id)
        if located is None or located[0].deleted:
            raise DomainError(ErrorCode.NOT_FOUND, "Storage object not found")
        await self._authorize(ctx, located[1], False)
        return self._dto(located[0])

    async def download(self, ctx: RequestContext, object_id: EntityId) -> bytes:
        return await self.dependencies.blobs.read((await self.get(ctx, object_id)).blob)

    async def list_objects(self, ctx: RequestContext, scope: Scope, page: PageQuery) -> Page[StoredObject]:
        await self._authorize(ctx, scope, False)
        if not 1 <= page.limit <= 100:
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid page limit")
        async with self.dependencies.uow() as uow:
            cursor = page.cursor
            items = []
            while len(items) <= page.limit:
                listing = await uow.repository(StorageObjectRecord).list(scope, PageQuery(cursor=cursor, limit=100))
                for record in listing.items:
                    cursor = str(record.id)
                    if not record.deleted:
                        items.append(self._dto(record))
                        if len(items) > page.limit:
                            break
                if len(items) > page.limit or listing.next_cursor is None:
                    break
        return Page(items=tuple(items[:page.limit]),
                    next_cursor=str(items[page.limit - 1].id) if len(items) > page.limit else None)

    async def delete(self, ctx: RequestContext, object_id: EntityId, mutation: Mutation) -> None:
        async with self.dependencies.uow() as uow:
            located = await self._located(uow, object_id)
            if located is None:
                raise DomainError(ErrorCode.NOT_FOUND, "Storage object not found")
            record, scope = located
            await self._authorize(ctx, scope, True)
            if record.deleted:
                return
            if mutation.expected_revision is not None and mutation.expected_revision != record.revision:
                raise DomainError(ErrorCode.CONFLICT, "Storage object revision changed")
            await uow.repository(StorageObjectRecord).save(scope, replace(record, deleted=True), record.revision)
            if record.blob.size_bytes:
                await uow.repository(StorageMeteringRecord).save(Scope(workspace_id=ctx.workspace_id),
                    StorageMeteringRecord(id=_hash(f"meter:delete:{record.id}"), workspace_id=ctx.workspace_id,
                        target_project_id=record.project_id, byte_delta=-record.blob.size_bytes,
                        occurred_at=self.dependencies.clock.now(), billable=False), None)
            await uow.commit()

    async def pending_metering(self, ctx: RequestContext) -> tuple[StorageMetering, ...]:
        workspace = ctx.workspace_id
        await self._authorize(ctx, Scope(workspace_id=workspace), False)
        async with self.dependencies.uow() as uow:
            cursor = None
            result = []
            while True:
                page = await uow.repository(StorageMeteringRecord).list(
                    Scope(workspace_id=workspace), PageQuery(cursor=cursor, limit=100))
                result.extend(StorageMetering(event_id=item.id, workspace_id=workspace,
                    project_id=item.target_project_id, byte_delta=item.byte_delta,
                    occurred_at=item.occurred_at, billable=item.billable)
                    for item in page.items if not item.processed)
                if page.next_cursor is None:
                    return tuple(sorted(result, key=lambda item: (item.occurred_at, str(item.event_id))))
                cursor = page.next_cursor

    async def mark_metered(self, ctx: RequestContext, event_id: EntityId) -> None:
        workspace = ctx.workspace_id
        await self._authorize(ctx, Scope(workspace_id=workspace), True)
        async with self.dependencies.uow() as uow:
            repo = uow.repository(StorageMeteringRecord)
            event = await repo.get(Scope(workspace_id=workspace), event_id)
            if event is None:
                raise DomainError(ErrorCode.NOT_FOUND, "Storage metering event not found")
            if event.processed:
                return
            await repo.save(Scope(workspace_id=workspace), replace(event, processed=True), event.revision)
            await uow.commit()

    async def usage(self, ctx: RequestContext, project_id: EntityId | None = None) -> StorageUsage:
        workspace = ctx.workspace_id
        await self._authorize(ctx, Scope(workspace_id=workspace, project_id=project_id), False)
        async with self.dependencies.uow() as uow:
            used = await self._used(uow, workspace, project_id)
            policy = await uow.repository(StoragePolicy).get(GLOBAL, workspace)
        return StorageUsage(used_bytes=used, quota_bytes=self.dependencies.quota_bytes,
                            pay_as_you_go=policy.pay_as_you_go if policy else False)

    async def set_pay_as_you_go(self, ctx: RequestContext, enabled: bool, mutation: Mutation) -> StorageUsage:
        workspace = ctx.workspace_id
        await self.dependencies.authorization.require(ctx, "workspace:manage", workspace)
        if type(enabled) is not bool:
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid pay-as-you-go setting")
        async with self.dependencies.uow() as uow:
            repo = uow.repository(StoragePolicy)
            prior = await repo.get(GLOBAL, workspace)
            if mutation.expected_revision is not None and mutation.expected_revision != (prior.revision if prior else 0):
                raise DomainError(ErrorCode.CONFLICT, "Storage policy revision changed")
            await repo.save(GLOBAL, StoragePolicy(id=workspace, pay_as_you_go=enabled),
                            prior.revision if prior else None)
            await uow.commit()
        return await self.usage(ctx)
