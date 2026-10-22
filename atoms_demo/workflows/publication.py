from atoms_demo.core.types import Environment, RequestContext
from atoms_demo.core.errors import DomainError, ErrorCode
from atoms_demo.contracts.app_resources import ResourceKind
from atoms_demo.contracts.publication import Publication, PublicationConfig
from atoms_demo.workflows.registry import PublicationServices


class PublicationWorkflow:
    def __init__(self, services: PublicationServices) -> None:
        self.services = services

    async def prepare(self, ctx: RequestContext, command: PublicationConfig) -> Publication:
        prior = await self.services.publication.configured_by_mutation(ctx, command)
        if prior is not None:
            return prior
        if command.environment != Environment.PRODUCTION:
            raise DomainError(ErrorCode.INVALID_INPUT, "Publication requires production environment")
        version = await self.services.versions.get(ctx, command.version_id)
        if version.project_id != command.project_id:
            raise DomainError(ErrorCode.FORBIDDEN, "Version belongs to another project")
        if version.build_status != "success" or version.artifact is None:
            raise DomainError(ErrorCode.CONFLICT, "Only a successful version can be published")
        scan = await self.services.builds.scan(ctx, version.id, version.artifact)
        if not scan.passed:
            raise DomainError(ErrorCode.CONFLICT, "Publication scan did not pass")
        if command.migrate_data:
            raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE,
                "Production data migration is not configured")
        if command.sync_db:
            resources = await self.services.app_resources.resources(ctx, command.project_id,
                Environment.PRODUCTION)
            if not any(item.kind == ResourceKind.DATABASE and item.status == "configured"
                       for item in resources):
                raise DomainError(ErrorCode.CONFLICT, "Production database resource is required")
        return await self.services.publication.configure(ctx, command)
