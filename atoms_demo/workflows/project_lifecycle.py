from dataclasses import dataclass
from atoms_demo.core.types import EntityId, Mutation, RequestContext
from atoms_demo.core.errors import DomainError, ErrorCode
from atoms_demo.contracts.projects import Project, CreateProject, Visibility
from atoms_demo.contracts.versions import CreateVersion, ProjectHead, PromoteVersion
from atoms_demo.workflows.registry import LifecycleServices


@dataclass(frozen=True, kw_only=True)
class CloneProject:
    source_version_id: EntityId
    target_name: str
    source_entry_id: EntityId | None
    mutation: Mutation


class ProjectLifecycle:
    def __init__(self, services: LifecycleServices) -> None:
        self.services = services

    async def clone(self, ctx: RequestContext, command: CloneProject) -> Project:
        if not command.mutation.idempotency_key:
            raise DomainError(ErrorCode.INVALID_INPUT, "Idempotency key required")
        source = await self.services.versions.clone_source(ctx, command.source_version_id)
        if command.source_entry_id:
            entry = await self.services.catalog.detail(ctx, command.source_entry_id)
            if entry.version_id != source.id or entry.project_id != source.project_id:
                raise DomainError(ErrorCode.FORBIDDEN, "Catalog entry does not match clone source")
        project = await self.services.projects.create(ctx, CreateProject(
            name=command.target_name, visibility=Visibility.PRIVATE,
            source_version_id=source.id,
            mutation=Mutation(idempotency_key=f"clone:project:{command.mutation.idempotency_key}")))
        candidate = await self.services.versions.create_candidate(ctx, CreateVersion(
            project_id=project.id, base_version_id=None, files=source.files,
            summary=f"Clone of {source.id}",
            mutation=Mutation(idempotency_key=f"clone:version:{command.mutation.idempotency_key}")))
        await self.services.versions.record_build(ctx, candidate.id, source.artifact, True)
        await self.services.versions.promote(ctx, PromoteVersion(
            project_id=project.id, version_id=candidate.id, expected_head_revision=0,
            task_id=None,
            mutation=Mutation(idempotency_key=f"clone:promote:{command.mutation.idempotency_key}")))
        return project

    async def restore(self, ctx: RequestContext, command: PromoteVersion) -> ProjectHead:
        if command.task_id is not None or command.lease_owner is not None:
            raise DomainError(ErrorCode.INVALID_INPUT, "Restore cannot claim a task lease")
        return await self.services.versions.promote(ctx, command)
