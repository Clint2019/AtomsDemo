from datetime import timedelta

from atoms_demo.core.types import EntityId, Environment, Mutation, PreviewGrant, RequestContext
from atoms_demo.core.errors import DomainError, ErrorCode
from atoms_demo.contracts.preview import PreviewTicket, ShareLink
from atoms_demo.contracts.app_resources import AppSession
from atoms_demo.ports.runtime import Clock
from atoms_demo.workflows.registry import PreviewServices


class PreviewWorkflow:
    def __init__(self, services: PreviewServices, clock: Clock) -> None:
        self.services = services
        self.clock = clock

    async def _grant(self, ctx: RequestContext, project_id: EntityId,
                     version_id: EntityId) -> PreviewGrant:
        version = await self.services.versions.get(ctx, version_id)
        if version.project_id != project_id:
            raise DomainError(ErrorCode.FORBIDDEN, "Version belongs to another project")
        if version.build_status != "success" or version.artifact is None:
            raise DomainError(ErrorCode.CONFLICT, "Only a successful version can be previewed")
        return PreviewGrant(project_id=project_id, version_id=version_id,
            artifact=version.artifact, environment=Environment.DEVELOPMENT,
            expires_at=self.clock.now() + timedelta(minutes=15))

    async def open(self, ctx: RequestContext, project_id: EntityId, version_id: EntityId) -> PreviewTicket:
        return await self.services.preview.issue_ticket(ctx,
            await self._grant(ctx, project_id, version_id))

    async def open_catalog(self, ctx: RequestContext, entry_id: EntityId) -> PreviewTicket:
        entry = await self.services.catalog.detail(ctx, entry_id)
        version = await self.services.versions.clone_source(ctx, entry.version_id)
        if version.project_id != entry.project_id or version.build_status != "success" or version.artifact is None:
            raise DomainError(ErrorCode.NOT_FOUND, "Public work is unavailable")
        return await self.services.preview.issue_catalog_ticket(PreviewGrant(
            project_id=entry.project_id, version_id=entry.version_id,
            artifact=version.artifact, environment=Environment.PUBLIC_PREVIEW,
            expires_at=self.clock.now() + timedelta(minutes=15),
            public_author_id=entry.author_id), entry.author_id)

    async def share(self, ctx: RequestContext, project_id: EntityId, version_id: EntityId, mutation: Mutation) -> ShareLink:
        return await self.services.preview.create_share(ctx,
            await self._grant(ctx, project_id, version_id), mutation)

    async def app_session(self, preview_token: str, existing_app_token: str | None) -> AppSession:
        grant = await self.services.preview.resolve_ticket(preview_token)
        return await self.services.app_resources.open_app_session(grant, existing_app_token)
