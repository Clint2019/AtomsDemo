"""Public project discovery with persisted saves, views and source validation."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from datetime import datetime

from atoms_demo.contracts.catalog import CatalogEntry, CatalogQuery, ContentKind
from atoms_demo.core.errors import DomainError, ErrorCode
from atoms_demo.core.types import EntityId, Mutation, Page, PageQuery, RequestContext, Scope
from atoms_demo.ports.dependencies import CatalogDependencies

GLOBAL = Scope(workspace_id=None)


@dataclass(frozen=True)
class CatalogRecord:
    id: EntityId
    kind: ContentKind
    target_project_id: EntityId
    version_id: EntityId
    author_id: EntityId
    title: str
    category: str
    description: str
    views: int
    saves: int
    created_at: datetime
    revision: int = 0


@dataclass(frozen=True)
class CatalogSaveRecord:
    id: EntityId
    entry_id: EntityId
    user_id: EntityId
    saved: bool
    revision: int = 0


@dataclass(frozen=True)
class CatalogMutation:
    id: EntityId
    entry_id: EntityId
    input_digest: EntityId
    revision: int = 0


def decode_catalog_record(data: dict) -> CatalogRecord:
    return CatalogRecord(**{**data, "kind": ContentKind(data["kind"]),
                            "created_at": datetime.fromisoformat(data["created_at"])})


def _hash(value: str) -> EntityId:
    return EntityId(hashlib.sha256(value.encode("utf-8")).hexdigest())


def _entry(record: CatalogRecord, saved: bool = False) -> CatalogEntry:
    return CatalogEntry(id=record.id, kind=record.kind,
        project_id=record.target_project_id, version_id=record.version_id,
        author_id=record.author_id, title=record.title, category=record.category,
        description=record.description, views=record.views, saves=record.saves,
        revision=record.revision, saved=saved)


class CatalogService:
    def __init__(self, dependencies: CatalogDependencies) -> None:
        self.dependencies = dependencies

    async def _all(self) -> list[CatalogRecord]:
        cursor = None
        result = []
        while True:
            async with self.dependencies.uow() as uow:
                page = await uow.repository(CatalogRecord).list(GLOBAL,
                    PageQuery(cursor=cursor, limit=100))
            result.extend(page.items)
            if page.next_cursor is None:
                return result
            cursor = page.next_cursor

    async def _public(self, record: CatalogRecord) -> bool:
        author = await self.dependencies.source.author_for(
            record.target_project_id, record.version_id)
        return author == record.author_id

    async def _saved(self, actor_id: EntityId | None,
                     entry_id: EntityId) -> bool:
        if actor_id is None:
            return False
        async with self.dependencies.uow() as uow:
            record = await uow.repository(CatalogSaveRecord).get(GLOBAL,
                _hash(f"catalog-save:{actor_id}:{entry_id}"))
        return bool(record and record.saved)

    async def search(self, ctx: RequestContext, query: CatalogQuery) -> Page[CatalogEntry]:
        if query.sort not in {"latest", "popular", "saved"} or not 1 <= query.page.limit <= 100:
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid catalog query")
        if query.saved_only and ctx.actor_id is None:
            raise DomainError(ErrorCode.UNAUTHENTICATED, "Sign in required")
        try:
            offset = int(query.page.cursor) if query.page.cursor is not None else 0
        except ValueError:
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid catalog cursor") from None
        if offset < 0:
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid catalog cursor")
        records = []
        for record in await self._all():
            if ((query.kind and record.kind != query.kind) or
                    (query.category and record.category != query.category) or
                    (query.author_id and record.author_id != query.author_id) or
                    not await self._public(record)):
                continue
            if query.saved_only:
                if not await self._saved(ctx.actor_id, record.id):
                    continue
            records.append(record)
        if query.sort == "latest":
            records.sort(key=lambda item: (item.created_at, str(item.id)), reverse=True)
        elif query.sort == "popular":
            records.sort(key=lambda item: (item.views, item.created_at), reverse=True)
        else:
            records.sort(key=lambda item: (item.saves, item.created_at), reverse=True)
        selected = records[offset:offset + query.page.limit]
        next_cursor = str(offset + len(selected)) if offset + len(selected) < len(records) else None
        entries = [_entry(item, await self._saved(ctx.actor_id, item.id))
                   for item in selected]
        return Page(items=tuple(entries), next_cursor=next_cursor)

    async def detail(self, ctx: RequestContext, entry_id: EntityId) -> CatalogEntry:
        async with self.dependencies.uow() as uow:
            record = await uow.repository(CatalogRecord).get(GLOBAL, entry_id)
        if record is None or not await self._public(record):
            raise DomainError(ErrorCode.NOT_FOUND, "Catalog entry not found")
        return _entry(record, await self._saved(ctx.actor_id, entry_id))

    async def categories(self, ctx: RequestContext,
                         kind: ContentKind) -> tuple[str, ...]:
        return tuple(sorted({record.category for record in await self._all()
            if record.kind == kind and await self._public(record)}))

    async def save(self, ctx: RequestContext, entry_id: EntityId,
                   saved: bool, mutation: Mutation) -> None:
        if ctx.actor_id is None:
            raise DomainError(ErrorCode.UNAUTHENTICATED, "Sign in required")
        if type(saved) is not bool or not mutation.idempotency_key:
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid catalog save")
        await self.detail(ctx, entry_id)
        mutation_id = _hash(f"catalog:save:{ctx.actor_id}:{entry_id}:{mutation.idempotency_key}")
        digest = _hash(str(saved))
        save_id = _hash(f"catalog-save:{ctx.actor_id}:{entry_id}")
        async with self.dependencies.uow() as uow:
            prior = await uow.repository(CatalogMutation).get(GLOBAL, mutation_id)
            if prior:
                if prior.input_digest != digest:
                    raise DomainError(ErrorCode.CONFLICT, "Catalog save key changed input")
                return
            save_repo = uow.repository(CatalogSaveRecord)
            existing = await save_repo.get(GLOBAL, save_id)
            old_value = existing.saved if existing else False
            if old_value != saved:
                await save_repo.save(GLOBAL, CatalogSaveRecord(id=save_id,
                    entry_id=entry_id, user_id=ctx.actor_id, saved=saved),
                    existing.revision if existing else None)
                record_repo = uow.repository(CatalogRecord)
                record = await record_repo.get(GLOBAL, entry_id)
                await record_repo.save(GLOBAL, replace(record,
                    saves=record.saves + (1 if saved else -1)), record.revision)
            await uow.repository(CatalogMutation).save(GLOBAL, CatalogMutation(
                id=mutation_id, entry_id=entry_id, input_digest=digest), None)
            await uow.commit()

    async def record_view(self, ctx: RequestContext, entry_id: EntityId,
                          mutation: Mutation) -> None:
        if not mutation.idempotency_key:
            raise DomainError(ErrorCode.INVALID_INPUT, "Idempotency key required")
        await self.detail(ctx, entry_id)
        mutation_id = _hash(f"catalog:view:{ctx.actor_id}:{entry_id}:{mutation.idempotency_key}")
        async with self.dependencies.uow() as uow:
            if await uow.repository(CatalogMutation).get(GLOBAL, mutation_id):
                return
            repo = uow.repository(CatalogRecord)
            record = await repo.get(GLOBAL, entry_id)
            await repo.save(GLOBAL, replace(record, views=record.views + 1), record.revision)
            await uow.repository(CatalogMutation).save(GLOBAL, CatalogMutation(
                id=mutation_id, entry_id=entry_id, input_digest=_hash("view")), None)
            await uow.commit()

    async def publish_entry(self, ctx: RequestContext, entry: CatalogEntry,
                            mutation: Mutation) -> CatalogEntry:
        await self.dependencies.authorization.require(ctx, "project:write", entry.project_id)
        if (ctx.actor_id is None or not mutation.idempotency_key or
                entry.kind not in {ContentKind.PUBLIC_PROJECT, ContentKind.TEMPLATE} or
                entry.views != 0 or entry.saves != 0 or entry.revision != 0 or
                not entry.title.strip() or len(entry.title) > 120 or
                not re.fullmatch(r"[A-Za-z0-9 _-]{1,60}", entry.category) or
                len(entry.description) > 2000):
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid public catalog entry")
        author = await self.dependencies.source.author_for(entry.project_id, entry.version_id)
        if author is None or entry.author_id != author:
            raise DomainError(ErrorCode.FORBIDDEN, "Catalog source is not public and built")
        mutation_id = _hash(f"catalog:publish:{ctx.actor_id}:{entry.project_id}:{mutation.idempotency_key}")
        digest = _hash(f"{entry.id}:{entry.project_id}:{entry.version_id}:{entry.title}:"
                       f"{entry.category}:{entry.description}")
        async with self.dependencies.uow() as uow:
            prior = await uow.repository(CatalogMutation).get(GLOBAL, mutation_id)
            if prior:
                if prior.input_digest != digest:
                    raise DomainError(ErrorCode.CONFLICT, "Catalog publication key changed input")
                record = await uow.repository(CatalogRecord).get(GLOBAL, prior.entry_id)
                if record is None:
                    raise DomainError(ErrorCode.CONFLICT, "Catalog publication is inconsistent")
                return _entry(record)
            record = CatalogRecord(id=entry.id, kind=entry.kind,
                target_project_id=entry.project_id, version_id=entry.version_id,
                author_id=entry.author_id, title=entry.title.strip(),
                category=entry.category, description=entry.description,
                views=0, saves=0, created_at=self.dependencies.clock.now())
            saved = await uow.repository(CatalogRecord).save(GLOBAL, record, None)
            await uow.repository(CatalogMutation).save(GLOBAL, CatalogMutation(
                id=mutation_id, entry_id=entry.id, input_digest=digest), None)
            await uow.commit()
        return _entry(saved)
