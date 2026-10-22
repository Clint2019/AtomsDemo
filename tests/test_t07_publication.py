"""Manual publication stays pending until deployed version is independently verified."""
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
from atoms_demo.contracts.builds import ScanResult
from atoms_demo.contracts.projects import CreateProject, Visibility
from atoms_demo.contracts.publication import (ManualDeploymentResult,
    PublicationConfig, PublicationState)
from atoms_demo.contracts.versions import CreateVersion, FileEntry, PromoteVersion
from atoms_demo.contracts.workspaces import Membership, Role, Workspace
from atoms_demo.core.errors import DomainError, ErrorCode
from atoms_demo.core.types import EntityId, Environment, Mutation, RequestContext, Scope
from atoms_demo.ports.providers import DeploymentVerification
from atoms_demo.workflows.publication import PublicationWorkflow
from atoms_demo.workflows.registry import PublicationServices


class Builds:
    def __init__(self):
        self.fails = False

    async def scan(self, ctx, version_id, artifact):
        if self.fails:
            raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Scanner unavailable")
        return ScanResult(version_id=version_id, passed=True, findings=())


class Verifier:
    def __init__(self):
        self.matches = False

    async def verify(self, result, expected_version_id):
        return DeploymentVerification(reachable=True, version_matches=self.matches,
                                      diagnostics=())

    async def verify_state(self, result, expected_version_id, state):
        return DeploymentVerification(reachable=True, version_matches=self.matches,
                                      diagnostics=())


