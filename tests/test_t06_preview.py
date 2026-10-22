"""Preview and share capabilities remain scoped to verified built versions."""
import asyncio
import http.client
import io
import json
import os
import socket
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

from cryptography.fernet import Fernet

from atoms_demo.adapters.authorization import membership_id
from atoms_demo.bootstrap import compose_application
from atoms_demo.api.preview_runtime import create_server
from atoms_demo.config import Settings
from atoms_demo.contracts.preview import RuntimeIssue
from atoms_demo.contracts.app_resources import AppDataContext, AppRecord, AppResource, ResourceKind
from atoms_demo.contracts.catalog import CatalogEntry, CatalogQuery, ContentKind
from atoms_demo.contracts.projects import CreateProject, Visibility
from atoms_demo.contracts.versions import CreateVersion, FileEntry, PromoteVersion
from atoms_demo.contracts.workspaces import Membership, Role, Workspace
from atoms_demo.core.errors import DomainError
from atoms_demo.core.types import EntityId, Environment, Mutation, PageQuery, RequestContext, Scope
from atoms_demo.workflows.project_lifecycle import CloneProject


class PreviewTests(unittest.TestCase):
    def test_version_ticket_share_revoke_and_scope(self):
        async def run():
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                with socket.socket() as free_port:
                    free_port.bind(("127.0.0.1", 0))
                    preview_port = free_port.getsockname()[1]
                settings = Settings(database_url=f"sqlite:///{root / 'db.sqlite'}",
                    blob_directory=root / "blobs", public_origin="http://localhost:5173",
                    preview_origin=f"http://preview.localhost:{preview_port}", model_provider="",
                    model_secret_name="", task_timeout_seconds=180)
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
                    name="App", visibility=Visibility.PRIVATE,
                    mutation=Mutation(idempotency_key="project")))
                blob = await app.services.versions.dependencies.blobs.put(b"<h1>app</h1>", "text/html")
                version = await app.services.versions.create_candidate(ctx, CreateVersion(
                    project_id=project.id, base_version_id=None,
                    files=(FileEntry(path="index.html", blob=blob),), summary="sample",
                    mutation=Mutation(idempotency_key="version")))
                with self.assertRaises(DomainError):
                    await app.preview.open(ctx, project.id, version.id)
                archive_bytes = io.BytesIO()
                with zipfile.ZipFile(archive_bytes, "w") as archive:
                    archive.writestr("index.html", '<h1>Preview</h1><script src="/assets/app.js"></script>')
                    archive.writestr("assets/app.js", "document.body.dataset.ready = 'yes'")
                artifact = await app.services.versions.dependencies.blobs.put(
                    archive_bytes.getvalue(), "application/zip")
                await app.services.versions.record_build(ctx, version.id, artifact, True)
                ticket = await app.preview.open(ctx, project.id, version.id)
                token = ticket.url.rsplit("/", 1)[-1]
                preview_host = urlsplit(ticket.url).netloc
                preview_origin = f"http://{preview_host}"
                self.assertEqual(await app.services.preview.serve_artifact(token), archive_bytes.getvalue())
                preview_server = create_server(app, settings)
                preview_thread = threading.Thread(target=preview_server.serve_forever, daemon=True)
                preview_thread.start()
                try:
                    connection = http.client.HTTPConnection("127.0.0.1", preview_port)
                    connection.request("GET", f"/preview/{token}", headers={
                        "Host": preview_host})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertIn(b"Preview", response.read())
                    cookies = "; ".join(header.split(";", 1)[0] for name, header in
                        response.getheaders() if name.lower() == "set-cookie")
                    connection.request("GET", "/assets/app.js", headers={
                        "Host": preview_host, "Cookie": cookies})
                    asset_response = connection.getresponse()
                    self.assertEqual(asset_response.status, 200)
                    self.assertIn(b"dataset.ready", asset_response.read())
                    connection.request("PUT", "/app/records/browser-1",
                        body=json.dumps({"collection": "items", "json_document": '{"x":1}',
                                         "schema_version": 1, "revision": 0}), headers={
                            "Host": preview_host, "Cookie": cookies,
                            "Origin": preview_origin, "Content-Type": "application/json",
                            "Idempotency-Key": "browser-write"})
                    write_response = connection.getresponse()
                    self.assertEqual(write_response.status, 200)
                    write_response.read()
                    connection.request("GET", "/app/collections/items/records", headers={
                        "Host": preview_host, "Cookie": cookies})
                    data_response = connection.getresponse()
                    self.assertEqual(data_response.status, 200)
                    self.assertIn(b"browser-1", data_response.read())
                    connection.request("GET", "/app/collections/items/records", headers={
                        "Host": preview_host, "Cookie": cookies,
                        "Origin": settings.public_origin})
                    cross_origin = connection.getresponse()
                    self.assertEqual(cross_origin.status, 403)
                    cross_origin.read()
                    connection.request("GET", "/assets/app.js", headers={
                        "Host": f"localhost:{preview_port}", "Cookie": cookies})
                    wrong_host = connection.getresponse()
                    self.assertEqual(wrong_host.status, 403)
                    wrong_host.read()
                    connection.close()
                finally:
                    preview_server.shutdown()
                    preview_server.server_close()
                    preview_thread.join(timeout=2)
                share = await app.preview.share(ctx, project.id, version.id,
                    Mutation(idempotency_key="share"))
                share_token = share.url.rsplit("/", 1)[-1]
                preview_server = create_server(app, settings)
                preview_thread = threading.Thread(target=preview_server.serve_forever, daemon=True)
                preview_thread.start()
                try:
                    connection = http.client.HTTPConnection("127.0.0.1", preview_port)
                    connection.request("GET", f"/share/{share_token}", headers={
                        "Host": urlsplit(share.url).netloc})
                    share_response = connection.getresponse()
                    self.assertEqual(share_response.status, 302)
                    share_location = share_response.getheader("Location")
                    share_response.read()
                    self.assertEqual(urlsplit(share_location).netloc, preview_host)
                    connection.request("GET", urlsplit(share_location).path, headers={
                        "Host": preview_host})
                    follow_response = connection.getresponse()
                    self.assertEqual(follow_response.status, 200)
                    self.assertIn(b"Preview", follow_response.read())
                    connection.close()
                finally:
                    preview_server.shutdown()
                    preview_server.server_close()
                    preview_thread.join(timeout=2)
                shared_ticket = await app.services.preview.resolve_share(share_token)
                shared_token = shared_ticket.url.rsplit("/", 1)[-1]
                self.assertEqual(await app.services.preview.serve_artifact(shared_token), archive_bytes.getvalue())
                app_session = await app.preview.app_session(token, None)
                renewed = await app.preview.app_session(token, str(app_session.id))
                self.assertEqual(renewed.app_user_id, app_session.app_user_id)
                data_ctx = await app.services.app_resources.authenticate_app(str(app_session.id))
                saved = await app.services.app_resources.put_record(data_ctx, AppRecord(
                    id=EntityId("record-1"), collection="items", json_document='{"label":"one"}',
                    schema_version=1, revision=0), Mutation(idempotency_key="data-1"))
                self.assertEqual(saved.revision, 1)
                changed = await app.services.app_resources.put_record(data_ctx, AppRecord(
                    id=saved.id, collection="items", json_document='{"label":"two"}',
                    schema_version=1, revision=saved.revision), Mutation(idempotency_key="data-2"))
                self.assertEqual(changed.revision, 2)
                replayed = await app.services.app_resources.put_record(data_ctx, AppRecord(
                    id=saved.id, collection="items", json_document='{"label":"one"}',
                    schema_version=1, revision=0), Mutation(idempotency_key="data-1"))
                self.assertEqual(replayed, saved)
                wrong_environment = AppDataContext(project_id=data_ctx.project_id,
                    app_user_id=data_ctx.app_user_id, environment=Environment.PRODUCTION)
                with self.assertRaises(DomainError):
                    await app.services.app_resources.records(wrong_environment, "items",
                        PageQuery(limit=20))
                listing = await app.services.app_resources.records(data_ctx, "items",
                    PageQuery(limit=20))
                self.assertEqual(listing.items[0].id, saved.id)
                other_session = await app.preview.app_session(token, None)
                other_context = await app.services.app_resources.authenticate_app(
                    str(other_session.id))
                for index in range(3):
                    await app.services.app_resources.put_record(other_context, AppRecord(
                        id=EntityId(f"a-foreign-{index}"), collection="items",
                        json_document='{"foreign":true}', schema_version=1, revision=0),
                        Mutation(idempotency_key=f"foreign-{index}"))
                own_second = await app.services.app_resources.put_record(data_ctx, AppRecord(
                    id=EntityId("z-own-2"), collection="items",
                    json_document='{"label":"three"}', schema_version=1, revision=0),
                    Mutation(idempotency_key="own-second"))
                first_page = await app.services.app_resources.records(data_ctx, "items",
                    PageQuery(limit=1))
                self.assertEqual(tuple(item.id for item in first_page.items), (saved.id,))
                second_page = await app.services.app_resources.records(data_ctx, "items",
                    PageQuery(cursor=first_page.next_cursor, limit=1))
                self.assertEqual(tuple(item.id for item in second_page.items), (own_second.id,))
                self.assertIsNone(second_page.next_cursor)
                compatible = await app.services.versions.create_candidate(ctx, CreateVersion(
                    project_id=project.id, base_version_id=version.id,
                    files=(FileEntry(path="index.html", blob=blob),), summary="compatible update",
                    mutation=Mutation(idempotency_key="compatible-version")))
                await app.services.versions.record_build(ctx, compatible.id, artifact, True)
                next_ticket = await app.preview.open(ctx, project.id, compatible.id)
                self.assertNotEqual(urlsplit(next_ticket.url).netloc, preview_host)
                continued = await app.preview.app_session(next_ticket.url.rsplit("/", 1)[-1],
                                                           str(app_session.id))
                self.assertEqual(continued.app_user_id, app_session.app_user_id)
                continued_context = await app.services.app_resources.authenticate_app(
                    str(continued.id))
                after_update = await app.services.app_resources.records(continued_context, "items",
                    PageQuery(limit=20))
                self.assertEqual(after_update.items[0].json_document, '{"label":"two"}')
                resource = await app.services.app_resources.configure(ctx, AppResource(
                    id=EntityId("resource-1"), project_id=project.id,
                    environment=Environment.DEVELOPMENT, kind=ResourceKind.DATABASE,
                    name="Data", status="configured", secret_ref=None, revision=0),
                    Mutation(idempotency_key="resource"))
                repeated_resource = await app.services.app_resources.configure(ctx, AppResource(
                    id=EntityId("resource-1"), project_id=project.id,
                    environment=Environment.DEVELOPMENT, kind=ResourceKind.DATABASE,
                    name="Data", status="configured", secret_ref=None, revision=0),
                    Mutation(idempotency_key="resource"))
                self.assertEqual(repeated_resource, resource)
                resource = await app.services.app_resources.set_secret(ctx, resource.id,
                    "private-value", Mutation(idempotency_key="resource-secret"))
                self.assertEqual(await app.services.app_resources.set_secret(ctx, resource.id,
                    "private-value", Mutation(idempotency_key="resource-secret")), resource)
                with self.assertRaises(DomainError):
                    await app.services.app_resources.set_secret(ctx, resource.id,
                        "different-value", Mutation(idempotency_key="resource-secret"))
                rotated = await app.services.app_resources.set_secret(ctx, resource.id,
                    "中文密钥", Mutation(idempotency_key="resource-secret-2"))
                self.assertEqual(await app.services.app_resources.set_secret(ctx, resource.id,
                    "中文密钥", Mutation(idempotency_key="resource-secret-2")), rotated)
                self.assertEqual(await app.services.app_resources.set_secret(ctx, resource.id,
                    "private-value", Mutation(idempotency_key="resource-secret")), resource)
                self.assertIsNotNone(resource.secret_ref)
                self.assertNotIn("private-value", repr(resource))
                await app.services.preview.report_issue(ctx, RuntimeIssue(
                    preview_token=token, project_id=project.id, version_id=version.id,
                    message="runtime error", stack=None, source_origin=preview_origin))
                await app.services.preview.revoke_share(ctx, share.id,
                    Mutation(idempotency_key="revoke"))
                with self.assertRaises(DomainError):
                    await app.services.preview.resolve_share(share_token)
                with self.assertRaises(DomainError):
                    await app.services.preview.resolve_ticket(shared_token)
                await app.services.preview.revoke_ticket(ctx, ticket.id,
                    Mutation(idempotency_key="ticket-revoke"))
                with self.assertRaises(DomainError):
                    await app.services.preview.resolve_ticket(token)
                stranger = RequestContext(actor_id=EntityId("stranger"), session_id=None,
                                          workspace_id=workspace, request_id="stranger")
                with self.assertRaises(DomainError):
                    await app.preview.open(stranger, project.id, version.id)
                with self.assertRaises(DomainError):
                    await app.services.app_resources.resources(stranger, project.id,
                        Environment.DEVELOPMENT)
                await app.services.projects.set_visibility(ctx, project.id, Visibility.PUBLIC,
                    Mutation(idempotency_key="public"))
                entry = await app.services.catalog.publish_entry(ctx, CatalogEntry(
                    id=EntityId("entry-1"), kind=ContentKind.PUBLIC_PROJECT,
                    project_id=project.id, version_id=version.id, author_id=owner,
                    title="Sample app", category="Tools", description="User project",
                    views=0, saves=0, revision=0), Mutation(idempotency_key="entry"))
                template = await app.services.catalog.publish_entry(ctx, CatalogEntry(
                    id=EntityId("template-1"), kind=ContentKind.TEMPLATE,
                    project_id=project.id, version_id=version.id, author_id=owner,
                    title="Starter template", category="Tools",
                    description="Community project version", views=0, saves=0,
                    revision=0), Mutation(idempotency_key="template"))
                anonymous = RequestContext(actor_id=None, session_id=None,
                    workspace_id=None, request_id="anonymous-catalog")
                public_ticket = await app.preview.open_catalog(anonymous, entry.id)
                public_token = public_ticket.url.rsplit("/", 1)[-1]
                owner_ticket = await app.preview.open(ctx, project.id, version.id)
                self.assertNotEqual(urlsplit(owner_ticket.url).netloc,
                                    urlsplit(public_ticket.url).netloc)
                public_host = urlsplit(public_ticket.url).netloc
                owner_host = urlsplit(owner_ticket.url).netloc
                preview_server = create_server(app, settings)
                preview_thread = threading.Thread(target=preview_server.serve_forever, daemon=True)
                preview_thread.start()
                try:
                    connection = http.client.HTTPConnection("127.0.0.1", preview_port)

                    def open_preview(preview_ticket, host, cookies=""):
                        connection.request("GET", urlsplit(preview_ticket.url).path,
                            headers={"Host": host, "Cookie": cookies})
                        response = connection.getresponse()
                        body = response.read()
                        self.assertEqual(response.status, 200, body)
                        return {header.split("=", 1)[0]: header.split(";", 1)[0]
                            for name, header in response.getheaders()
                            if name.lower() == "set-cookie"}

                    owner_cookies = open_preview(owner_ticket, owner_host)
                    self.assertIn("app_session", owner_cookies)
                    owner_cookie = "; ".join(owner_cookies.values())
                    connection.request("PUT", "/app/records/owner-visible", body=json.dumps({
                        "collection": "items", "json_document": '{"owner":true}',
                        "schema_version": 1, "revision": 0}), headers={
                            "Host": owner_host, "Cookie": owner_cookie,
                            "Origin": f"http://{owner_host}",
                            "Content-Type": "application/json", "Idempotency-Key": "owner-visible"})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200, response.read())
                    response.read()

                    public_cookies = open_preview(public_ticket, public_host,
                        owner_cookies["app_session"])
                    self.assertIn("app_session_public_preview", public_cookies)
                    self.assertNotIn("app_session", public_cookies)
                    public_cookie = "; ".join(public_cookies.values())
                    connection.request("GET", "/app/collections/items/records", headers={
                        "Host": public_host, "Cookie": public_cookie})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertNotIn(b"owner-visible", response.read())
                    connection.request("GET", f"/preview/{public_token}", headers={
                        "Host": owner_host})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 403)
                    response.read()

                    combined = "; ".join((owner_cookies["app_session"],
                        public_cookies["app_session_public_preview"]))
                    owner_return = open_preview(owner_ticket, owner_host, combined)
                    self.assertEqual(owner_return["app_session"], owner_cookies["app_session"])
                    connection.request("GET", "/app/collections/items/records", headers={
                        "Host": owner_host, "Cookie": "; ".join((
                            owner_return["preview_ticket"], owner_return["app_session"],
                            public_cookies["app_session_public_preview"]))})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertIn(b"owner-visible", response.read())
                    connection.close()
                finally:
                    preview_server.shutdown()
                    preview_server.server_close()
                    preview_thread.join(timeout=2)
                self.assertEqual(await app.services.preview.serve_artifact(public_token),
                                 archive_bytes.getvalue())
                public_session = await app.preview.app_session(public_token, None)
                self.assertEqual(public_session.environment, Environment.PUBLIC_PREVIEW)
                await app.services.app_resources.authenticate_app(str(public_session.id))
                await app.services.preview.revoke_ticket(ctx, public_ticket.id,
                    Mutation(idempotency_key="revoke-catalog-ticket"))
                with self.assertRaises(DomainError):
                    await app.services.preview.resolve_ticket(public_token)
                public_ticket = await app.preview.open_catalog(anonymous, entry.id)
                public_token = public_ticket.url.rsplit("/", 1)[-1]
                public_session = await app.preview.app_session(public_token, None)
                await app.services.projects.set_visibility(ctx, project.id, Visibility.PRIVATE,
                    Mutation(idempotency_key="hide-public-work"))
                with self.assertRaises(DomainError):
                    await app.services.preview.resolve_ticket(public_token)
                with self.assertRaises(DomainError):
                    await app.services.app_resources.authenticate_app(str(public_session.id))
                with self.assertRaises(DomainError):
                    await app.preview.open_catalog(anonymous, entry.id)
                await app.services.projects.set_visibility(ctx, project.id, Visibility.PUBLIC,
                    Mutation(idempotency_key="republish-work"))
                self.assertIn(template.id, tuple(item.id for item in
                    (await app.services.catalog.search(ctx, CatalogQuery(
                        page=PageQuery(limit=20), kind=ContentKind.TEMPLATE))).items))
                await app.services.catalog.record_view(stranger, entry.id,
                    Mutation(idempotency_key="view-1"))
                await app.services.catalog.save(ctx, entry.id, True,
                    Mutation(idempotency_key="save-1"))
                found = await app.services.catalog.search(ctx, CatalogQuery(
                    page=PageQuery(limit=20), saved_only=True))
                self.assertEqual((found.items[0].views, found.items[0].saves), (1, 1))
                self.assertTrue(found.items[0].saved)
                self.assertTrue((await app.services.catalog.detail(ctx, entry.id)).saved)
                self.assertFalse((await app.services.catalog.detail(stranger, entry.id)).saved)
                await app.services.catalog.save(ctx, entry.id, False,
                    Mutation(idempotency_key="unsave-1"))
                self.assertFalse((await app.services.catalog.detail(ctx, entry.id)).saved)
                self.assertEqual((await app.services.catalog.search(ctx, CatalogQuery(
                    page=PageQuery(limit=20), saved_only=True))).items, ())
                await app.services.catalog.save(ctx, entry.id, True,
                    Mutation(idempotency_key="save-2"))
                clone_command = CloneProject(source_version_id=version.id,
                    target_name="Cloned app", source_entry_id=entry.id,
                    mutation=Mutation(idempotency_key="clone-1"))
                clone = await app.lifecycle.clone(ctx, clone_command)
                self.assertEqual((await app.lifecycle.clone(ctx, clone_command)).id, clone.id)
                template_clone = await app.lifecycle.clone(ctx, CloneProject(
                    source_version_id=version.id, target_name="Template app",
                    source_entry_id=template.id,
                    mutation=Mutation(idempotency_key="template-clone")))
                self.assertEqual(template_clone.source_version_id, version.id)
                cloned_head = await app.services.versions.head(ctx, clone.id)
                cloned_version = await app.services.versions.get(ctx, cloned_head.version_id)
                self.assertEqual(cloned_version.files, version.files)
                self.assertEqual(cloned_version.build_status, "success")
                self.assertEqual(clone.source_version_id, version.id)
                self.assertEqual(await app.services.app_resources.resources(ctx, clone.id,
                    Environment.DEVELOPMENT), ())
                clone_ticket = await app.preview.open(ctx, clone.id, cloned_version.id)
                clone_host = urlsplit(clone_ticket.url).netloc
                preview_server = create_server(app, settings)
                preview_thread = threading.Thread(target=preview_server.serve_forever, daemon=True)
                preview_thread.start()
                try:
                    connection = http.client.HTTPConnection("127.0.0.1", preview_port)
                    connection.request("GET", urlsplit(clone_ticket.url).path,
                        headers={"Host": clone_host})
                    clone_response = connection.getresponse()
                    self.assertEqual(clone_response.status, 200)
                    clone_response.read()
                    clone_cookies = "; ".join(header.split(";", 1)[0] for name, header in
                        clone_response.getheaders() if name.lower() == "set-cookie")
                    connection.request("GET", "/app/collections/items/records", headers={
                        "Host": preview_host, "Cookie": clone_cookies})
                    cross_project = connection.getresponse()
                    self.assertEqual(cross_project.status, 403)
                    cross_project.read()
                    connection.close()
                finally:
                    preview_server.shutdown()
                    preview_server.server_close()
                    preview_thread.join(timeout=2)
                newer = await app.services.versions.create_candidate(ctx, CreateVersion(
                    project_id=clone.id, base_version_id=cloned_version.id,
                    files=cloned_version.files, summary="revision",
                    mutation=Mutation(idempotency_key="clone-next")))
                await app.services.versions.record_build(ctx, newer.id, artifact, True)
                newer_head = await app.services.versions.promote(ctx, PromoteVersion(
                    project_id=clone.id, version_id=newer.id,
                    expected_head_revision=cloned_head.revision, task_id=None,
                    mutation=Mutation(idempotency_key="clone-next-promote")))
                restored = await app.lifecycle.restore(ctx, PromoteVersion(
                    project_id=clone.id, version_id=cloned_version.id,
                    expected_head_revision=newer_head.revision, task_id=None,
                    mutation=Mutation(idempotency_key="restore")))
                self.assertEqual(restored.version_id, cloned_version.id)
                self.assertEqual((await app.services.versions.get(ctx, newer.id)).id, newer.id)
                await app.services.projects.set_visibility(ctx, project.id, Visibility.PRIVATE,
                    Mutation(idempotency_key="private"))
                self.assertEqual((await app.services.catalog.search(ctx, CatalogQuery(
                    page=PageQuery(limit=20)))).items, ())
        asyncio.run(run())
