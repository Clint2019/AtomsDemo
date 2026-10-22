"""T05 workflow with explicitly injected model/build/pricing/billing substitutes."""
import asyncio
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from atoms_demo.adapters.authorization import membership_id
from atoms_demo.bootstrap import compose_application
from atoms_demo.config import Settings
from atoms_demo.config import database_path
from atoms_demo.contracts.billing import AccountKind, UsageReservation
from atoms_demo.contracts.builds import BuildResult, ScanResult
from atoms_demo.contracts.conversations import MessageRole
from atoms_demo.contracts.generation import FileChangeSet, SourceFile
from atoms_demo.contracts.projects import CreateProject, Visibility
from atoms_demo.contracts.tasks import TaskState, TaskTransition
from atoms_demo.contracts.workspaces import Membership, Role, Workspace
from atoms_demo.core.types import EntityId, Mutation, PageQuery, RequestContext, Scope
from atoms_demo.core.errors import DomainError, ErrorCode, UnsentModelError
from atoms_demo.workflows.generation import GenerateApplication
from atoms_demo.api.task_worker import recover, run_once
from atoms_demo.adapters.persistence import connect


class PriceStub:
    async def reserve(self, model, prompt):
        return 100

    async def charge(self, model, input_tokens, output_tokens):
        return 10


class BillingStub:
    def __init__(self):
        self.settled = []
        self.released = []

    async def reserve(self, ctx, task_id, maximum_units, mutation, project_id=None):
        return UsageReservation(id=task_id, task_id=task_id, maximum_units=maximum_units,
                                account=AccountKind.BUILD_CREDITS)

    async def settle(self, ctx, charge):
        self.settled.append(charge)

    async def release(self, ctx, reservation_id):
        self.released.append(reservation_id)

    async def release_interrupted(self, workspace_id, task_id):
        pass

    async def release_task(self, ctx, task_id):
        pass

    async def settle_interrupted(self, workspace_id, task_id, consumed_units):
        pass

    async def task_reservation_status(self, workspace_id, task_id):
        return "reserved"


class ModelStub:
    def __init__(self):
        self.repair_files = []

    async def generate(self, command):
        return FileChangeSet(summary="generated", upserts=(SourceFile(path="index.html",
            content="<h1>hello</h1>"),), deleted_paths=(), input_tokens=5, output_tokens=5)

    async def repair(self, command):
        self.repair_files.append(command.generation.files)
        return FileChangeSet(summary="repair", upserts=(SourceFile(path="index.html",
            content=f"<h1>repair {command.repair_number}</h1>"),), deleted_paths=(),
            input_tokens=5, output_tokens=5)


class BuildStub:
    def __init__(self, blobs, succeed, before_return=None):
        self.blobs = blobs
        self.succeed = succeed
        self.build_count = 0
        self.before_return = before_return

    async def build(self, command):
        self.build_count += 1
        if self.before_return:
            await self.before_return(command)
        artifact = await self.blobs.put(b"built", "application/zip") if self.succeed else None
        return BuildResult(version_id=command.version_id, success=self.succeed,
                           artifact=artifact, diagnostics=("failed",) if not self.succeed else (), log=None)

    async def scan(self, version_id, artifact):
        return ScanResult(version_id=version_id, passed=True, findings=())


