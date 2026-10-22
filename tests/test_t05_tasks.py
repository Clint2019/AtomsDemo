"""Durable task lifecycle and project concurrency guard."""
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
from atoms_demo.contracts.projects import CreateProject, Visibility
from atoms_demo.contracts.versions import CreateVersion, EditVersion, FileEdit, FileEntry, PromoteVersion
from atoms_demo.contracts.tasks import SubmitTask, TaskState, TaskTransition
from atoms_demo.contracts.builds import BuildInput, BuildResult, ScanResult
from atoms_demo.contracts.billing import AccountKind
from atoms_demo.contracts.workspaces import Membership, Role, Workspace
from atoms_demo.core.errors import DomainError, ErrorCode
from atoms_demo.core.events import TaskEvent
from atoms_demo.core.types import EntityId, Mutation, PageQuery, RequestContext, Scope
from atoms_demo.services.billing import LedgerRecord
from atoms_demo.api.task_worker import recover, run_once
from atoms_demo.adapters.persistence import connect


class TaskTests(unittest.TestCase):
    def test_manual_edit_recovers_queued_and_expires_building_lease(self):
        class SuccessfulBuilder:
            def __init__(self, blobs):
                self.blobs = blobs
                self.build_count = 0

            async def build(self, command):
                self.build_count += 1
                artifact = await self.blobs.put(b"built", "application/zip")
                return BuildResult(version_id=command.version_id, success=True,
                                   artifact=artifact, diagnostics=(), log=None)

            async def scan(self, version_id, artifact):
                return ScanResult(version_id=version_id, passed=True, findings=())

        async def run():
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                settings = Settings(database_url=f"sqlite:///{root / 'db.sqlite'}",
                    blob_directory=root / "blobs", public_origin="http://localhost:5173",
                    preview_origin="http://localhost:8001", model_provider="",
                    model_secret_name="", task_timeout_seconds=180)
                key = Fernet.generate_key().decode()

                def boot():
                    with patch.dict(os.environ, {"ATOMS_SECRET_ENCRYPTION_KEY": key}):
                        application = compose_application(settings)
                    builder = SuccessfulBuilder(application.services.versions.dependencies.blobs)
                    application.services.builds.dependencies = replace(
                        application.services.builds.dependencies, builder=builder)
                    return application, builder

                app, _ = boot()
                workspace, owner = EntityId("workspace-manual"), EntityId("owner-manual")
                ctx = RequestContext(actor_id=owner, session_id=EntityId("session-manual"),
                                     workspace_id=workspace, request_id="manual-recovery")
                async with app.services.tasks.dependencies.uow() as uow:
                    await uow.repository(Workspace).save(Scope(workspace_id=workspace), Workspace(
                        id=workspace, name="Team", revision=0), None)
                    await uow.repository(Membership).save(Scope(workspace_id=workspace), Membership(
                        id=membership_id(workspace, owner), workspace_id=workspace,
                        user_id=owner, role=Role.OWNER, revision=0), None)
                    await uow.commit()
                project = await app.services.projects.create(ctx, CreateProject(
                    name="Manual recovery", visibility=Visibility.PRIVATE,
                    mutation=Mutation(idempotency_key="manual-project")))
                source = await app.services.versions.dependencies.blobs.put(
                    b"<h1>base</h1>", "text/html")
                base = await app.services.versions.create_candidate(ctx, CreateVersion(
                    project_id=project.id, base_version_id=None,
                    files=(FileEntry(path="index.html", blob=source),), summary="base",
                    mutation=Mutation(idempotency_key="manual-base")))
                artifact = await app.services.versions.dependencies.blobs.put(
                    b"seed", "application/zip")
                await app.services.versions.record_build(ctx, base.id, artifact, True)
                initial = await app.services.versions.promote(ctx, PromoteVersion(
                    project_id=project.id, version_id=base.id, expected_head_revision=0,
                    task_id=None, mutation=Mutation(idempotency_key="manual-base-head")))

                first_command = EditVersion(project_id=project.id, base_version_id=base.id,
                    edits=(FileEdit(path="index.html", content="<h1>after restart</h1>"),),
                    summary="queued edit", mutation=Mutation(idempotency_key="manual-queued"))
                queued = await app.generation.edit(ctx, first_command)
                self.assertEqual(queued.state, TaskState.QUEUED)
                restarted, builder = boot()
                await recover(restarted)
                self.assertEqual((await restarted.services.tasks.get(ctx, queued.id)).state,
                                 TaskState.QUEUED)
                self.assertTrue(await run_once(restarted, "queued-worker", 60))
                completed = await restarted.services.tasks.get(ctx, queued.id)
                head = await restarted.services.versions.head(ctx, project.id)
                self.assertEqual((completed.state, head.revision, builder.build_count),
                                 (TaskState.SUCCEEDED, initial.revision + 1, 1))
                self.assertEqual(head.version_id, completed.result_version_id)
                self.assertEqual(await restarted.services.versions.read_file(
                    ctx, head.version_id, "index.html"), "<h1>after restart</h1>")
                self.assertEqual(await restarted.services.billing.task_reservation_status(
                    workspace, queued.id), "settled")
                self.assertEqual((await restarted.generation.edit(ctx, first_command)).id,
                                 queued.id)
                self.assertEqual((await restarted.services.versions.head(ctx, project.id)), head)
                first_history = await restarted.services.versions.list_versions(
                    ctx, project.id, PageQuery(limit=100))
                self.assertEqual(len(first_history.items), 2)

                second_command = EditVersion(project_id=project.id,
                    base_version_id=head.version_id,
                    edits=(FileEdit(path="index.html", content="<h1>interrupted</h1>"),),
                    summary="building edit", mutation=Mutation(idempotency_key="manual-building"))
                building = await restarted.generation.edit(ctx, second_command)
                self.assertEqual(await restarted.queue.lease("old-worker"), building.id)
                building = await restarted.services.tasks.transition(ctx, TaskTransition(
                    task_id=building.id, expected_revision=building.revision,
                    target=TaskState.BUILDING))
                live_restarted, live_builder = boot()
                await recover(live_restarted)
                self.assertEqual((await live_restarted.services.tasks.get(ctx, building.id)).state,
                                 TaskState.BUILDING)
                self.assertEqual(await live_restarted.services.billing.task_reservation_status(
                    workspace, building.id), "reserved")
                self.assertEqual(live_builder.build_count, 0)
                with connect(root / "db.sqlite") as db:
                    db.execute("UPDATE jobs SET leased_until=? WHERE task_id=?",
                               ("2000-01-01T00:00:00+00:00", str(building.id)))
                restarted_again, next_builder = boot()
                await recover(restarted_again)
                interrupted = await restarted_again.services.tasks.get(ctx, building.id)
                self.assertEqual((interrupted.state, interrupted.error_code),
                                 (TaskState.INTERRUPTED, "process_restart"))
                self.assertIsNone(await restarted_again.services.tasks.active_for_project(
                    ctx, project.id))
                self.assertEqual(await restarted_again.services.billing.task_reservation_status(
                    workspace, building.id), "released")
                self.assertEqual(await restarted_again.services.versions.head(ctx, project.id),
                                 head)
                self.assertEqual(next_builder.build_count, 0)
                self.assertEqual((await restarted_again.generation.edit(ctx, second_command)).id,
                                 building.id)
                self.assertEqual(await restarted_again.services.versions.head(ctx, project.id),
                                 head)
                second_history = await restarted_again.services.versions.list_versions(
                    ctx, project.id, PageQuery(limit=100))
                self.assertEqual(len(second_history.items), 3)
                self.assertTrue(await run_once(restarted_again, "cleanup-worker", 60))
                self.assertEqual(next_builder.build_count, 0)
                self.assertEqual((await restarted_again.services.tasks.get(ctx, building.id)).state,
                                 TaskState.INTERRUPTED)
                self.assertEqual(await restarted_again.services.versions.head(ctx, project.id),
                                 head)
                self.assertIsNone(await restarted_again.services.tasks.active_for_project(
                    ctx, project.id))
        asyncio.run(run())

    def test_build_result_rejects_non_boolean_status(self):
        class BadExecutor:
            async def build(self, command):
                return BuildResult(version_id=command.version_id, success="false",
                                   artifact=None, diagnostics=(), log=None)

            async def scan(self, version_id, artifact):
                return ScanResult(version_id=version_id, passed="false", findings=())

        async def run():
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                settings = Settings(database_url=f"sqlite:///{root / 'db.sqlite'}", blob_directory=root / "blobs",
                    public_origin="http://localhost:5173", preview_origin="http://localhost:8001",
                    model_provider="", model_secret_name="", task_timeout_seconds=180)
                with patch.dict(os.environ, {"ATOMS_SECRET_ENCRYPTION_KEY": Fernet.generate_key().decode()}):
                    app = compose_application(settings)
                from dataclasses import replace
                service = app.services.builds
                service.dependencies = replace(service.dependencies, builder=BadExecutor())
                ctx = RequestContext(actor_id=EntityId("owner"), session_id=None,
                                     workspace_id=None, request_id="r")
                with self.assertRaises(DomainError):
                    await service.build(ctx, BuildInput(task_id=EntityId("task"),
                        version_id=EntityId("version"), source_archive=None, template_id="fixed"))
                with self.assertRaises(DomainError):
                    await service.scan(ctx, EntityId("version"), None)
        asyncio.run(run())

    def test_project_guard_retry_events_and_restart(self):
        async def run():
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                settings = Settings(database_url=f"sqlite:///{root / 'db.sqlite'}", blob_directory=root / "blobs",
                    public_origin="http://localhost:5173", preview_origin="http://localhost:8001",
                    model_provider="", model_secret_name="", task_timeout_seconds=180)
                key = Fernet.generate_key().decode()
                with patch.dict(os.environ, {"ATOMS_SECRET_ENCRYPTION_KEY": key}):
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
                self.assertIsNone(await app.services.tasks.latest_for_project(ctx, project.id))
                command = SubmitTask(project_id=project.id, message_id=EntityId("message-a"),
                    base_version_id=None, expected_head_revision=0, model="stub",
                    agent_mode="normal", mutation=Mutation(idempotency_key="first"))
                task = await app.services.tasks.submit(ctx, command)
                self.assertEqual((await app.services.tasks.submit(ctx, command)).id, task.id)
                self.assertEqual((await app.services.tasks.active_for_project(ctx, project.id)).id, task.id)
                self.assertEqual((await app.services.tasks.latest_for_project(ctx, project.id)).id, task.id)
                other_project = await app.services.projects.create(ctx, CreateProject(
                    name="Other", visibility=Visibility.PRIVATE,
                    mutation=Mutation(idempotency_key="other-project")))
                self.assertIsNone(await app.services.tasks.latest_for_project(ctx, other_project.id))
                outsider = RequestContext(actor_id=EntityId("outsider"), session_id=EntityId("outside"),
                    workspace_id=workspace, request_id="outside")
                with self.assertRaises(DomainError) as hidden:
                    await app.services.tasks.active_for_project(outsider, project.id)
                self.assertEqual(hidden.exception.code, ErrorCode.FORBIDDEN)
                with self.assertRaises(DomainError) as hidden_latest:
                    await app.services.tasks.latest_for_project(outsider, project.id)
                self.assertEqual(hidden_latest.exception.code, ErrorCode.FORBIDDEN)
                with self.assertRaises(DomainError) as busy:
                    await app.services.tasks.submit(ctx, SubmitTask(**{**command.__dict__,
                        "mutation": Mutation(idempotency_key="second")}))
                self.assertEqual(busy.exception.code, ErrorCode.CONFLICT)
                await app.services.tasks.append_event(ctx, TaskEvent(
                    event_id=EntityId("event-1"), task_id=task.id, project_id=project.id,
                    sequence=1, kind="queued", message="queued",
                    created_at=app.services.tasks.dependencies.clock.now()))
                self.assertEqual((await app.services.tasks.events(ctx, task.id, 0))[0].sequence, 1)
                task = await app.services.tasks.transition(ctx, TaskTransition(
                    task_id=task.id, expected_revision=task.revision, target=TaskState.GENERATING))
                async with app.services.tasks.dependencies.uow() as uow:
                    await uow.repository(LedgerRecord).save(Scope(workspace_id=workspace), LedgerRecord(
                        id=EntityId("test-credit"), workspace_id=workspace,
                        account=AccountKind.BUILD_CREDITS, units=200, unit="credit",
                        target_project_id=project.id, source_event_id="test-credit",
                        recorded_at=app.services.tasks.dependencies.clock.now()), None)
                    await uow.commit()
                reservation = await app.services.billing.reserve(ctx, task.id, 100,
                    Mutation(idempotency_key="reserve-test"), project.id)
                await app.services.tasks.record_usage(ctx, task.id, "generate", 5, 5)
                with self.assertRaises(DomainError):
                    await app.services.tasks.transition(ctx, TaskTransition(
                        task_id=task.id, expected_revision=1, target=TaskState.BUILDING))
                with patch.dict(os.environ, {"ATOMS_SECRET_ENCRYPTION_KEY": key}):
                    restarted = compose_application(settings)
                class PriceStub:
                    async def charge(self, model, input_tokens, output_tokens):
                        return input_tokens + output_tokens
                restarted.generation.pricing = PriceStub()
                self.assertEqual((await restarted.services.tasks.get(ctx, task.id)).state, TaskState.GENERATING)
                self.assertEqual(await app.queue.lease("active-worker"), task.id)
                await recover(restarted)
                self.assertEqual((await restarted.services.tasks.get(ctx, task.id)).state,
                                 TaskState.GENERATING)
                self.assertEqual(await restarted.services.billing.task_reservation_status(
                    workspace, task.id), "reserved")
                with connect(root / "db.sqlite") as db:
                    db.execute("UPDATE jobs SET leased_until=? WHERE task_id=?",
                               ("2000-01-01T00:00:00+00:00", str(task.id)))
                await recover(restarted)
                self.assertEqual((await restarted.services.tasks.get(ctx, task.id)).state, TaskState.INTERRUPTED)
                self.assertIsNone(await restarted.services.tasks.active_for_project(ctx, project.id))
                self.assertEqual((await restarted.services.tasks.latest_for_project(ctx, project.id)).id,
                                 task.id)
                async with restarted.services.tasks.dependencies.uow() as uow:
                    from atoms_demo.services.billing import ReservationRecord
                    stored = await uow.repository(ReservationRecord).get(
                        Scope(workspace_id=workspace), reservation.id)
                self.assertEqual((stored.status, stored.consumed_units), ("settled", 10))
                class UnavailablePricing:
                    async def charge(self, model, input_tokens, output_tokens):
                        raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Pricing is offline")
                restarted.generation.pricing = UnavailablePricing()
                await recover(restarted)
                self.assertTrue(await run_once(restarted, "cleanup-worker", 60))
                restarted.generation.pricing = PriceStub()
                second = await restarted.services.tasks.submit(ctx, SubmitTask(**{**command.__dict__,
                    "mutation": Mutation(idempotency_key="second")}))
                self.assertEqual(second.state, TaskState.QUEUED)
                self.assertEqual((await restarted.services.tasks.latest_for_project(ctx, project.id)).id,
                                 second.id)
                await restarted.services.billing.reserve(ctx, second.id, 100,
                    Mutation(idempotency_key="reserve-second"), project.id)
                self.assertEqual(await restarted.queue.lease("active-worker-2"), second.id)
                second = await restarted.services.tasks.transition(ctx, TaskTransition(
                    task_id=second.id, expected_revision=second.revision, target=TaskState.GENERATING))
                await restarted.services.tasks.begin_model_call(ctx, second.id,
                    "generate", "active-worker-2")
                second = await restarted.services.tasks.cancel(ctx, second.id,
                    Mutation(idempotency_key="cancel-second"))
                await recover(restarted)
                self.assertEqual(await restarted.services.billing.task_reservation_status(
                    workspace, second.id), "reserved")
                with connect(root / "db.sqlite") as db:
                    db.execute("UPDATE jobs SET leased_until=? WHERE task_id=?",
                               ("2000-01-01T00:00:00+00:00", str(second.id)))
                await recover(restarted)
                self.assertTrue(await restarted.services.tasks.has_uncheckpointed_model_call(second.id))
                self.assertEqual(await restarted.services.billing.task_reservation_status(
                    workspace, second.id), "reserved")
                await restarted.services.tasks.record_usage(ctx, second.id, "generate", 7, 7)
                await recover(restarted)
                self.assertEqual(await restarted.services.billing.task_reservation_status(
                    workspace, second.id), "settled")
                third = await restarted.services.tasks.submit(ctx, SubmitTask(**{**command.__dict__,
                    "mutation": Mutation(idempotency_key="third")}))
                for state in (TaskState.GENERATING, TaskState.BUILDING, TaskState.REPAIRING,
                              TaskState.BUILDING, TaskState.REPAIRING, TaskState.BUILDING):
                    third = await restarted.services.tasks.transition(ctx, TaskTransition(
                        task_id=third.id, expected_revision=third.revision, target=state))
                with self.assertRaises(DomainError) as repair_limit:
                    await restarted.services.tasks.transition(ctx, TaskTransition(
                        task_id=third.id, expected_revision=third.revision, target=TaskState.REPAIRING))
                self.assertEqual(repair_limit.exception.code, ErrorCode.CONFLICT)
        asyncio.run(run())
