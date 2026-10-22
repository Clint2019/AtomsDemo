"""T03 source, storage, conversation and restart behavior."""
import asyncio
import json
import os
import tempfile
import unittest
import zipfile
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from cryptography.fernet import Fernet

from atoms_demo.bootstrap import compose_application
from atoms_demo.adapters.blob_store import LocalBlobStore
from atoms_demo.config import Settings
from atoms_demo.contracts.identity import Credentials, VerificationRequest
from atoms_demo.contracts.projects import CreateProject, Visibility
from atoms_demo.contracts.storage import Upload
from atoms_demo.contracts.conversations import AppendMessage, MessageRole
from atoms_demo.contracts.versions import CreateVersion, FileEntry, PromoteVersion
from atoms_demo.core.errors import DomainError, ErrorCode
from atoms_demo.core.types import Mutation, PageQuery, RequestContext, Scope
from tests.test_workspace_profiles import CaptchaStub, MailStub


class T03Tests(unittest.TestCase):
    def test_physical_blob_cap_counts_retained_bytes(self):
        async def run():
            with tempfile.TemporaryDirectory() as temporary:
                store = LocalBlobStore(Path(temporary), max_bytes=10, max_total_bytes=8)
                await store.put(b"12345", "text/plain")
                await store.put(b"12345", "text/plain")
                with self.assertRaises(DomainError) as exceeded:
                    await store.put(b"abcdef", "text/plain")
                self.assertEqual(exceeded.exception.code, ErrorCode.QUOTA_EXCEEDED)
        asyncio.run(run())

    def test_source_storage_conversation_and_restart(self):
        async def run():
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                settings = Settings(database_url=f"sqlite:///{root / 'db.sqlite'}", blob_directory=root / "blobs",
                                    public_origin="http://localhost:5173", preview_origin="http://localhost:8001",
                                    model_provider="", model_secret_name="", task_timeout_seconds=180,
                                    storage_quota_bytes=1000)
                with patch.dict(os.environ, {"ATOMS_SECRET_ENCRYPTION_KEY": Fernet.generate_key().decode()}):
                    app = compose_application(settings)
                    mail = MailStub()
                    identity = app.services.identity
                    identity.dependencies = replace(identity.dependencies, captcha=CaptchaStub(), mail=mail)
                    anonymous = RequestContext(actor_id=None, session_id=None, workspace_id=None, request_id="r")
                    await identity.register(anonymous, Credentials(email="a@example.com", password="strong-password-123",
                                                                   captcha_token="ok", accepted_terms=True))
                    await identity.request_verification(anonymous, VerificationRequest(
                        email="a@example.com", mutation=Mutation(idempotency_key="verify")))
                    token = parse_qs(urlsplit(mail.urls[-1][1]).fragment)["token"][0]
                    session = await identity.verify(anonymous, token)
                    ctx = await identity.authenticate(str(session.id))
                    workspace = await app.services.workspaces.create(ctx, "Team", Mutation(idempotency_key="workspace"))
                    ctx = replace(ctx, workspace_id=workspace.id)
                    project = await app.services.projects.create(ctx, CreateProject(
                        name="App", visibility=Visibility.PRIVATE, mutation=Mutation(idempotency_key="project")))
                    second = await app.services.projects.create(ctx, CreateProject(
                        name="Other", visibility=Visibility.PRIVATE, mutation=Mutation(idempotency_key="other")))
                    scope = Scope(workspace_id=workspace.id, project_id=project.id)
                    upload = Upload(scope=scope, purpose="source", display_name="main.txt", media_type="text/plain",
                                    content=b"hello", mutation=Mutation(idempotency_key="upload"))
                    stored = await app.services.storage.upload(ctx, upload)
                    self.assertEqual((await app.services.storage.upload(ctx, upload)).id, stored.id)
                    self.assertEqual((await app.services.storage.download(ctx, stored.id)), b"hello")
                    wrong_attachment = await app.services.storage.upload(ctx, replace(
                        upload, scope=Scope(workspace_id=workspace.id, project_id=second.id),
                        mutation=Mutation(idempotency_key="wrong-attachment")))
                    self.assertEqual((await app.services.storage.usage(ctx)).used_bytes, 10)
                    with self.assertRaises(DomainError):
                        await app.services.storage.upload(ctx, replace(upload, content=b"different"))
                    version = await app.services.versions.create_candidate(ctx, CreateVersion(
                        project_id=project.id, base_version_id=None,
                        files=(FileEntry(path="main.txt", blob=stored.blob),), summary="initial",
                        mutation=Mutation(idempotency_key="version")))
                    self.assertEqual((await app.services.versions.create_candidate(ctx, CreateVersion(
                        project_id=project.id, base_version_id=None,
                        files=(FileEntry(path="main.txt", blob=stored.blob),), summary="initial",
                        mutation=Mutation(idempotency_key="version")))).id, version.id)
                    with self.assertRaises(DomainError):
                        await app.services.versions.create_candidate(ctx, CreateVersion(
                            project_id=project.id, base_version_id=None,
                            files=(FileEntry(path="../escape", blob=stored.blob),), summary="invalid",
                            mutation=Mutation(idempotency_key="bad-path")))
                    for unsafe_path in ("a/./b", "C:/secret", "a:stream", "CON.txt", "x./file"):
                        with self.assertRaises(DomainError):
                            await app.services.versions.create_candidate(ctx, CreateVersion(
                                project_id=project.id, base_version_id=None,
                                files=(FileEntry(path=unsafe_path, blob=stored.blob),), summary="invalid",
                                mutation=Mutation(idempotency_key=f"path-{unsafe_path}")))
                    package = await app.services.storage.upload(ctx, replace(
                        upload, content=json.dumps({"dependencies": {"bad": "1.0.0"}}).encode(),
                        display_name="package.json", mutation=Mutation(idempotency_key="package")))
                    with self.assertRaises(DomainError):
                        await app.services.versions.create_candidate(ctx, CreateVersion(
                            project_id=project.id, base_version_id=None,
                            files=(FileEntry(path="package.json", blob=package.blob),), summary="invalid",
                            mutation=Mutation(idempotency_key="bad-dependency")))
                    optional = await app.services.storage.upload(ctx, replace(
                        upload, content=json.dumps({"optionalDependencies": {"bad": "file:../bad"}}).encode(),
                        display_name="optional.json", mutation=Mutation(idempotency_key="optional")))
                    with self.assertRaises(DomainError):
                        await app.services.versions.create_candidate(ctx, CreateVersion(
                            project_id=project.id, base_version_id=None,
                            files=(FileEntry(path="package.json", blob=optional.blob),), summary="invalid",
                            mutation=Mutation(idempotency_key="bad-optional")))
                    message = await app.services.conversations.append(ctx, AppendMessage(
                        project_id=project.id, role=MessageRole.USER, content="build this",
                        attachment_ids=(stored.id,), task_id=None,
                        mutation=Mutation(idempotency_key="message")))
                    with self.assertRaises(DomainError):
                        await app.services.conversations.append(ctx, AppendMessage(
                            project_id=project.id, role=MessageRole.USER, content="steal",
                            attachment_ids=(wrong_attachment.id,), task_id=None,
                            mutation=Mutation(idempotency_key="cross-attachment")))
                    self.assertEqual((await app.services.conversations.history(ctx, project.id, PageQuery())).items[0].id,
                                     message.id)
                    later = []
                    for index in range(4):
                        later.append(await app.services.conversations.append(ctx, AppendMessage(
                            project_id=project.id, role=MessageRole.USER, content=f"later-{index}",
                            attachment_ids=(), task_id=None,
                            mutation=Mutation(idempotency_key=f"later-{index}"))))
                    recent = await app.services.conversations.history_recent(
                        ctx, project.id, PageQuery(limit=2))
                    self.assertEqual([item.id for item in recent.items], [later[2].id, later[3].id])
                    older = await app.services.conversations.history_recent(
                        ctx, project.id, PageQuery(cursor=recent.next_cursor, limit=2))
                    self.assertEqual([item.id for item in older.items], [later[0].id, later[1].id])
                    with self.assertRaises(DomainError):
                        await app.services.conversations.history_recent(
                            ctx, project.id, PageQuery(cursor="missing", limit=2))
                    other_message = await app.services.conversations.append(ctx, AppendMessage(
                        project_id=second.id, role=MessageRole.USER, content="other project",
                        attachment_ids=(), task_id=None,
                        mutation=Mutation(idempotency_key="other-message")))
                    for target_id, foreign_cursor in ((project.id, str(other_message.id)),
                                                      (second.id, str(later[0].id))):
                        for history in (app.services.conversations.history,
                                        app.services.conversations.history_recent):
                            with self.assertRaises(DomainError) as invalid_cursor:
                                await history(ctx, target_id,
                                              PageQuery(cursor=foreign_cursor, limit=2))
                            self.assertEqual(invalid_cursor.exception.code, ErrorCode.INVALID_INPUT)
                    self.assertEqual([item.id for item in
                        (await app.services.conversations.history_recent(
                            ctx, second.id, PageQuery(limit=2))).items], [other_message.id])
                    self.assertIn("build this", await app.services.conversations.summarize(ctx, project.id))
                    bulk = [message, *later]
                    for index in range(101):
                        bulk.append(await app.services.conversations.append(ctx, AppendMessage(
                            project_id=project.id, role=MessageRole.USER, content=f"page-boundary-{index}",
                            attachment_ids=(), task_id=None,
                            mutation=Mutation(idempotency_key=f"page-boundary-{index}"))))
                    ordered = sorted(bulk, key=lambda item: (item.created_at, str(item.id)), reverse=True)
                    newest = await app.services.conversations.history_recent(
                        ctx, project.id, PageQuery(limit=100))
                    self.assertEqual([item.id for item in newest.items],
                                     [item.id for item in reversed(ordered[:100])])
                    self.assertIsNotNone(newest.next_cursor)
                    remainder = await app.services.conversations.history_recent(
                        ctx, project.id, PageQuery(cursor=newest.next_cursor, limit=100))
                    self.assertEqual([item.id for item in remainder.items],
                                     [item.id for item in reversed(ordered[100:])])
                    self.assertIsNone(remainder.next_cursor)
                    await app.services.storage.delete(ctx, stored.id, Mutation(idempotency_key="delete"))
                    self.assertEqual(await app.services.versions.read_file(ctx, version.id, "main.txt"), "hello")
                    with self.assertRaises(DomainError):
                        await app.services.storage.download(ctx, stored.id)
                    artifact = await app.services.versions.dependencies.blobs.put(b"built", "application/octet-stream")
                    await app.services.versions.record_build(ctx, version.id, artifact, True)
                    head = await app.services.versions.promote(ctx, PromoteVersion(
                        project_id=project.id, version_id=version.id, expected_head_revision=0,
                        task_id=None, mutation=Mutation(idempotency_key="promote")))
                    self.assertEqual(head.version_id, version.id)
                    self.assertEqual((await app.services.versions.promote(ctx, PromoteVersion(
                        project_id=project.id, version_id=version.id, expected_head_revision=0,
                        task_id=None, mutation=Mutation(idempotency_key="promote")))).revision, head.revision)
                    with self.assertRaises(DomainError) as stale:
                        await app.services.versions.promote(ctx, PromoteVersion(
                            project_id=project.id, version_id=version.id, expected_head_revision=0,
                            task_id=None, mutation=Mutation(idempotency_key="stale")))
                    self.assertEqual(stale.exception.code, ErrorCode.CONFLICT)
                    with zipfile.ZipFile(BytesIO(await app.services.versions.dependencies.blobs.read(
                            await app.services.versions.export(ctx, version.id)))) as archive:
                        self.assertEqual(archive.read("main.txt"), b"hello")
                    with zipfile.ZipFile(BytesIO(await app.services.versions.download_export(
                            ctx, version.id))) as archive:
                        self.assertEqual(archive.read("source/main.txt"), b"hello")
                        self.assertIn(b"npm run dev", archive.read("README.md"))
                        self.assertIn(b"localAppDataPlugin", archive.read("vite.config.mjs"))
                        package = json.loads(archive.read("package.json"))
                        lock = json.loads(archive.read("package-lock.json"))
                        self.assertEqual(package["dependencies"],
                                         lock["packages"][""]["dependencies"])
                        self.assertEqual(package["scripts"]["build"],
                                         "vite build --config vite.config.mjs")
                    with self.assertRaises(DomainError):
                        await app.services.versions.create_candidate(ctx, CreateVersion(
                            project_id=second.id, base_version_id=version.id,
                            files=(FileEntry(path="main.txt", blob=stored.blob),), summary="wrong project",
                            mutation=Mutation(idempotency_key="cross-project")))
                    restarted = compose_application(settings)
                    self.assertEqual((await restarted.services.versions.head(ctx, project.id)).version_id, version.id)
                    self.assertEqual((await restarted.services.conversations.history(
                        ctx, project.id, PageQuery())).items[0].id, message.id)
                    self.assertEqual(await restarted.services.versions.read_file(ctx, version.id, "main.txt"), "hello")
        asyncio.run(run())
