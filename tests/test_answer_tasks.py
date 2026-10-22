"""Questions are answered without creating an application version."""
import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from atoms_demo.api.task_worker import recover, run_once
from atoms_demo.adapters.model_provider import BailianTokenPlanProvider
from atoms_demo.bootstrap import compose_application
from atoms_demo.config import Settings
from atoms_demo.contracts.billing import AccountKind
from atoms_demo.contracts.conversations import MessageRole
from atoms_demo.contracts.generation import AnswerResult, GenerationInput
from atoms_demo.contracts.projects import CreateProject, Visibility
from atoms_demo.contracts.storage import Upload
from atoms_demo.contracts.tasks import TaskState
from atoms_demo.core.errors import DomainError, ErrorCode, UnsentModelError
from atoms_demo.core.types import EntityId, Mutation, PageQuery, RequestContext, Scope
from atoms_demo.workflows.generation import GenerateApplication, _asks_how_to


class AnswerModel:
    def __init__(self, failure=None):
        self.calls = 0
        self.failure = failure
        self.on_answer = None
        self.last_command = None

    async def answer(self, command):
        self.calls += 1
        self.last_command = command
        if self.on_answer:
            await self.on_answer(command)
        if self.failure:
            raise self.failure
        return AnswerResult(content="先描述用途和字段，再确认需要构建。", input_tokens=20,
                            output_tokens=30)

    async def generate(self, command):
        raise AssertionError("Question must not enter application generation")

    async def repair(self, command):
        raise AssertionError("Question must not enter automatic repair")


class NoBuild:
    async def build(self, command):
        raise AssertionError("Question must not start a build")

    async def scan(self, version_id, artifact):
        raise AssertionError("Question must not scan an artifact")


class FixedPricing:
    async def reserve(self, model, prompt):
        return 100

    async def charge(self, model, input_tokens, output_tokens):
        return 10


