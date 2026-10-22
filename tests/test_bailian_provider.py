"""Token Plan adapter and local product-credit rules without remote calls."""
import asyncio
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from cryptography.fernet import Fernet

from atoms_demo.adapters.model_provider import BailianTokenPlanProvider, create_adapter
from atoms_demo.adapters.usage_pricing import TokenUsagePricing
from atoms_demo.bootstrap import compose_application
from atoms_demo.config import Settings, load_settings
from atoms_demo.contracts.billing import AccountKind, UsageCharge
from atoms_demo.contracts.generation import GenerationInput, RepairInput, SourceFile
from atoms_demo.core.errors import DomainError, ErrorCode, MeteredProviderError, UnsentModelError
from atoms_demo.core.types import BlobRef, EntityId, Mutation, PageQuery, RequestContext


class FakeBlobs:
    async def read(self, reference):
        return b"attachment text"


class BailianProviderTests(unittest.TestCase):
    def _settings(self, root: Path) -> Settings:
        return Settings(database_url=f"sqlite:///{root / 'data.db'}", blob_directory=root / "blobs",
            public_origin="http://localhost:5173", preview_origin="http://preview.localhost:8001",
            model_provider="aliyun_token_plan", model_secret_name="TEST_BAILIAN_KEY",
            task_timeout_seconds=180, model_name="qwen3.7-plus", initial_build_credits=100000)

    def _command(self) -> GenerationInput:
        return GenerationInput(task_id=EntityId("task-1"), prompt="Make a todo app",
            context_summary="", files=(SourceFile(path="src/App.tsx", content="old"),),
            attachments=(BlobRef(id=EntityId("blob"), sha256="0" * 64,
                                 size_bytes=15, media_type="text/plain"),),
            model="qwen3.7-plus", agent_mode="normal")

    def test_adapter_maps_generation_repair_and_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(Path(directory))
            with patch.dict(os.environ, {"TEST_BAILIAN_KEY": "sk-sp-local-test-key"}):
                provider = create_adapter(replace(settings, task_timeout_seconds=42,
                    model_timeout_seconds=333), FakeBlobs())
            self.assertIsInstance(provider, BailianTokenPlanProvider)
            self.assertEqual(provider.timeout_seconds, 333)
            payloads = []

            def fake_post(payload):
                payloads.append(json.loads(payload))
                return {"choices": [{"message": {"content": json.dumps({
                    "summary": "created", "upserts": [{"path": "src/App.tsx", "content": "new"}],
                    "deleted_paths": []})}}], "usage": {"prompt_tokens": 1200,
                                                    "completion_tokens": 250}}

            with patch.object(provider, "_post", side_effect=fake_post):
                result = asyncio.run(provider.generate(self._command()))
                repaired = asyncio.run(provider.repair(RepairInput(
                    generation=self._command(), diagnostics=("syntax error",), repair_number=1)))
            self.assertEqual((result.input_tokens, result.output_tokens), (1200, 250))
            self.assertEqual(result.upserts[0].content, "new")
            self.assertEqual(repaired.summary, "created")
            first = json.loads(payloads[0]["messages"][1]["content"])
            second = json.loads(payloads[1]["messages"][1]["content"])
            response_format = payloads[0]["response_format"]
            self.assertEqual(response_format["type"], "json_schema")
            self.assertTrue(response_format["json_schema"]["strict"])
            schema = response_format["json_schema"]["schema"]
            self.assertEqual(set(schema["required"]), {"summary", "upserts", "deleted_paths"})
            self.assertEqual(set(schema["properties"]["upserts"]["items"]["required"]),
                             {"path", "content"})
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(first["attachments"][0]["content"], "attachment text")
            self.assertEqual(second["build_diagnostics"], ["syntax error"])
            instruction = payloads[0]["messages"][0]["content"]
            self.assertIn("GET /app/collections/{collection}/records?limit=100", instruction)
            self.assertIn("PUT /app/records/{id}", instruction)
            self.assertIn("DELETE /app/records/{id}", instruction)
            self.assertIn("not localStorage", instruction)

    def test_missing_key_bad_endpoint_and_missing_usage_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(Path(directory))
            with patch.dict(os.environ, {}, clear=True):
                self.assertNotIsInstance(create_adapter(settings, FakeBlobs()), BailianTokenPlanProvider)
            with self.assertRaises(ValueError):
                BailianTokenPlanProvider(key="sk-sp-local-test-key", model="qwen3.7-plus",
                    timeout_seconds=10, max_output_tokens=1000, blobs=FakeBlobs(),
                    base_url="https://example.com/v1")
            with self.assertRaisesRegex(ValueError, "sk-sp-"):
                BailianTokenPlanProvider(key="sk-standard-key", model="qwen3.7-plus",
                    timeout_seconds=10, max_output_tokens=1000, blobs=FakeBlobs())
            provider = BailianTokenPlanProvider(key="sk-sp-local-test-key", model="qwen3.7-plus",
                timeout_seconds=10, max_output_tokens=1000, blobs=FakeBlobs())
            with patch.object(provider, "_post", return_value={"choices": [{"message": {
                    "content": '{"summary":"x","upserts":[{"path":"a","content":"x"}],"deleted_paths":[]}'}}]}):
                with self.assertRaisesRegex(DomainError, "missing usage"):
                    asyncio.run(provider.generate(self._command()))
            with self.assertRaises(DomainError):
                asyncio.run(provider.generate(replace(self._command(), model="other")))
            unsupported = replace(self._command(), attachments=(replace(
                self._command().attachments[0], media_type="image/png"),))
            with patch.object(provider, "_post") as post:
                with self.assertRaises(UnsentModelError):
                    asyncio.run(provider.generate(unsupported))
                post.assert_not_called()
            with patch.object(provider, "_post", return_value={"choices": [{"message": {
                    "content": "not JSON"}}], "usage": {"prompt_tokens": 1000,
                                                    "completion_tokens": 1000}}):
                with self.assertRaises(MeteredProviderError) as failure:
                    asyncio.run(provider.generate(self._command()))
            self.assertEqual((failure.exception.input_tokens, failure.exception.output_tokens),
                             (1000, 1000))

    def test_provider_failure_logs_only_safe_diagnostics(self):
        provider = BailianTokenPlanProvider(key="sk-sp-local-test-key", model="qwen3.7-plus",
            timeout_seconds=10, max_output_tokens=1000, blobs=FakeBlobs())
        command = replace(self._command(), prompt="private-prompt-marker")
        with patch.object(provider, "_post", side_effect=DomainError(
                ErrorCode.PROVIDER_UNAVAILABLE, "Model provider request timed out")):
            with self.assertLogs("atoms_demo.adapters.model_provider", level="WARNING") as captured:
                with self.assertRaisesRegex(DomainError, "timed out"):
                    asyncio.run(provider.generate(command))
        line = captured.output[0]
        self.assertIn("task_id=task-1", line)
        self.assertIn("phase=transport", line)
        self.assertIn("reason=Model provider request timed out", line)
        self.assertNotIn("private-prompt-marker", line)
        self.assertNotIn("sk-sp-local-test-key", line)

        with patch.object(provider, "_post", return_value={"choices": [], "usage": None}):
            with self.assertLogs("atoms_demo.adapters.model_provider", level="WARNING") as captured:
                with self.assertRaisesRegex(DomainError, "missing usage"):
                    asyncio.run(provider.generate(command))
        self.assertIn("phase=response_validation", captured.output[0])
        self.assertNotIn("private-prompt-marker", captured.output[0])

    def test_url_timeout_is_distinct_from_connection_failure(self):
        provider = BailianTokenPlanProvider(key="sk-sp-local-test-key", model="qwen3.7-plus",
            timeout_seconds=10, max_output_tokens=1000, blobs=FakeBlobs())
        with patch.object(provider._opener, "open", side_effect=URLError(TimeoutError())):
            with self.assertRaisesRegex(DomainError, "timed out"):
                provider._post(b"{}")

    def test_existing_token_plan_variable_is_the_default_secret_name(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {"ATOMS_DATABASE_URL": f"sqlite:///{Path(directory) / 'db.sqlite'}",
                   "ATOMS_BLOB_DIRECTORY": str(Path(directory) / "blobs"),
                   "ATOMS_PUBLIC_ORIGIN": "http://app.preview.localhost:5173",
                   "ATOMS_PREVIEW_ORIGIN": "http://preview.localhost:8001",
                   "ATOMS_MODEL_PROVIDER": "aliyun_token_plan"}
            settings = load_settings(env)
            self.assertEqual(settings.model_secret_name, "OPS_TOKEN_PLAN_API_KEY")
            self.assertEqual(settings.model_name, "qwen3.7-plus")
            with patch.dict(os.environ, {"OPS_TOKEN_PLAN_API_KEY": "sk-sp-local-test-key",
                                      "ATOMS_SECRET_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii")}):
                self.assertIsInstance(create_adapter(settings, FakeBlobs()), BailianTokenPlanProvider)
                application = compose_application(settings)
                ctx = RequestContext(actor_id=None, session_id=None,
                                     workspace_id=None, request_id="models")
                options = asyncio.run(application.services.generation.models(ctx))
                self.assertEqual([(item.key, item.available) for item in options],
                                 [("qwen3.7-plus", True)])

    def test_credit_rounding_and_initial_grant_once(self):
        pricing = TokenUsagePricing("qwen3.7-plus", 1000, 4000, 8192, 2)
        self.assertEqual(asyncio.run(pricing.charge("qwen3.7-plus", 1000, 1000)), 5)
        self.assertEqual(asyncio.run(pricing.charge("qwen3.7-plus", 1, 0)), 1)
        self.assertGreaterEqual(asyncio.run(pricing.reserve("qwen3.7-plus", "x")), 5)
        self.assertEqual(asyncio.run(pricing.reserve("manual", "manual edit")), 0)
        self.assertEqual(asyncio.run(pricing.charge("manual", 0, 0)), 0)

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                settings = self._settings(Path(directory))
                with patch.dict(os.environ, {"ATOMS_SECRET_ENCRYPTION_KEY":
                                                 Fernet.generate_key().decode("ascii")}):
                    app = compose_application(settings)
                owner = EntityId("owner")
                creation = RequestContext(actor_id=owner, session_id=None,
                                          workspace_id=None, request_id="create")
                workspace = await app.services.workspaces.create(creation, "Personal",
                    Mutation(idempotency_key="workspace"))
                ctx = replace(creation, workspace_id=workspace.id)
                billing = app.services.billing
                self.assertEqual((await billing.balances(ctx))[0].available, 100000)
                self.assertEqual((await billing.balances(ctx))[0].available, 100000)
                entries = await billing.ledger(ctx, AccountKind.BUILD_CREDITS, PageQuery())
                self.assertEqual(len(entries.items), 1)
                reservation = await billing.reserve(ctx, EntityId("task"), 100,
                    Mutation(idempotency_key="reserve"))
                await billing.settle(ctx, UsageCharge(reservation_id=reservation.id,
                    consumed_units=5, source_event_id="task:usage"))
                self.assertEqual((await billing.balances(ctx))[0].available, 99995)
                zero = await billing.reserve(ctx, EntityId("manual-task"), 0,
                    Mutation(idempotency_key="manual"))
                await billing.settle(ctx, UsageCharge(reservation_id=zero.id,
                    consumed_units=0, source_event_id="manual:usage"))
                self.assertEqual((await billing.balances(ctx))[0].available, 99995)
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