class PublicationTests(unittest.TestCase):
    def test_manual_deployment_requires_verified_version(self):
        async def run():
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                settings = Settings(database_url=f"sqlite:///{root / 'db.sqlite'}",
                    blob_directory=root / "blobs", public_origin="http://localhost:5173",
                    preview_origin="http://preview.localhost:8001", model_provider="",
                    model_secret_name="", task_timeout_seconds=180)
                with patch.dict(os.environ, {"ATOMS_SECRET_ENCRYPTION_KEY": Fernet.generate_key().decode()}):
                    app = compose_application(settings)
                owner = EntityId("owner")
                workspace = EntityId("workspace")
                ctx = RequestContext(actor_id=owner, session_id=EntityId("session"),
                    workspace_id=workspace, request_id="r")
                async with app.services.tasks.dependencies.uow() as uow:
                    await uow.repository(Workspace).save(Scope(workspace_id=workspace),
                        Workspace(id=workspace, name="Team", revision=0), None)
                    await uow.repository(Membership).save(Scope(workspace_id=workspace),
                        Membership(id=membership_id(workspace, owner), workspace_id=workspace,
                            user_id=owner, role=Role.OWNER, revision=0), None)
                    await uow.commit()
                project = await app.services.projects.create(ctx, CreateProject(
                    name="App", visibility=Visibility.PRIVATE,
                    mutation=Mutation(idempotency_key="project")))
                source = await app.services.versions.dependencies.blobs.put(
                    b"<h1>app</h1>", "text/html")
                version = await app.services.versions.create_candidate(ctx, CreateVersion(
                    project_id=project.id, base_version_id=None,
                    files=(FileEntry(path="index.html", blob=source),), summary="sample",
                    mutation=Mutation(idempotency_key="version")))
                artifact = await app.services.versions.dependencies.blobs.put(
                    b"built", "application/zip")
                await app.services.versions.record_build(ctx, version.id, artifact, True)
                verifier = Verifier()
                app.services.publication.dependencies = replace(
                    app.services.publication.dependencies, verifier=verifier)
                builds = Builds()
                workflow = PublicationWorkflow(PublicationServices(
                    versions=app.services.versions, builds=builds,
                    app_resources=app.services.app_resources,
                    publication=app.services.publication))
                command = PublicationConfig(project_id=project.id, version_id=version.id,
                    environment=Environment.PRODUCTION, domain=None,
                    sync_db=False, migrate_data=False,
                    mutation=Mutation(idempotency_key="publish"))
                draft = await workflow.prepare(ctx, command)
                self.assertEqual(draft.state, PublicationState.DRAFT)
                builds.fails = True
                self.assertEqual((await workflow.prepare(ctx, command)).revision, draft.revision)
                pending = await app.services.publication.request_manual(ctx, draft.id,
                    Mutation(idempotency_key="manual"))
                self.assertEqual(pending.state, PublicationState.AWAITING_MANUAL)
                evidence = await app.services.versions.dependencies.blobs.put(
                    b"operator deployment receipt", "text/plain")
                result = ManualDeploymentResult(publication_id=draft.id,
                    public_url="https://app.example.com", evidence=evidence,
                    operator_id=owner, mutation=Mutation(idempotency_key="verify"))
                with self.assertRaises(DomainError):
                    await app.services.publication.verify_manual_result(ctx, result)
                self.assertEqual((await app.services.publication.get(ctx, project.id)).state,
                    PublicationState.AWAITING_MANUAL)
                verifier.matches = True
                live = await app.services.publication.verify_manual_result(ctx, result)
                self.assertEqual((live.state, live.live_version_id),
                    (PublicationState.LIVE, version.id))
                head = await app.services.versions.promote(ctx, PromoteVersion(
                    project_id=project.id, version_id=version.id,
                    expected_head_revision=0, task_id=None,
                    mutation=Mutation(idempotency_key="head-live")))
                older = await app.services.versions.create_candidate(ctx, CreateVersion(
                    project_id=project.id, base_version_id=version.id,
                    files=(FileEntry(path="index.html", blob=source),), summary="older",
                    mutation=Mutation(idempotency_key="older")))
                await app.services.versions.record_build(ctx, older.id, artifact, True)
                head = await app.services.versions.promote(ctx, PromoteVersion(
                    project_id=project.id, version_id=older.id,
                    expected_head_revision=head.revision, task_id=None,
                    mutation=Mutation(idempotency_key="head-older")))
                newest = await app.services.versions.create_candidate(ctx, CreateVersion(
                    project_id=project.id, base_version_id=older.id,
                    files=(FileEntry(path="index.html", blob=source),), summary="newest",
                    mutation=Mutation(idempotency_key="newest")))
                await app.services.versions.record_build(ctx, newest.id, artifact, True)
                head = await app.services.versions.promote(ctx, PromoteVersion(
                    project_id=project.id, version_id=newest.id,
                    expected_head_revision=head.revision, task_id=None,
                    mutation=Mutation(idempotency_key="head-newest")))
                restored = await app.lifecycle.restore(ctx, PromoteVersion(
                    project_id=project.id, version_id=older.id,
                    expected_head_revision=head.revision, task_id=None,
                    mutation=Mutation(idempotency_key="restore-older")))
                self.assertEqual(restored.version_id, older.id)
                self.assertEqual((await app.services.publication.get(ctx, project.id)).live_version_id,
                    version.id)
                unchanged = await app.services.publication.change_state(ctx, live.id,
                    PublicationState.LIVE, Mutation(idempotency_key="noop"))
                self.assertEqual(unchanged, live)
                pending_pause = await app.services.publication.change_state(ctx, live.id,
                    PublicationState.PAUSED, Mutation(idempotency_key="pause"))
                self.assertEqual(pending_pause.state, PublicationState.AWAITING_MANUAL)
                self.assertEqual((await app.services.publication.change_state(ctx, live.id,
                    PublicationState.LIVE, Mutation(idempotency_key="noop"))), live)
                with self.assertRaises(DomainError) as wrong_url:
                    await app.services.publication.verify_manual_result(ctx, replace(result,
                        public_url="https://other.example.com",
                        mutation=Mutation(idempotency_key="wrong-pause-url")))
                self.assertEqual(wrong_url.exception.code, ErrorCode.INVALID_INPUT)
                verifier.matches = False
                with self.assertRaises(DomainError) as failure:
                    await app.services.publication.verify_manual_result(ctx, replace(result,
                        mutation=Mutation(idempotency_key="pause-verify")))
                self.assertEqual(failure.exception.code, ErrorCode.CONFLICT)
                verifier.matches = True
                paused = await app.services.publication.verify_manual_result(ctx, replace(result,
                    mutation=Mutation(idempotency_key="pause-verify")))
                self.assertEqual(paused.state, PublicationState.PAUSED)
        asyncio.run(run())