class AnswerTaskTests(unittest.TestCase):
    def test_text_provider_validates_response_and_usage(self):
        class Blobs:
            async def read(self, reference):
                return b""

        provider = BailianTokenPlanProvider(key="sk-sp-local-test-key", model="stub",
            timeout_seconds=10, max_output_tokens=32768, blobs=Blobs())
        command = GenerationInput(task_id=EntityId("answer-provider"),
            prompt="如何创建一个任务？", context_summary="", files=(), attachments=(),
            model="stub", agent_mode="answer")
        payloads = []

        def respond(payload):
            payloads.append(json.loads(payload))
            return {"choices": [{"message": {"content": "先列出任务字段。"}}],
                    "usage": {"prompt_tokens": 8, "completion_tokens": 9}}

        with patch.object(provider, "_post", side_effect=respond):
            answer = asyncio.run(provider.answer(command))
        self.assertEqual((answer.content, answer.input_tokens, answer.output_tokens),
                         ("先列出任务字段。", 8, 9))
        self.assertEqual(payloads[0]["max_completion_tokens"], 8192)
        self.assertNotIn("response_format", payloads[0])
        self.assertIn("do not generate source files", payloads[0]["messages"][0]["content"])
        with patch.object(provider, "_post", return_value={"choices": [{"message": {
                "content": ""}}], "usage": {"prompt_tokens": 8, "completion_tokens": 9}}):
            with self.assertRaisesRegex(DomainError, "invalid answer"):
                asyncio.run(provider.answer(command))
        with patch.object(provider, "_post", return_value={"choices": [{"message": {
                "content": "text"}}]}):
            with self.assertRaisesRegex(DomainError, "missing usage"):
                asyncio.run(provider.answer(command))

    def test_howto_routing_is_conservative(self):
        self.assertTrue(_asks_how_to("如何创建一个求职管理网页"))
        self.assertTrue(_asks_how_to("请问怎么创建一个日程管理应用？"))
        self.assertTrue(_asks_how_to("How do I create a task tracker?"))
        self.assertFalse(_asks_how_to("帮我创建一个求职管理网页"))
        self.assertFalse(_asks_how_to("怎么创建一个任务管理网页？现在帮我创建"))

    def test_answer_is_durable_metered_and_never_builds(self):
        async def run():
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                settings = Settings(database_url=f"sqlite:///{root / 'db.sqlite'}",
                    blob_directory=root / "blobs", public_origin="http://localhost:5173",
                    preview_origin="http://preview.localhost:8001", model_provider="stub",
                    model_name="stub", model_secret_name="", task_timeout_seconds=180,
                    initial_build_credits=100000)
                secret = Fernet.generate_key().decode()
                with patch.dict(os.environ, {"ATOMS_SECRET_ENCRYPTION_KEY": secret}):
                    app = compose_application(settings)
                actor = EntityId("answer-owner")
                creation = RequestContext(actor_id=actor, session_id=EntityId("answer-session"),
                                          workspace_id=None, request_id="answer-test")
                workspace = await app.services.workspaces.create(creation, "Personal",
                    Mutation(idempotency_key="answer-workspace"))
                ctx = replace(creation, workspace_id=workspace.id)
                project = await app.services.projects.create(ctx, CreateProject(
                    name="Question", visibility=Visibility.PRIVATE,
                    mutation=Mutation(idempotency_key="answer-project")))
                model = AnswerModel()
                app.services.generation.dependencies = replace(
                    app.services.generation.dependencies, model=model, model_configured=True)
                app.services.builds.dependencies = replace(
                    app.services.builds.dependencies, builder=NoBuild())
                app.generation.pricing = FixedPricing()
                scope = Scope(workspace_id=workspace.id, project_id=project.id)
                attachment = await app.services.storage.upload(ctx, Upload(
                    scope=scope, purpose="attachment", display_name="notes.txt",
                    media_type="text/plain", content="日程字段：标题、日期".encode(),
                    mutation=Mutation(idempotency_key="answer-attachment")))
                invalid_attachment = await app.services.storage.upload(ctx, Upload(
                    scope=scope, purpose="attachment", display_name="bad.txt",
                    media_type="text/plain", content=b"\xff",
                    mutation=Mutation(idempotency_key="invalid-answer-attachment")))
                with self.assertRaisesRegex(DomainError, "UTF-8"):
                    await app.generation.submit(ctx, GenerateApplication(
                        project_id=project.id, prompt="如何安排日程？",
                        attachment_ids=(invalid_attachment.id,), model="stub",
                        agent_mode="answer", expected_head_revision=0,
                        mutation=Mutation(idempotency_key="rejected-answer")))
                self.assertIsNone(await app.services.tasks.find_submission(
                    ctx, project.id, "rejected-answer"))
                command = GenerateApplication(project_id=project.id,
                    prompt="如何创建一个求职管理网页", attachment_ids=(attachment.id,), model="stub",
                    agent_mode="approval", expected_head_revision=0,
                    mutation=Mutation(idempotency_key="answer-request"))
                task = await app.generation.submit(ctx, command)
                self.assertEqual(task.request.agent_mode, "answer")
                self.assertEqual(task.state, TaskState.QUEUED)
                self.assertEqual((await app.generation.submit(ctx, command)).id, task.id)
                with patch.dict(os.environ, {"ATOMS_SECRET_ENCRYPTION_KEY": secret}):
                    restarted = compose_application(settings)
                restarted.services.generation.dependencies = replace(
                    restarted.services.generation.dependencies, model=model,
                    model_configured=True)
                restarted.services.builds.dependencies = replace(
                    restarted.services.builds.dependencies, builder=NoBuild())
                restarted.generation.pricing = FixedPricing()
                await restarted.services.tasks.recover_interrupted()
                self.assertTrue(await run_once(restarted, "answer-worker", 60))
                completed = await restarted.services.tasks.get(ctx, task.id)
                self.assertEqual(completed.state, TaskState.SUCCEEDED,
                                 completed.error_code)
                self.assertIsNone(completed.result_version_id)
                self.assertEqual(model.calls, 1)
                self.assertEqual(model.last_command.attachments, (attachment.blob,))
                head = await restarted.services.versions.head(ctx, project.id)
                self.assertEqual((head.version_id, head.revision), (None, 0))
                history = await restarted.services.conversations.history_recent(
                    ctx, project.id, PageQuery(limit=10))
                self.assertEqual([item.role for item in history.items],
                                 [MessageRole.USER, MessageRole.ASSISTANT])
                self.assertEqual(history.items[-1].content,
                                 "先描述用途和字段，再确认需要构建。")
                self.assertEqual(history.items[-1].task_id, task.id)
                self.assertEqual(await restarted.services.billing.task_reservation_status(
                    workspace.id, task.id), "settled")
                self.assertEqual((await restarted.services.billing.balances(ctx))[0].available,
                                 99990)
                self.assertEqual([event.kind for event in await restarted.services.tasks.events(
                    ctx, task.id, 0)], ["queued", "generating", "succeeded"])

                async def cancel_while_model_finishes(command):
                    await restarted.generation.cancel(ctx, command.task_id,
                        Mutation(idempotency_key="cancel-answer"))

                model.on_answer = cancel_while_model_finishes
                cancelled_task = await restarted.generation.submit(ctx, replace(command,
                    prompt="可以怎么规划？", agent_mode="answer",
                    mutation=Mutation(idempotency_key="cancelled-answer")))
                self.assertTrue(await run_once(restarted, "cancelled-answer-worker", 60))
                self.assertEqual((await restarted.services.tasks.get(ctx, cancelled_task.id)).state,
                                 TaskState.CANCELLED)
                history = await restarted.services.conversations.history_recent(
                    ctx, project.id, PageQuery(limit=10))
                self.assertEqual([item.role for item in history.items],
                                 [MessageRole.USER, MessageRole.ASSISTANT, MessageRole.USER])
                self.assertEqual((await restarted.services.versions.head(ctx, project.id)).revision, 0)

                model.on_answer = None
                original_settle = restarted.services.billing.settle
                did_cancel = False

                async def cancel_after_checkpoint(settle_ctx, charge):
                    nonlocal did_cancel
                    if not did_cancel:
                        did_cancel = True
                        await restarted.generation.cancel(ctx, checkpoint_cancelled.id,
                            Mutation(idempotency_key="cancel-after-checkpoint"))
                    return await original_settle(settle_ctx, charge)

                checkpoint_cancelled = await restarted.generation.submit(ctx, replace(command,
                    prompt="如何配置提醒？", agent_mode="answer", attachment_ids=(),
                    mutation=Mutation(idempotency_key="checkpoint-cancelled")))
                with patch.object(restarted.services.billing, "settle",
                                  side_effect=cancel_after_checkpoint):
                    self.assertTrue(await run_once(restarted, "checkpoint-cancel-worker", 60))
                self.assertEqual((await restarted.services.tasks.get(
                    ctx, checkpoint_cancelled.id)).state, TaskState.CANCELLED)
                self.assertIsNotNone(await restarted.services.tasks.answer_checkpoint(
                    ctx, checkpoint_cancelled.id))
                await recover(restarted)
                self.assertEqual([item.role for item in (await
                    restarted.services.conversations.history_recent(
                        ctx, project.id, PageQuery(limit=10))).items],
                    [MessageRole.USER, MessageRole.ASSISTANT,
                     MessageRole.USER, MessageRole.USER])

                async def lose_lease_after_model(command):
                    db = sqlite3.connect(root / "db.sqlite")
                    try:
                        db.execute("UPDATE jobs SET leased_until=? WHERE task_id=?",
                                   ("2000-01-01T00:00:00+00:00", str(command.task_id)))
                        db.commit()
                    finally:
                        db.close()

                model.on_answer = lose_lease_after_model
                lost_lease_task = await restarted.generation.submit(ctx, replace(command,
                    prompt="怎么规划一个看板？", agent_mode="answer", attachment_ids=(),
                    mutation=Mutation(idempotency_key="lost-lease-answer")))
                self.assertTrue(await run_once(restarted, "lost-answer-worker", 60))
                self.assertEqual((await restarted.services.tasks.get(ctx, lost_lease_task.id)).state,
                                 TaskState.FAILED)
                self.assertIsNone(await restarted.services.tasks.answer_checkpoint(
                    ctx, lost_lease_task.id))
                history = await restarted.services.conversations.history_recent(
                    ctx, project.id, PageQuery(limit=10))
                self.assertEqual([item.role for item in history.items],
                                 [MessageRole.USER, MessageRole.ASSISTANT,
                                  MessageRole.USER, MessageRole.USER, MessageRole.USER])
                await restarted.services.storage.delete(ctx, attachment.id,
                    Mutation(idempotency_key="delete-answer-attachment"))
                self.assertEqual((await restarted.generation.submit(ctx, command)).id, task.id)
                confirmed = GenerateApplication(project_id=project.id,
                    prompt="怎么创建一个网页，再直接帮我做一个", attachment_ids=(),
                    model="stub", agent_mode="approval", expected_head_revision=0,
                    mutation=Mutation(idempotency_key="confirmed-build"),
                    confirmed_build_intent=True)
                build_task = await restarted.generation.submit(ctx, confirmed)
                self.assertEqual(build_task.state, TaskState.WAITING_FOR_APPROVAL)
                self.assertEqual(build_task.request.agent_mode, "approval")
                self.assertTrue(build_task.request.confirmed_build_intent)
                self.assertEqual((await restarted.generation.submit(ctx, confirmed)).id,
                                 build_task.id)
                with self.assertRaisesRegex(DomainError, "Idempotency key"):
                    await restarted.generation.submit(ctx, replace(confirmed,
                        confirmed_build_intent=False))

        asyncio.run(run())

    def test_unsent_answer_failure_releases_reservation_without_build(self):
        async def run():
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                settings = Settings(database_url=f"sqlite:///{root / 'db.sqlite'}",
                    blob_directory=root / "blobs", public_origin="http://localhost:5173",
                    preview_origin="http://preview.localhost:8001", model_provider="stub",
                    model_name="stub", model_secret_name="", task_timeout_seconds=180,
                    initial_build_credits=100000)
                with patch.dict(os.environ, {"ATOMS_SECRET_ENCRYPTION_KEY":
                                                 Fernet.generate_key().decode()}):
                    app = compose_application(settings)
                actor = EntityId("failed-answer-owner")
                creation = RequestContext(actor_id=actor, session_id=EntityId("answer-session"),
                                          workspace_id=None, request_id="answer-failure")
                workspace = await app.services.workspaces.create(creation, "Personal",
                    Mutation(idempotency_key="answer-workspace"))
                ctx = replace(creation, workspace_id=workspace.id)
                project = await app.services.projects.create(ctx, CreateProject(
                    name="Question", visibility=Visibility.PRIVATE,
                    mutation=Mutation(idempotency_key="answer-project")))
                model = AnswerModel(UnsentModelError(ErrorCode.PROVIDER_UNAVAILABLE,
                    "Model unavailable before dispatch"))
                app.services.generation.dependencies = replace(
                    app.services.generation.dependencies, model=model, model_configured=True)
                app.services.builds.dependencies = replace(
                    app.services.builds.dependencies, builder=NoBuild())
                app.generation.pricing = FixedPricing()
                task = await app.generation.submit(ctx, GenerateApplication(
                    project_id=project.id, prompt="怎么创建项目？", attachment_ids=(),
                    model="stub", agent_mode="default", expected_head_revision=0,
                    mutation=Mutation(idempotency_key="failed-answer")))
                self.assertTrue(await run_once(app, "failed-answer-worker", 60))
                failed = await app.services.tasks.get(ctx, task.id)
                self.assertEqual(failed.state, TaskState.FAILED)
                self.assertEqual(await app.services.billing.task_reservation_status(
                    workspace.id, task.id), "released")
                self.assertEqual((await app.services.billing.balances(ctx))[0].available,
                                 100000)
                self.assertEqual((await app.services.versions.head(ctx, project.id)).revision, 0)
                history = await app.services.conversations.history_recent(
                    ctx, project.id, PageQuery(limit=10))
                self.assertEqual([item.role for item in history.items], [MessageRole.USER])

        asyncio.run(run())

    def test_answer_checkpoint_recovers_before_or_after_billing_settlement(self):
        async def run(settled_before_crash: bool):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                settings = Settings(database_url=f"sqlite:///{root / 'db.sqlite'}",
                    blob_directory=root / "blobs", public_origin="http://localhost:5173",
                    preview_origin="http://preview.localhost:8001", model_provider="stub",
                    model_name="stub", model_secret_name="", task_timeout_seconds=180,
                    initial_build_credits=100000)
                secret = Fernet.generate_key().decode()
                with patch.dict(os.environ, {"ATOMS_SECRET_ENCRYPTION_KEY": secret}):
                    app = compose_application(settings)
                actor = EntityId("recover-answer-owner")
                creation = RequestContext(actor_id=actor, session_id=EntityId("answer-session"),
                                          workspace_id=None, request_id="answer-recovery")
                workspace = await app.services.workspaces.create(creation, "Personal",
                    Mutation(idempotency_key="answer-workspace"))
                ctx = replace(creation, workspace_id=workspace.id)
                project = await app.services.projects.create(ctx, CreateProject(
                    name="Question", visibility=Visibility.PRIVATE,
                    mutation=Mutation(idempotency_key="answer-project")))
                model = AnswerModel()
                app.services.generation.dependencies = replace(
                    app.services.generation.dependencies, model=model, model_configured=True)
                app.services.builds.dependencies = replace(
                    app.services.builds.dependencies, builder=NoBuild())
                app.generation.pricing = FixedPricing()
                task = await app.generation.submit(ctx, GenerateApplication(
                    project_id=project.id, prompt="如何规划一个求职管理网页？",
                    attachment_ids=(), model="stub", agent_mode="answer",
                    expected_head_revision=0,
                    mutation=Mutation(idempotency_key="recover-answer")))
                self.assertEqual(await app.queue.lease("crashed-worker"), task.id)
                crash_target = (app.services.tasks if settled_before_crash
                                else app.services.billing)
                crash_method = "complete_answer" if settled_before_crash else "settle"
                with patch.object(crash_target, crash_method,
                                  side_effect=SystemExit("simulated process crash")):
                    with self.assertRaises(SystemExit):
                        await app.generation.run(ctx, task.id, lease_owner="crashed-worker")
                self.assertEqual(model.calls, 1)
                self.assertIsNotNone(await app.services.tasks.answer_checkpoint(ctx, task.id))
                self.assertEqual((await app.services.tasks.get(ctx, task.id)).state,
                                 TaskState.GENERATING)
                self.assertEqual(await app.services.billing.task_reservation_status(
                    workspace.id, task.id),
                    "settled" if settled_before_crash else "reserved")
                db = sqlite3.connect(root / "db.sqlite")
                try:
                    db.execute("UPDATE jobs SET leased_until=? WHERE task_id=?",
                               ("2000-01-01T00:00:00+00:00", str(task.id)))
                    db.commit()
                finally:
                    db.close()

                with patch.dict(os.environ, {"ATOMS_SECRET_ENCRYPTION_KEY": secret}):
                    restarted = compose_application(settings)
                restarted.services.generation.dependencies = replace(
                    restarted.services.generation.dependencies, model=model,
                    model_configured=True)
                restarted.services.builds.dependencies = replace(
                    restarted.services.builds.dependencies, builder=NoBuild())
                restarted.generation.pricing = FixedPricing()
                await recover(restarted)
                completed = await restarted.services.tasks.get(ctx, task.id)
                self.assertEqual(completed.state, TaskState.SUCCEEDED)
                self.assertIsNone(completed.result_version_id)
                self.assertEqual(await restarted.services.billing.task_reservation_status(
                    workspace.id, task.id), "settled")
                self.assertEqual((await restarted.services.billing.balances(ctx))[0].available,
                                 99990)
                self.assertEqual((await restarted.services.versions.head(ctx, project.id)).revision,
                                 0)
                messages = await restarted.services.conversations.history_recent(
                    ctx, project.id, PageQuery(limit=10))
                self.assertEqual([item.role for item in messages.items],
                                 [MessageRole.USER, MessageRole.ASSISTANT])
                self.assertEqual(model.calls, 1)
                self.assertIn("succeeded", [event.kind for event in
                    await restarted.services.tasks.events(ctx, task.id, 0)])
                await recover(restarted)
                self.assertTrue(await run_once(restarted, "after-recovery-worker", 60))
                messages = await restarted.services.conversations.history_recent(
                    ctx, project.id, PageQuery(limit=10))
                self.assertEqual([item.role for item in messages.items],
                                 [MessageRole.USER, MessageRole.ASSISTANT])
                self.assertEqual(model.calls, 1)

        for settled_before_crash in (False, True):
            with self.subTest(settled_before_crash=settled_before_crash):
                asyncio.run(run(settled_before_crash))

    def test_answer_checkpoint_survives_transient_settle_or_completion_error(self):
        async def run(failed_step: str):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                settings = Settings(database_url=f"sqlite:///{root / 'db.sqlite'}",
                    blob_directory=root / "blobs", public_origin="http://localhost:5173",
                    preview_origin="http://preview.localhost:8001", model_provider="stub",
                    model_name="stub", model_secret_name="", task_timeout_seconds=180,
                    initial_build_credits=100000)
                with patch.dict(os.environ, {"ATOMS_SECRET_ENCRYPTION_KEY":
                                                 Fernet.generate_key().decode()}):
                    app = compose_application(settings)
                actor = EntityId("transient-answer-owner")
                creation = RequestContext(actor_id=actor, session_id=EntityId("answer-session"),
                                          workspace_id=None, request_id="answer-transient")
                workspace = await app.services.workspaces.create(creation, "Personal",
                    Mutation(idempotency_key="answer-workspace"))
                ctx = replace(creation, workspace_id=workspace.id)
                project = await app.services.projects.create(ctx, CreateProject(
                    name="Question", visibility=Visibility.PRIVATE,
                    mutation=Mutation(idempotency_key="answer-project")))
                model = AnswerModel()
                app.services.generation.dependencies = replace(
                    app.services.generation.dependencies, model=model, model_configured=True)
                app.services.builds.dependencies = replace(
                    app.services.builds.dependencies, builder=NoBuild())
                app.generation.pricing = FixedPricing()
                task = await app.generation.submit(ctx, GenerateApplication(
                    project_id=project.id, prompt="如何安排任务？", attachment_ids=(),
                    model="stub", agent_mode="answer", expected_head_revision=0,
                    mutation=Mutation(idempotency_key="transient-answer")))
                if failed_step == "settle_until_recovery":
                    with patch.object(app.services.billing, "settle", side_effect=DomainError(
                            ErrorCode.PROVIDER_UNAVAILABLE, "Billing temporarily unavailable")):
                        with self.assertRaises(DomainError):
                            await run_once(app, "transient-worker", 60)
                    self.assertEqual((await app.services.tasks.get(ctx, task.id)).state,
                                     TaskState.INTERRUPTED)
                    self.assertEqual(await app.services.billing.task_reservation_status(
                        workspace.id, task.id), "reserved")
                    db = sqlite3.connect(root / "db.sqlite")
                    try:
                        db.execute("UPDATE jobs SET leased_until=? WHERE task_id=?",
                                   ("2000-01-01T00:00:00+00:00", str(task.id)))
                        db.commit()
                    finally:
                        db.close()
                    await recover(app)
                else:
                    target = (app.services.billing if failed_step == "settle"
                              else app.services.tasks)
                    method = "settle" if failed_step == "settle" else "complete_answer"
                    original = getattr(target, method)
                    attempts = 0

                    async def fail_once(*args, **kwargs):
                        nonlocal attempts
                        attempts += 1
                        if attempts == 1:
                            raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE,
                                              "Simulated transient persistence error")
                        return await original(*args, **kwargs)

                    with patch.object(target, method, side_effect=fail_once):
                        self.assertTrue(await run_once(app, "transient-worker", 60))
                    self.assertEqual(attempts, 2 if failed_step == "settle" else 1)
                self.assertEqual((await app.services.tasks.get(ctx, task.id)).state,
                                 TaskState.SUCCEEDED)
                self.assertEqual(await app.services.billing.task_reservation_status(
                    workspace.id, task.id), "settled")
                self.assertEqual((await app.services.versions.head(ctx, project.id)).revision,
                                 0)
                messages = await app.services.conversations.history_recent(
                    ctx, project.id, PageQuery(limit=10))
                self.assertEqual([item.role for item in messages.items],
                                 [MessageRole.USER, MessageRole.ASSISTANT])
                self.assertEqual(model.calls, 1)

        for failed_step in ("settle", "complete_answer", "settle_until_recovery"):
            with self.subTest(failed_step=failed_step):
                asyncio.run(run(failed_step))


if __name__ == "__main__":
    unittest.main()
