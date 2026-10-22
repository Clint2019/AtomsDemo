"""Build result validation around an injected isolated executor."""
from __future__ import annotations
from atoms_demo.contracts.builds import BuildInput, BuildResult, ScanResult
from atoms_demo.core.errors import DomainError, ErrorCode
from atoms_demo.core.types import BlobRef, EntityId, RequestContext
from atoms_demo.ports.dependencies import BuildDependencies


class BuildService:
    def __init__(self, dependencies: BuildDependencies) -> None:
        self.dependencies = dependencies

    async def build(self, ctx: RequestContext, command: BuildInput) -> BuildResult:
        if ctx.actor_id is None:
            raise DomainError(ErrorCode.UNAUTHENTICATED, "Sign in required")
        if not command.template_id:
            raise DomainError(ErrorCode.INVALID_INPUT, "Build recipe is required")
        result = await self.dependencies.builder.build(command)
        if (result.version_id != command.version_id or type(result.success) is not bool or
                result.success and not isinstance(result.artifact, BlobRef) or
                not isinstance(result.diagnostics, tuple)):
            raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Build executor returned invalid result")
        return result

    async def scan(self, ctx: RequestContext, version_id: EntityId, artifact: BlobRef) -> ScanResult:
        if ctx.actor_id is None:
            raise DomainError(ErrorCode.UNAUTHENTICATED, "Sign in required")
        result = await self.dependencies.builder.scan(version_id, artifact)
        if result.version_id != version_id or type(result.passed) is not bool or not isinstance(result.findings, tuple):
            raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Scan result version differs")
        return result
