"""Manual edits have zero model usage even without a model provider."""
import asyncio
import unittest

from atoms_demo.adapters.usage_pricing import TokenUsagePricing, UnconfiguredUsagePricing
from atoms_demo.core.errors import DomainError, ErrorCode


class ManualPricingTests(unittest.TestCase):
    def test_manual_without_model_is_free_but_model_is_unavailable(self):
        pricing = UnconfiguredUsagePricing()
        self.assertEqual(asyncio.run(pricing.reserve("manual", "manual edit")), 0)
        self.assertEqual(asyncio.run(pricing.charge("manual", 0, 0)), 0)
        for operation in (
            pricing.reserve("some-model", "prompt"),
            pricing.charge("some-model", 0, 0),
        ):
            with self.assertRaises(DomainError) as error:
                asyncio.run(operation)
            self.assertEqual(error.exception.code, ErrorCode.PROVIDER_UNAVAILABLE)

    def test_manual_rejects_nonzero_or_noninteger_usage(self):
        for pricing in (UnconfiguredUsagePricing(),
                        TokenUsagePricing("configured-model", 1000, 4000, 8192, 2)):
            for input_tokens, output_tokens in ((1, 0), (0, 1), (-1, 0),
                                                 (True, 0), (0, False)):
                with self.subTest(pricing=type(pricing).__name__,
                                  usage=(input_tokens, output_tokens)):
                    with self.assertRaises(DomainError) as error:
                        asyncio.run(pricing.charge("manual", input_tokens, output_tokens))
                    self.assertEqual(error.exception.code, ErrorCode.INVALID_INPUT)


if __name__ == "__main__":
    unittest.main()
