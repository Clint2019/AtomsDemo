"""Project identity, visibility and per-user favorite ownership."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace

from atoms_demo.contracts.projects import Visibility, Project, CreateProject, ProjectQuery, ProjectAPI
from atoms_demo.core.errors import DomainError, ErrorCode
from atoms_demo.core.types import EntityId, Mutation, Page, PageQuery, RequestContext, Scope
from atoms_demo.ports.dependencies import ProjectDependencies

_GLOBAL = Scope(workspace_id=None)


@dataclass(frozen=True)
class ProjectLocator:
    id: EntityId
    target_workspace_id: EntityId
    revision: int = 0


@dataclass(frozen=True)
class ProjectMutation:
    id: EntityId
    result_id: EntityId
    input_digest: EntityId
    revision: int = 0


@dataclass(frozen=True)
class ProjectFavoriteIndex:
    id: EntityId
    project_ids: tuple[EntityId, ...]
    revision: int = 0


def _hash(value: str) -> EntityId:
    return EntityId(hashlib.sha256(value.encode("utf-8")).hexdigest())


class ProjectService:
    def __init__(self, dependencies: ProjectDependencies) -> None:
        self.dependencies = dependencies

    @staticmethod
    def _scope(ctx: RequestContext) -> Scope:
        if ctx.actor_id is None:
            raise DomainError(ErrorCode.UNAUTHENTICATED, "Sign in required")
        if ctx.workspace_id is None:
            raise DomainError(ErrorCode.FORBIDDEN, "Workspace selection required")
        return Scope(workspace_id=ctx.workspace_id)

    async def _located(self, uow, project_id: EntityId) -> Project | None:
        locator = await uow.repository(ProjectLocator).get(_GLOBAL, project_id)
        if locator is None:
            return None
        return await uow.repository(Project).get(
            Scope(workspace_id=locator.target_workspace_id), project_id)

    async def create(self, ctx: RequestContext, command: CreateProject) -> Project:
        scope = self._scope(ctx)
        await self.dependencies.authorization.require(ctx, "workspace:write", ctx.workspace_id)
        name = command.name.strip()
        if not 1 <= len(name) <= 120:
            raise DomainError(ErrorCode.INVALID_INPUT, "Project name must be 1–120 characters")
        if not command.mutation.idempotency_key:
            raise DomainError(ErrorCode.INVALID_INPUT, "Idempotency key required")
        mutation_id = _hash(f"project:create:{ctx.actor_id}:{command.mutation.idempotency_key}")
        input_digest = _hash(f"{ctx.workspace_id}:{name}:{command.visibility}:{command.source_version_id}")
        async with self.dependencies.uow() as uow:
            previous = await uow.repository(ProjectMutation).get(_GLOBAL, mutation_id)
            if previous:
                if previous.input_digest != input_digest:
                    raise DomainError(ErrorCode.CONFLICT, "Idempotency key was used with different input")
                project = await self._located(uow, previous.result_id)
                if project is None:
                    raise DomainError(ErrorCode.CONFLICT, "Project creation record is inconsistent")
                return project
            project = Project(id=self.dependencies.ids.new(), workspace_id=ctx.workspace_id,
                              owner_id=ctx.actor_id, name=name, visibility=command.visibility,
                              revision=0, source_version_id=command.source_version_id)
            project = await uow.repository(Project).save(scope, project, None)
            await uow.repository(ProjectLocator).save(
                _GLOBAL, ProjectLocator(id=project.id, target_workspace_id=ctx.workspace_id), None)
            await uow.repository(ProjectMutation).save(
                _GLOBAL, ProjectMutation(id=mutation_id, result_id=project.id, input_digest=input_digest), None)
            await uow.commit()
        return project

    async def get(self, ctx: RequestContext, project_id: EntityId) -> Project:
        await self.dependencies.authorization.require(ctx, "project:read", project_id)
        async with self.dependencies.uow() as uow:
            project = await self._located(uow, project_id)
        if project is None:
            raise DomainError(ErrorCode.NOT_FOUND, "Project not found")
        return project

    async def list_projects(self, ctx: RequestContext, query: ProjectQuery) -> Page[Project]:
        scope = self._scope(ctx)
        await self.dependencies.authorization.require(ctx, "workspace:read", ctx.workspace_id)
        if not 1 <= query.page.limit <= 100:
            raise DomainError(ErrorCode.INVALID_INPUT, "Page limit must be 1–100")
        async with self.dependencies.uow() as uow:
            favorite = await uow.repository(ProjectFavoriteIndex).get(_GLOBAL, ctx.actor_id)
            favorite_ids = set(favorite.project_ids) if favorite else set()
            cursor = query.page.cursor
            items = []
            while len(items) <= query.page.limit:
                page = await uow.repository(Project).list(scope, PageQuery(cursor=cursor, limit=100))
                if not page.items:
                    break
                for project in page.items:
                    cursor = str(project.id)
                    if not query.favorites_only or project.id in favorite_ids:
                        items.append(project)
                        if len(items) > query.page.limit:
                            break
                if len(items) > query.page.limit or page.next_cursor is None:
                    break
        next_cursor = str(items[query.page.limit - 1].id) if len(items) > query.page.limit else None
        return Page(items=tuple(items[:query.page.limit]), next_cursor=next_cursor)

    async def rename(self, ctx: RequestContext, project_id: EntityId, name: str, mutation: Mutation) -> Project:
        await self.dependencies.authorization.require(ctx, "project:write", project_id)
        clean = name.strip()
        if not 1 <= len(clean) <= 120:
            raise DomainError(ErrorCode.INVALID_INPUT, "Project name must be 1–120 characters")
        async with self.dependencies.uow() as uow:
            project = await self._located(uow, project_id)
            if project is None:
                raise DomainError(ErrorCode.NOT_FOUND, "Project not found")
            if mutation.expected_revision is not None and mutation.expected_revision != project.revision:
                raise DomainError(ErrorCode.CONFLICT, "Project revision changed")
            project = await uow.repository(Project).save(
                Scope(workspace_id=project.workspace_id), replace(project, name=clean), project.revision)
            await uow.commit()
        return project

    async def set_visibility(self, ctx: RequestContext, project_id: EntityId,
                             visibility: Visibility, mutation: Mutation) -> Project:
        await self.dependencies.authorization.require(ctx, "project:write", project_id)
        async with self.dependencies.uow() as uow:
            project = await self._located(uow, project_id)
            if project is None:
                raise DomainError(ErrorCode.NOT_FOUND, "Project not found")
            if mutation.expected_revision is not None and mutation.expected_revision != project.revision:
                raise DomainError(ErrorCode.CONFLICT, "Project revision changed")
            project = await uow.repository(Project).save(
                Scope(workspace_id=project.workspace_id), replace(project, visibility=visibility), project.revision)
            await uow.commit()
        return project

    async def set_favorite(self, ctx: RequestContext, project_id: EntityId,
                           favorite: bool, mutation: Mutation) -> None:
        if ctx.actor_id is None:
            raise DomainError(ErrorCode.UNAUTHENTICATED, "Sign in required")
        await self.dependencies.authorization.require(ctx, "project:read", project_id)
        async with self.dependencies.uow() as uow:
            repo = uow.repository(ProjectFavoriteIndex)
            current = await repo.get(_GLOBAL, ctx.actor_id)
            ids = set(current.project_ids) if current else set()
            if favorite:
                ids.add(project_id)
            else:
                ids.discard(project_id)
            await repo.save(_GLOBAL, ProjectFavoriteIndex(id=ctx.actor_id, project_ids=tuple(sorted(ids))),
                            current.revision if current else None)
            await uow.commit()
