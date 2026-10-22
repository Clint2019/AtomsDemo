"""Public IP preview keeps each application inside its ticket and app session."""
import asyncio
import hashlib
import http.client
import io
import json
import os
import socket
import tempfile
import threading
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

from cryptography.fernet import Fernet

from atoms_demo.adapters.authorization import membership_id
from atoms_demo.api.preview_runtime import create_server
from atoms_demo.api.server import handler_type as platform_handler_type
from atoms_demo.bootstrap import compose_application
from atoms_demo.config import Settings
from atoms_demo.contracts.catalog import CatalogEntry, ContentKind
from atoms_demo.contracts.projects import CreateProject, Visibility
from atoms_demo.contracts.versions import CreateVersion, FileEntry
from atoms_demo.contracts.workspaces import Membership, Role, Workspace
from atoms_demo.core.types import EntityId, Mutation, RequestContext, Scope
from atoms_demo.services.identity import SessionRecord


class IpPreviewTests(unittest.TestCase):
    def test_ticket_scoped_assets_data_share_and_sandbox(self):
        async def run():
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                with socket.socket() as free_port:
                    free_port.bind(("127.0.0.1", 0))
                    port = free_port.getsockname()[1]
                with socket.socket() as free_port:
                    free_port.bind(("127.0.0.1", 0))
                    platform_port = free_port.getsockname()[1]
                settings = Settings(database_url=f"sqlite:///{root / 'db.sqlite'}",
                    blob_directory=root / "blobs",
                    public_origin=f"http://1.1.1.1:{platform_port}",
                    preview_origin=f"http://1.1.1.1:{port}",
                    model_provider="", model_secret_name="", task_timeout_seconds=180,
                    public_ip_test_mode=True, preview_bind_port=port)
                with patch.dict(os.environ, {"ATOMS_SECRET_ENCRYPTION_KEY": Fernet.generate_key().decode()}):
                    app = compose_application(settings)
                workspace, owner = EntityId("workspace-ip"), EntityId("owner-ip")
                collaborator = EntityId("collaborator-ip")
                ctx = RequestContext(actor_id=owner, session_id=EntityId("session-ip"),
                                     workspace_id=workspace, request_id="r")
                async with app.services.tasks.dependencies.uow() as uow:
                    await uow.repository(Workspace).save(Scope(workspace_id=workspace),
                        Workspace(id=workspace, name="Team", revision=0), None)
                    await uow.repository(Membership).save(Scope(workspace_id=workspace),
                        Membership(id=membership_id(workspace, owner), workspace_id=workspace,
                                   user_id=owner, role=Role.OWNER, revision=0), None)
                    await uow.repository(Membership).save(Scope(workspace_id=workspace),
                        Membership(id=membership_id(workspace, collaborator), workspace_id=workspace,
                                   user_id=collaborator, role=Role.MEMBER, revision=0), None)
                    for raw, actor in (("platform-owner-1", owner), ("platform-owner-2", owner),
                                       ("platform-collaborator", collaborator)):
                        await uow.repository(SessionRecord).save(Scope(workspace_id=None),
                            SessionRecord(id=EntityId(hashlib.sha256(raw.encode()).hexdigest()),
                                user_id=actor, workspace_id=None,
                                expires_at=app.services.identity.dependencies.clock.now() + timedelta(days=1)), None)
                    await uow.commit()

                async def built_project(name):
                    project = await app.services.projects.create(ctx, CreateProject(
                        name=name, visibility=Visibility.PRIVATE,
                        mutation=Mutation(idempotency_key=f"project-{name}")))
                    blob = await app.services.versions.dependencies.blobs.put(
                        b"<html><head></head><body></body></html>", "text/html")
                    version = await app.services.versions.create_candidate(ctx, CreateVersion(
                        project_id=project.id, base_version_id=None,
                        files=(FileEntry(path="index.html", blob=blob),), summary="sample",
                        mutation=Mutation(idempotency_key=f"version-{name}")))
                    archive_bytes = io.BytesIO()
                    with zipfile.ZipFile(archive_bytes, "w") as archive:
                        archive.writestr("index.html", '<html><head></head><body><script type="module" src="/assets/app.js"></script></body></html>')
                        archive.writestr("assets/app.js", "fetch('/app/collections/items/records')")
                    artifact = await app.services.versions.dependencies.blobs.put(
                        archive_bytes.getvalue(), "application/zip")
                    await app.services.versions.record_build(ctx, version.id, artifact, True)
                    return project, version, await app.preview.open(ctx, project.id, version.id)

                project_a, version_a, ticket_a = await built_project("A")
                _, _, ticket_b = await built_project("B")
                token_a = urlsplit(ticket_a.url).path.split("/")[2]
                token_b = urlsplit(ticket_b.url).path.split("/")[2]
                self.assertEqual(urlsplit(ticket_a.url).netloc, f"1.1.1.1:{port}")
                server = create_server(app, settings)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                platform_server = ThreadingHTTPServer(("127.0.0.1", platform_port),
                    platform_handler_type(settings, app))
                platform_thread = threading.Thread(target=platform_server.serve_forever, daemon=True)
                platform_thread.start()
                try:
                    conn = http.client.HTTPConnection("127.0.0.1", port)
                    host = f"1.1.1.1:{port}"

                    def owner_session(preview_token, platform_token="platform-owner-1"):
                        api = http.client.HTTPConnection("127.0.0.1", platform_port)
                        try:
                            csrf = "csrf-ip-test"
                            api.request("POST", "/api/v1/app/sessions",
                                body=json.dumps({"preview_token": preview_token}), headers={
                                    "Host": f"1.1.1.1:{platform_port}",
                                    "Origin": settings.public_origin,
                                    "Cookie": f"session={platform_token}; csrf_token={csrf}",
                                    "X-CSRF-Token": csrf,
                                    "Content-Type": "application/json"})
                            result = api.getresponse()
                            payload = json.loads(result.read())
                            return result.status, payload
                        finally:
                            api.close()

                    def request(method, path, headers=None, body=None):
                        conn.request(method, path, body=body, headers={"Host": host, **(headers or {})})
                        response = conn.getresponse()
                        data = response.read()
                        return response, data

                    response, html = request("GET", f"/p/{token_a}/", {"Cookie": "session=platform"})
                    self.assertEqual(response.status, 200)
                    self.assertIn(b"X-Preview-Session", html)
                    self.assertIn(b"let session=null", html)
                    self.assertIn(b"atoms:app-session-ping", html)
                    self.assertIn(f'src="/p/{token_a}/assets/app.js"'.encode(), html)
                    self.assertIn(b"sandbox allow-scripts allow-forms", response.getheader("Content-Security-Policy").encode())
                    self.assertEqual(response.getheaders().count(("Set-Cookie", "session=platform")), 0)
                    response, asset = request("GET", f"/p/{token_a}/assets/app.js", {"Origin": "null"})
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.getheader("Access-Control-Allow-Origin"), "null")
                    self.assertIn(b"fetch", asset)
                    response, _ = request("OPTIONS", f"/p/{token_a}/app/collections/items/records",
                        {"Origin": "null", "Access-Control-Request-Method": "GET",
                         "Access-Control-Request-Headers": "X-Preview-Session"})
                    self.assertEqual(response.status, 204)
                    response, _ = request("GET", f"/p/{token_a}/app/collections/items/records",
                        {"Origin": "null", "Cookie": "session=platform"})
                    self.assertEqual(response.status, 401)
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        first = pool.submit(owner_session, token_a, "platform-owner-1")
                        second = pool.submit(owner_session, token_a, "platform-owner-2")
                        status, app_session = first.result()
                        concurrent_status, concurrent_session = second.result()
                    self.assertEqual(status, 200, app_session)
                    self.assertEqual(concurrent_status, 200, concurrent_session)
                    self.assertEqual(concurrent_session["app_user_id"],
                                     app_session["app_user_id"])
                    unauthenticated_status, _ = owner_session(token_a, "invalid-platform")
                    self.assertEqual(unauthenticated_status, 401)
                    legacy = await app.preview.app_session(token_a, None)
                    response, _ = request("GET", f"/p/{token_a}/app/collections/items/records",
                        {"Origin": "null", "X-Preview-Session": str(legacy.id)})
                    self.assertEqual(response.status, 403)
                    headers = {"Origin": "null", "X-Preview-Session": app_session["id"]}
                    response, _ = request("PUT", f"/p/{token_a}/app/records/one",
                        {**headers, "Content-Type": "application/json", "Idempotency-Key": "one"},
                        json.dumps({"collection": "items", "json_document": '{"x":1}',
                                    "schema_version": 1, "revision": 0}))
                    self.assertEqual(response.status, 200)
                    response, data = request("GET", f"/p/{token_a}/app/collections/items/records", headers)
                    self.assertEqual(response.status, 200)
                    self.assertIn(b"one", data)
                    refreshed, html = request("GET", f"/p/{token_a}/",
                        {"Cookie": "session=platform-owner-2"})
                    self.assertEqual(refreshed.status, 200)
                    self.assertIn(b"atoms:app-session-ready", html)
                    second_status, second_session = owner_session(token_a, "platform-owner-2")
                    self.assertEqual(second_status, 200, second_session)
                    self.assertEqual(second_session["app_user_id"], app_session["app_user_id"])
                    response, data = request("GET", f"/p/{token_a}/app/collections/items/records",
                        {"Origin": "null", "X-Preview-Session": second_session["id"]})
                    self.assertEqual(response.status, 200)
                    self.assertIn(b"one", data)
                    collaborator_ctx = RequestContext(actor_id=collaborator,
                        session_id=EntityId("collaborator-session"), workspace_id=workspace,
                        request_id="collaborator")
                    collaborator_ticket = await app.preview.open(
                        collaborator_ctx, project_a.id, version_a.id)
                    collaborator_token = urlsplit(collaborator_ticket.url).path.split("/")[2]
                    collaborator_status, collaborator_session = owner_session(
                        collaborator_token, "platform-collaborator")
                    self.assertEqual(collaborator_status, 200, collaborator_session)
                    self.assertNotEqual(collaborator_session["app_user_id"], app_session["app_user_id"])
                    forbidden_status, _ = owner_session(token_a, "platform-collaborator")
                    self.assertEqual(forbidden_status, 403)
                    response, _ = request("GET", f"/p/{collaborator_token}/app/collections/items/records",
                        headers)
                    self.assertEqual(response.status, 403)
                    response, data = request("GET", f"/p/{collaborator_token}/app/collections/items/records",
                        {"Origin": "null", "X-Preview-Session": collaborator_session["id"]})
                    self.assertEqual(response.status, 200)
                    self.assertNotIn(b"one", data)
                    response, _ = request("GET", f"/p/{token_b}/app/collections/items/records", headers)
                    self.assertEqual(response.status, 403)
                    response, _ = request("GET", f"/p/{token_a}/app/collections/items/records",
                        {**headers, "Origin": settings.public_origin})
                    self.assertEqual(response.status, 403)
                    response, _ = request("GET", f"/p/{token_a}/assets/app.js",
                        {"Host": f"evil.example:{port}"})
                    self.assertEqual(response.status, 403)
                    share = await app.preview.share(ctx, project_a.id, version_a.id,
                        Mutation(idempotency_key="share-a"))
                    response, _ = request("GET", urlsplit(share.url).path)
                    self.assertEqual(response.status, 302)
                    self.assertTrue(response.getheader("Location").startswith(
                        f"http://1.1.1.1:{port}/p/"))
                    share_token = urlsplit(response.getheader("Location")).path.split("/")[2]
                    response, share_html = request("GET", f"/p/{share_token}/")
                    self.assertEqual(response.status, 200)
                    share_cookie = response.getheader("Set-Cookie")
                    self.assertTrue(share_cookie.startswith("ip_app_"))
                    first_share_session = share_html.split(b"let session=", 1)[1].split(b";", 1)[0]
                    response, share_html = request("GET", f"/p/{share_token}/",
                        {"Cookie": share_cookie.split(";", 1)[0]})
                    self.assertEqual(response.status, 200)
                    self.assertEqual(first_share_session,
                        share_html.split(b"let session=", 1)[1].split(b";", 1)[0])
                    share_session = json.loads(first_share_session)
                    response, _ = request("GET", urlsplit(share.url).path)
                    self.assertEqual(response.status, 302)
                    renewed_share_token = urlsplit(response.getheader("Location")).path.split("/")[2]
                    response, renewed_share_html = request("GET",
                        f"/p/{renewed_share_token}/",
                        {"Cookie": share_cookie.split(";", 1)[0]})
                    self.assertEqual(response.status, 200)
                    self.assertEqual(first_share_session,
                        renewed_share_html.split(b"let session=", 1)[1].split(b";", 1)[0])
                    response, _ = request("GET", f"/p/{share_token}/app/collections/items/records",
                        headers)
                    self.assertEqual(response.status, 403)
                    response, _ = request("GET", f"/p/{token_a}/app/collections/items/records",
                        {"Origin": "null", "X-Preview-Session": share_session})
                    self.assertEqual(response.status, 403)
                    response, data = request("GET", f"/p/{share_token}/app/collections/items/records",
                        {"Origin": "null", "X-Preview-Session": share_session})
                    self.assertEqual(response.status, 200)
                    self.assertNotIn(b"one", data)
                    other_share = await app.preview.share(ctx, project_a.id, version_a.id,
                        Mutation(idempotency_key="share-a-again"))
                    response, _ = request("GET", urlsplit(other_share.url).path)
                    self.assertEqual(response.status, 302)
                    other_share_token = urlsplit(response.getheader("Location")).path.split("/")[2]
                    response, _ = request("GET",
                        f"/p/{other_share_token}/app/collections/items/records",
                        {"Origin": "null", "X-Preview-Session": share_session})
                    self.assertEqual(response.status, 403)

                    renewal = await app.preview.open(ctx, project_a.id, version_a.id)
                    renewal_token = urlsplit(renewal.url).path.split("/")[2]
                    renewed_status, renewed_session = owner_session(
                        renewal_token, "platform-owner-2")
                    self.assertEqual(renewed_status, 200, renewed_session)
                    self.assertEqual(renewed_session["app_user_id"], app_session["app_user_id"])
                    response, data = request("GET", f"/p/{renewal_token}/app/collections/items/records",
                        {"Origin": "null", "X-Preview-Session": renewed_session["id"]})
                    self.assertEqual(response.status, 200)
                    self.assertIn(b"one", data)

                    next_version = await app.services.versions.create_candidate(ctx, CreateVersion(
                        project_id=project_a.id, base_version_id=version_a.id,
                        files=version_a.files, summary="next",
                        mutation=Mutation(idempotency_key="next-ip-version")))
                    built_version = await app.services.versions.get(ctx, version_a.id)
                    await app.services.versions.record_build(ctx, next_version.id,
                        built_version.artifact, True)
                    next_ticket = await app.preview.open(ctx, project_a.id, next_version.id)
                    next_token = urlsplit(next_ticket.url).path.split("/")[2]
                    next_status, next_session = owner_session(next_token)
                    self.assertEqual(next_status, 200, next_session)
                    self.assertEqual(next_session["app_user_id"], app_session["app_user_id"])
                    response, data = request("GET", f"/p/{next_token}/app/collections/items/records",
                        {"Origin": "null", "X-Preview-Session": next_session["id"]})
                    self.assertEqual(response.status, 200)
                    self.assertIn(b"one", data)

                    await app.services.projects.set_visibility(ctx, project_a.id, Visibility.PUBLIC,
                        Mutation(idempotency_key="public-ip"))
                    entry = await app.services.catalog.publish_entry(ctx, CatalogEntry(
                        id=EntityId("entry-ip"), kind=ContentKind.PUBLIC_PROJECT,
                        project_id=project_a.id, version_id=version_a.id,
                        author_id=owner, title="IP app", category="Tools", description="Sample",
                        views=0, saves=0, revision=0),
                        Mutation(idempotency_key="entry-ip"))
                    anonymous = RequestContext(actor_id=None, session_id=None,
                        workspace_id=None, request_id="anonymous")
                    catalog_ticket = await app.preview.open_catalog(anonymous, entry.id)
                    catalog_token = urlsplit(catalog_ticket.url).path.split("/")[2]
                    response, catalog_html = request("GET", f"/p/{catalog_token}/")
                    self.assertEqual(response.status, 200)
                    catalog_session = json.loads(
                        catalog_html.split(b"let session=", 1)[1].split(b";", 1)[0])
                    response, _ = request("GET",
                        f"/p/{catalog_token}/app/collections/items/records", headers)
                    self.assertEqual(response.status, 403)
                    response, data = request("GET",
                        f"/p/{catalog_token}/app/collections/items/records",
                        {"Origin": "null", "X-Preview-Session": catalog_session})
                    self.assertEqual(response.status, 200)
                    self.assertNotIn(b"one", data)
                    later_entry = await app.services.catalog.publish_entry(ctx, CatalogEntry(
                        id=EntityId("entry-ip-later"), kind=ContentKind.PUBLIC_PROJECT,
                        project_id=project_a.id, version_id=next_version.id,
                        author_id=owner, title="IP app later", category="Tools",
                        description="Next version", views=0, saves=0, revision=0),
                        Mutation(idempotency_key="entry-ip-later"))
                    later_catalog_ticket = await app.preview.open_catalog(anonymous, later_entry.id)
                    later_catalog_token = urlsplit(later_catalog_ticket.url).path.split("/")[2]
                    response, _ = request("GET",
                        f"/p/{later_catalog_token}/app/collections/items/records",
                        {"Origin": "null", "X-Preview-Session": catalog_session})
                    self.assertEqual(response.status, 403)
                    forbidden_status, _ = owner_session(share_token)
                    self.assertEqual(forbidden_status, 403)
                    forbidden_status, _ = owner_session(catalog_token)
                    self.assertEqual(forbidden_status, 403)
                    await app.services.preview.revoke_ticket(ctx, ticket_a.id,
                        Mutation(idempotency_key="revoke-a"))
                    response, _ = request("GET", f"/p/{token_a}/")
                    self.assertEqual(response.status, 404)
                    await app.services.identity.logout(
                        await app.services.identity.authenticate("platform-owner-1"))
                    response, _ = request("GET", f"/p/{next_token}/app/collections/items/records",
                        {"Origin": "null", "X-Preview-Session": next_session["id"]})
                    self.assertEqual(response.status, 401)
                    response, data = request("GET", f"/p/{renewal_token}/app/collections/items/records",
                        {"Origin": "null", "X-Preview-Session": renewed_session["id"]})
                    self.assertEqual(response.status, 200)
                    self.assertIn(b"one", data)
                    async with app.services.tasks.dependencies.uow() as uow:
                        await uow.repository(Membership).delete(Scope(workspace_id=workspace),
                            membership_id(workspace, owner), 1)
                        await uow.commit()
                    response, _ = request("GET", f"/p/{renewal_token}/app/collections/items/records",
                        {"Origin": "null", "X-Preview-Session": renewed_session["id"]})
                    self.assertEqual(response.status, 403)
                    forbidden_status, _ = owner_session(renewal_token, "platform-owner-2")
                    self.assertEqual(forbidden_status, 403)
                    conn.close()
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)
                    platform_server.shutdown()
                    platform_server.server_close()
                    platform_thread.join(timeout=2)

        asyncio.run(run())
