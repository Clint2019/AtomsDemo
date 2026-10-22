"""Integer-credit product pricing, separate from Alibaba's subscription credits."""
from atoms_demo.config import Settings
from atoms_demo.core.errors import DomainError, ErrorCode
from atoms_demo.ports.providers import UsagePricing


class UnconfiguredUsagePricing:
    async def reserve(self, model: str, prompt: str) -> int:
        if model == "manual":
            return 0
        raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Generation pricing is not configured")

    async def charge(self, model: str, input_tokens: int, output_tokens: int) -> int:
        if model == "manual":
            if (type(input_tokens) is not int or type(output_tokens) is not int or
                    input_tokens != 0 or output_tokens != 0):
                raise DomainError(ErrorCode.INVALID_INPUT, "Manual edit has no model usage")
            return 0
        raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Generation pricing is not configured")


class TokenUsagePricing:
    def __init__(self, model: str, input_rate: int, output_rate: int,
                 max_output_tokens: int, max_repairs: int):
        self.model = model
        self.input_rate = input_rate
        self.output_rate = output_rate
        self.max_output_tokens = max_output_tokens
        self.max_repairs = max_repairs

    def _check_model(self, model: str) -> None:
        if model not in {self.model, "manual"}:
            raise DomainError(ErrorCode.INVALID_INPUT, "Model price is not configured")

    async def reserve(self, model: str, prompt: str) -> int:
        self._check_model(model)
        if model == "manual":
            return 0
        # At most one initial call and max_repairs retries. The provider caps
        # serialized request size at 1 MB; 1.1M input tokens includes overhead.
        calls = self.max_repairs + 1
        maximum_numerator = calls * (1_100_000 * self.input_rate +
                                     self.max_output_tokens * self.output_rate)
        return max(1, (maximum_numerator + 999_999) // 1_000_000)

    async def charge(self, model: str, input_tokens: int, output_tokens: int) -> int:
        self._check_model(model)
        if (type(input_tokens) is not int or type(output_tokens) is not int or
                input_tokens < 0 or output_tokens < 0):
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid model usage")
        if model == "manual":
            if input_tokens or output_tokens:
                raise DomainError(ErrorCode.INVALID_INPUT, "Manual edit has no model usage")
            return 0
        numerator = input_tokens * self.input_rate + output_tokens * self.output_rate
        return (numerator + 999_999) // 1_000_000


def create_adapter(settings: Settings) -> UsagePricing:
    if settings.model_provider == "aliyun_token_plan" and settings.model_name:
        return TokenUsagePricing(settings.model_name,
            settings.model_input_credits_per_million,
            settings.model_output_credits_per_million,
            settings.model_max_output_tokens, settings.max_repairs)
    return UnconfiguredUsagePricing()
