"""Generation API validation around an injected model provider."""
from __future__ import annotations
from atoms_demo.contracts.generation import ModelOption, GenerationInput, FileChangeSet, RepairInput
from atoms_demo.core.errors import DomainError, ErrorCode, MeteredProviderError, UnsentModelError
from atoms_demo.core.types import RequestContext
from atoms_demo.ports.dependencies import GenerationDependencies


class GenerationService:
    def __init__(self, dependencies: GenerationDependencies) -> None:
        self.dependencies = dependencies

    async def models(self, ctx: RequestContext) -> tuple[ModelOption, ...]:
        if not self.dependencies.model_name:
            return ()
        return (ModelOption(key=self.dependencies.model_name, label=self.dependencies.model_name,
                            available=self.dependencies.model_configured),)

    @staticmethod
    def _input(ctx: RequestContext, command: GenerationInput) -> None:
        if ctx.actor_id is None:
            raise UnsentModelError(ErrorCode.UNAUTHENTICATED, "Sign in required")
        if not command.prompt.strip() or len(command.prompt) > 50_000:
            raise UnsentModelError(ErrorCode.INVALID_INPUT, "Prompt is invalid")

    @staticmethod
    def _output(result: FileChangeSet) -> FileChangeSet:
        if (type(result.input_tokens) is not int or type(result.output_tokens) is not int or
                result.input_tokens < 0 or result.output_tokens < 0 or
                not result.upserts and not result.deleted_paths):
            if (type(result.input_tokens) is int and type(result.output_tokens) is int and
                    result.input_tokens >= 0 and result.output_tokens >= 0):
                raise MeteredProviderError("Model returned invalid changes",
                                           result.input_tokens, result.output_tokens)
            raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Model returned invalid changes")
        return result

    async def generate(self, ctx: RequestContext, command: GenerationInput) -> FileChangeSet:
        self._input(ctx, command)
        if not self.dependencies.model_configured:
            raise UnsentModelError(ErrorCode.PROVIDER_UNAVAILABLE, "Model provider is not configured")
        return self._output(await self.dependencies.model.generate(command))

    async def repair(self, ctx: RequestContext, command: RepairInput) -> FileChangeSet:
        self._input(ctx, command.generation)
        if not 1 <= command.repair_number <= 2:
            raise UnsentModelError(ErrorCode.INVALID_INPUT, "Repair number exceeds limit")
        if not self.dependencies.model_configured:
            raise UnsentModelError(ErrorCode.PROVIDER_UNAVAILABLE, "Model provider is not configured")
        return self._output(await self.dependencies.model.repair(command))