class WorkflowTests(unittest.TestCase):
    def test_approval_waits_recovers_and_uses_selected_tasks(self):
        class CapturingModel(ModelStub):
            def __init__(self):
                super().__init__()
                self.prompts = []

            async def generate(self, command):
                self.prompts.append(command.prompt)
                return await super().generate(command)

        class ApprovalBilling(BillingStub):
            async def release_task(self, ctx, task_id):
                self.released.append(task_id)

        async def run():
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                settings = Settings(database_url=f"sqlite:///{root / 'db.sqlite'}", blob_directory=root / "blobs",
                    public_origin="http://localhost:5173", preview_origin="http://localhost:8001",
                    model_provider="stub", model_secret_name="", task_timeout_seconds=180,
                    model_name="stub")
                secret_key = Fernet.generate_key().decode()
                with patch.dict(os.environ, {"ATOMS_SECRET_ENCRYPTION_KEY": secret_key}):
                    app = compose_application(settings)
                workspace, owner = EntityId("approval-workspace"), EntityId("approval-owner")
                ctx = RequestContext(actor_id=owner, session_id=EntityId("approval-session"),
                                     workspace_id=workspace, request_id="approval-test")
                async with app.services.tasks.dependencies.uow() as uow:
                    await uow.repository(Workspace).save(Scope(workspace_id=workspace), Workspace(
                        id=workspace, name="Team", revision=0), None)
                    await uow.repository(Membership).save(Scope(workspace_id=workspace), Membership(
                        id=membership_id(workspace, owner), workspace_id=workspace,
                        user_id=owner, role=Role.OWNER, revision=0), None)
                    await uow.commit()
                project = await app.services.projects.create(ctx, CreateProject(
                    name="Approval", visibility=Visibility.PRIVATE,
                    mutation=Mutation(idempotency_key="approval-project")))
                model = CapturingModel()
                app.services.generation.dependencies = replace(app.services.generation.dependencies,
                    model=model, model_configured=True)
                app.services.builds.dependencies = replace(app.services.builds.dependencies,
                    builder=BuildStub(app.services.versions.dependencies.blobs, True))
                billing = ApprovalBilling()
                app.generation.services = replace(app.generation.services, billing=billing)
                app = replace(app, services=replace(app.services, billing=billing))
                app.generation.pricing = PriceStub()
                command = GenerateApplication(project_id=project.id,
                    prompt="Build a landing page；Add a customer list", attachment_ids=(),
                    model="stub", agent_mode="approval", expected_head_revision=0,
                    mutation=Mutation(idempotency_key="approval-task"))
                task = await app.generation.submit(ctx, command)
                self.assertEqual(task.state, TaskState.WAITING_FOR_APPROVAL)
                plan = await app.services.tasks.approval(ctx, task.id)
                self.assertEqual(plan.items, ("Build a landing page", "Add a customer list"))
                history = await app.services.conversations.history_recent(
                    ctx, project.id, PageQuery(limit=100))
                self.assertEqual([message.role for message in history.items],
                    [MessageRole.USER, MessageRole.ASSISTANT])
                self.assertIn("Add a customer list", history.items[1].content)
                with connect(database_path(settings)) as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE task_id=?",
                        (str(task.id),)).fetchone()[0], 0)
                self.assertFalse(await run_once(app, "approval-worker", 60))
                self.assertEqual(model.prompts, [])
                with patch.dict(os.environ, {"ATOMS_SECRET_ENCRYPTION_KEY": secret_key}):
                    restarted = compose_application(settings)
                restarted.services.generation.dependencies = replace(
                    restarted.services.generation.dependencies, model=model, model_configured=True)
                restarted.services.builds.dependencies = replace(restarted.services.builds.dependencies,
                    builder=BuildStub(restarted.services.versions.dependencies.blobs, True))
                restarted.generation.services = replace(restarted.generation.services, billing=billing)
                app = replace(restarted, services=replace(restarted.services, billing=billing))
                app.generation.pricing = PriceStub()
                await app.services.tasks.recover_interrupted()
                self.assertEqual((await app.services.tasks.get(ctx, task.id)).state,
                                 TaskState.WAITING_FOR_APPROVAL)
                self.assertEqual((await app.services.tasks.approval(ctx, task.id)).items,
                                 ("Build a landing page", "Add a customer list"))
                self.assertFalse(await run_once(app, "approval-worker-after-restart", 60))
                self.assertEqual(model.prompts, [])
                with self.assertRaises(DomainError):
                    await app.services.tasks.approve(ctx, task.id, (2,), "", Mutation(idempotency_key="bad"))
                queued = await app.services.tasks.approve(ctx, task.id, (1,), "Use local data",
                    Mutation(idempotency_key="approved"))
                self.assertEqual(queued.state, TaskState.QUEUED)
                with connect(database_path(settings)) as db:
                    db.execute("DELETE FROM jobs WHERE task_id=?", (str(task.id),))
                    db.commit()
                self.assertEqual((await app.services.tasks.approve(ctx, task.id, (1,), "Use local data",
                    Mutation(idempotency_key="approved"))).id, task.id)
                history = await app.services.conversations.history_recent(
                    ctx, project.id, PageQuery(limit=100))
                self.assertEqual([message.role for message in history.items],
                    [MessageRole.USER, MessageRole.ASSISTANT, MessageRole.USER])
                self.assertIn("Add a customer list", history.items[-1].content)
                self.assertIn("Use local data", history.items[-1].content)
                self.assertNotIn("Build a landing page", history.items[-1].content)
                self.assertTrue(await run_once(app, "approval-worker", 60))
                self.assertEqual((await app.services.tasks.get(ctx, task.id)).state, TaskState.SUCCEEDED)
                with patch.dict(os.environ, {"ATOMS_SECRET_ENCRYPTION_KEY": secret_key}):
                    refreshed = compose_application(settings)
                persisted = await refreshed.services.conversations.history_recent(
                    ctx, project.id, PageQuery(limit=100))
                self.assertEqual([message.role for message in persisted.items],
                    [MessageRole.USER, MessageRole.ASSISTANT,
                     MessageRole.USER, MessageRole.ASSISTANT])
                self.assertEqual(len({message.id for message in persisted.items}), 4)
                self.assertIn("Add a customer list", model.prompts[0])
                self.assertIn("Use local data", model.prompts[0])
                self.assertNotIn("Build a landing page", model.prompts[0])
                with self.assertRaises(DomainError):
                    await app.services.tasks.approve(ctx, task.id, (0,), "",
                        Mutation(idempotency_key="different"))
                head = await app.services.versions.head(ctx, project.id)
                second = await app.generation.submit(ctx, replace(command,
                    prompt="Wait for review", expected_head_revision=head.revision,
                    mutation=Mutation(idempotency_key="approval-cancel")))
                cancelled = await app.generation.cancel(ctx, second.id,
                    Mutation(idempotency_key="cancel-waiting"))
                self.assertEqual(cancelled.state, TaskState.CANCELLED)
                self.assertIn(second.id, billing.released)
                self.assertFalse(await run_once(app, "approval-worker", 60))
                with self.assertRaises(DomainError):
                    await app.services.tasks.approve(ctx, second.id, (0,), "",
                        Mutation(idempotency_key="approve-cancelled"))
                third = await app.generation.submit(ctx, replace(command,
                    prompt="Another review", expected_head_revision=head.revision,
                    mutation=Mutation(idempotency_key="approval-cancel-queued")))
                await app.services.tasks.approve(ctx, third.id, (0,), "",
                    Mutation(idempotency_key="approve-third"))
                self.assertEqual((await app.generation.cancel(ctx, third.id,
                    Mutation(idempotency_key="cancel-queued"))).state, TaskState.CANCELLED)
                self.assertIn(third.id, billing.released)
                self.assertEqual((await app.generation.cancel(ctx, third.id,
                    Mutation(idempotency_key="cancel-queued"))).state, TaskState.CANCELLED)
                self.assertEqual(billing.released.count(third.id), 1)
                self.assertTrue(await run_once(app, "cancelled-task-worker", 60))
                self.assertEqual(len(model.prompts), 1)

                fourth = await app.generation.submit(ctx, replace(command,
                    agent_mode="default", prompt="Build and report the result",
                    expected_head_revision=head.revision,
                    mutation=Mutation(idempotency_key="reply-failure")))
                append = app.services.conversations.append

                async def fail_reply(reply_ctx, message):
                    if message.role == MessageRole.ASSISTANT and message.task_id == fourth.id:
                        raise RuntimeError("conversation store unavailable")
                    return await append(reply_ctx, message)

                with patch.object(app.services.conversations, "append", side_effect=fail_reply):
                    with self.assertRaisesRegex(RuntimeError, "conversation store unavailable"):
                        await run_once(app, "reply-worker", 60)
                self.assertEqual((await app.services.tasks.get(ctx, fourth.id)).state,
                                 TaskState.COMMITTING)
                before_reply = await app.services.conversations.history_recent(
                    ctx, project.id, PageQuery(limit=100))
                self.assertEqual(len([message for message in before_reply.items
                                      if message.task_id == fourth.id]), 0)
                with connect(database_path(settings)) as db:
                    db.execute("UPDATE jobs SET leased_until=? WHERE task_id=?",
                        ("2000-01-01T00:00:00+00:00", str(fourth.id)))
                    db.commit()
                self.assertTrue(await run_once(app, "reply-retry-worker", 60))
                self.assertEqual((await app.services.tasks.get(ctx, fourth.id)).state,
                                 TaskState.SUCCEEDED)
                after_reply = await app.services.conversations.history_recent(
                    ctx, project.id, PageQuery(limit=100))
                self.assertEqual(len([message for message in after_reply.items
                                      if message.task_id == fourth.id and
                                      message.role == MessageRole.ASSISTANT]), 1)

                recovery_head = await app.services.versions.head(ctx, project.id)
                recovery_task = await app.generation.submit(ctx, replace(command,
                    agent_mode="default", prompt="Recover the saved reply",
                    expected_head_revision=recovery_head.revision,
                    mutation=Mutation(idempotency_key="reply-recovery")))

                async def fail_recovery_reply(reply_ctx, message):
                    if message.role == MessageRole.ASSISTANT and message.task_id == recovery_task.id:
                        raise RuntimeError("conversation store unavailable")
                    return await append(reply_ctx, message)

                with patch.object(app.services.conversations, "append",
                                  side_effect=fail_recovery_reply):
                    with self.assertRaisesRegex(RuntimeError, "conversation store unavailable"):
                        await run_once(app, "recovery-worker", 60)
                self.assertEqual((await app.services.tasks.get(ctx, recovery_task.id)).state,
                                 TaskState.COMMITTING)
                with connect(database_path(settings)) as db:
                    db.execute("UPDATE jobs SET leased_until=? WHERE task_id=?",
                        ("2000-01-01T00:00:00+00:00", str(recovery_task.id)))
                    db.commit()
                with patch.dict(os.environ, {"ATOMS_SECRET_ENCRYPTION_KEY": secret_key}):
                    recovered_app = compose_application(settings)
                await recover(recovered_app)
                self.assertEqual((await recovered_app.services.tasks.get(ctx, recovery_task.id)).state,
                                 TaskState.SUCCEEDED)
                recovered_history = await recovered_app.services.conversations.history_recent(
                    ctx, project.id, PageQuery(limit=100))
                self.assertEqual(len([message for message in recovered_history.items
                                      if message.task_id == recovery_task.id and
                                      message.role == MessageRole.ASSISTANT]), 1)
                new_head = await app.services.versions.head(ctx, project.id)
                fifth = await app.generation.submit(ctx, replace(command,
                    prompt="Check cancellation accounting",
                    expected_head_revision=new_head.revision,
                    mutation=Mutation(idempotency_key="cancel-generating")))
                queued_fifth = await app.services.tasks.approve(ctx, fifth.id, (0,), "",
                    Mutation(idempotency_key="approve-fifth"))
                await app.services.tasks.transition(ctx, TaskTransition(
                    task_id=fifth.id, expected_revision=queued_fifth.revision,
                    target=TaskState.GENERATING))
                await app.generation.cancel(ctx, fifth.id,
                    Mutation(idempotency_key="cancel-after-start"))
                self.assertNotIn(fifth.id, billing.released)

        asyncio.run(run())

    def test_success_and_two_repairs_limit(self):
        async def run():
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                settings = Settings(database_url=f"sqlite:///{root / 'db.sqlite'}", blob_directory=root / "blobs",
                    public_origin="http://localhost:5173", preview_origin="http://localhost:8001",
                    model_provider="stub", model_secret_name="", task_timeout_seconds=180,
                    model_name="stub")
                with patch.dict(os.environ, {"ATOMS_SECRET_ENCRYPTION_KEY": Fernet.generate_key().decode()}):
                    app = compose_application(settings)
                workspace, owner = EntityId("workspace-a"), EntityId("owner-a")
                ctx = RequestContext(actor_id=owner, session_id=EntityId("session-a"),
                                     workspace_id=workspace, request_id="r")
                async with app.services.tasks.dependencies.uow() as uow:
                    await uow.repository(Workspace).save(Scope(workspace_id=workspace), Workspace(
                        id=workspace, name="Team", revision=0), None)
                    await uow.repository(Membership).save(Scope(workspace_id=workspace), Membership(
                        id=membership_id(workspace, owner), workspace_id=workspace,
                        user_id=owner, role=Role.OWNER, revision=0), None)
                    await uow.commit()
                project = await app.services.projects.create(ctx, CreateProject(
                    name="App", visibility=Visibility.PRIVATE, mutation=Mutation(idempotency_key="project")))
                model = app.services.generation
                model_stub = ModelStub()
                model.dependencies = replace(model.dependencies, model=model_stub, model_configured=True)
                builder = app.services.builds
                build_stub = BuildStub(app.services.versions.dependencies.blobs, True)
                builder.dependencies = replace(builder.dependencies, builder=build_stub)
                billing = BillingStub()
                app.generation.services = replace(app.generation.services, billing=billing)
                app = replace(app, services=replace(app.services, billing=billing))
                app.generation.pricing = PriceStub()
                command = GenerateApplication(project_id=project.id, prompt="make a page",
                    attachment_ids=(), model="stub", agent_mode="normal", expected_head_revision=0,
                    mutation=Mutation(idempotency_key="generate-1"))
                task = await app.generation.submit(ctx, command)
                self.assertTrue(await run_once(app, "worker-test", 60))
                completed = await app.services.tasks.get(ctx, task.id)
                self.assertEqual(completed.state, TaskState.SUCCEEDED,
                                 f"error={completed.error_code}, builds={build_stub.build_count}")
                self.assertEqual(build_stub.build_count, 1)
                self.assertEqual(len(billing.settled), 1)
                head = await app.services.versions.head(ctx, project.id)
                self.assertEqual(head.version_id, completed.result_version_id)
                self.assertEqual(await app.services.versions.read_file(ctx, head.version_id, "index.html"),
                                 "<h1>hello</h1>")
                conversation = await app.services.conversations.history_recent(
                    ctx, project.id, PageQuery(limit=10))
                self.assertEqual([item.role for item in conversation.items],
                                 [MessageRole.USER, MessageRole.ASSISTANT])
                self.assertEqual(conversation.items[-1].task_id, task.id)
                self.assertEqual((await app.generation.submit(ctx, command)).id, task.id)
                build_stub = BuildStub(app.services.versions.dependencies.blobs, False)
                builder.dependencies = replace(builder.dependencies, builder=build_stub)
                failed_task = await app.generation.submit(ctx, replace(command,
                    prompt="change page", expected_head_revision=head.revision,
                    mutation=Mutation(idempotency_key="generate-2")))
                self.assertTrue(await run_once(app, "worker-test", 60))
                failed = await app.services.tasks.get(ctx, failed_task.id)
                self.assertEqual(failed.state, TaskState.FAILED)
                self.assertEqual(build_stub.build_count, 3)
                self.assertIn("hello", model_stub.repair_files[0][0].content)
                self.assertIn("repair 1", model_stub.repair_files[1][0].content)
                self.assertEqual((await app.services.versions.head(ctx, project.id)).version_id, head.version_id)
                class RejectBeforeDispatch:
                    async def generate(self, command):
                        raise UnsentModelError(ErrorCode.INVALID_INPUT,
                                               "Attachment type is unsupported")

                    async def repair(self, command):
                        raise AssertionError("No repair should run")

                model.dependencies = replace(model.dependencies, model=RejectBeforeDispatch())
                rejected_task = await app.generation.submit(ctx, replace(command,
                    prompt="unsupported attachment", expected_head_revision=head.revision,
                    mutation=Mutation(idempotency_key="preflight-rejected")))
                self.assertTrue(await run_once(app, "worker-test", 60))
                rejected = await app.services.tasks.get(ctx, rejected_task.id)
                self.assertEqual((rejected.state, rejected.error_code),
                                 (TaskState.FAILED, ErrorCode.INVALID_INPUT.value))
                self.assertFalse(await app.services.tasks.has_uncheckpointed_model_call(rejected.id))
                self.assertIn(rejected.id, billing.released)
                model.dependencies = replace(model.dependencies, model=model_stub)
                async def cancel_during_build(command):
                    await app.services.tasks.cancel(ctx, command.task_id,
                        Mutation(idempotency_key="cancel-during-build"))
                build_stub = BuildStub(app.services.versions.dependencies.blobs, True,
                                       cancel_during_build)
                builder.dependencies = replace(builder.dependencies, builder=build_stub)
                cancelled_task = await app.generation.submit(ctx, replace(command,
                    prompt="another change", expected_head_revision=head.revision,
                    mutation=Mutation(idempotency_key="generate-3")))
                self.assertTrue(await run_once(app, "worker-test", 60))
                cancelled = await app.services.tasks.get(ctx, cancelled_task.id)
                self.assertEqual(cancelled.state, TaskState.CANCELLED)
                self.assertEqual((await app.services.versions.head(ctx, project.id)).version_id,
                                 head.version_id)
                orphan = EntityId("orphan-job")
                await app.queue.enqueue(orphan, "orphan-job")
                with self.assertRaises(DomainError):
                    await run_once(app, "worker-test", 60)
                with connect(database_path(settings)) as db:
                    row = db.execute("SELECT state FROM jobs WHERE task_id=?", (str(orphan),)).fetchone()
                self.assertEqual(row["state"], "leased")
                build_stub = BuildStub(app.services.versions.dependencies.blobs, True)
                builder.dependencies = replace(builder.dependencies, builder=build_stub)
                original_promote = app.services.versions.promote
                async def interrupt_before_promote(commit_ctx, promotion):
                    await app.services.tasks.interrupt_execution(promotion.task_id,
                        "worker_unavailable")
                    return await original_promote(commit_ctx, promotion)
                app.services.versions.promote = interrupt_before_promote
                raced_task = await app.generation.submit(ctx, replace(command,
                    prompt="race with old worker", expected_head_revision=head.revision,
                    mutation=Mutation(idempotency_key="generate-4")))
                self.assertTrue(await run_once(app, "worker-test", 60))
                self.assertEqual((await app.services.tasks.get(ctx, raced_task.id)).state,
                                 TaskState.INTERRUPTED)
                self.assertEqual((await app.services.versions.head(ctx, project.id)).version_id,
                                 head.version_id)
        asyncio.run(run())
