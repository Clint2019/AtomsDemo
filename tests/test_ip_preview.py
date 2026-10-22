"""Public IP preview keeps each application inside its ticket and app session."""
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
from atoms_demo.api.preview_runtime import create_server
from atoms_demo.bootstrap import compose_application
from atoms_demo.config import Settings
from atoms_demo.contracts.projects import CreateProject, Visibility
from atoms_demo.contracts.versions import CreateVersion, FileEntry
from atoms_demo.contracts.workspaces import Membership, Role, Workspace
from atoms_demo.core.types import EntityId, Mutation, RequestContext, Scope


class IpPreviewTests(unittest.TestCase):
    def test_ticket_scoped_assets_data_share_and_sandbox(self):
        async def run():
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                with socket.socket() as free_port:
                    free_port.bind(("127.0.0.1", 0))
                    port = free_port.getsockname()[1]
                settings = Settings(database_url=f"sqlite:///{root / 'db.sqlite'}",
                    blob_directory=root / "blobs",
                    public_origin="http://1.1.1.1:18080",
                    preview_origin=f"http://1.1.1.1:{port}",
                    model_provider="", model_secret_name="", task_timeout_seconds=180,
                    public_ip_test_mode=True, preview_bind_port=port)
                with patch.dict(os.environ, {"ATOMS_SECRET_ENCRYPTION_KEY": Fernet.generate_key().decode()}):
                    app = compose_application(settings)
                workspace, owner = EntityId("workspace-ip"), EntityId("owner-ip")
                ctx = RequestContext(actor_id=owner, session_id=EntityId("session-ip"),
                                     workspace_id=workspace, request_id="r")
                async with app.services.tasks.dependencies.uow() as uow:
                    await uow.repository(Workspace).save(Scope(workspace_id=workspace),
                        Workspace(id=workspace, name="Team", revision=0), None)
                    await uow.repository(Membership).save(Scope(workspace_id=workspace),
                        Membership(id=membership_id(workspace, owner), workspace_id=workspace,
                                   user_id=owner, role=Role.OWNER, revision=0), None)
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
                        archive.writestr("index.html", '<html><head></head><body><script type="module" src="./assets/app.js"></script></body></html>')
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
                try:
                    conn = http.client.HTTPConnection("127.0.0.1", port)
                    host = f"1.1.1.1:{port}"

                    def request(method, path, headers=None, body=None):
                        conn.request(method, path, body=body, headers={"Host": host, **(headers or {})})
                        response = conn.getresponse()
                        data = response.read()
                        return response, data

                    response, html = request("GET", f"/p/{token_a}/", {"Cookie": "session=platform"})
                    self.assertEqual(response.status, 200)
                    self.assertIn(b"X-Preview-Session", html)
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
                    app_session = await app.preview.app_session(token_a, None)
                    headers = {"Origin": "null", "X-Preview-Session": str(app_session.id)}
                    response, _ = request("PUT", f"/p/{token_a}/app/records/one",
                        {**headers, "Content-Type": "application/json", "Idempotency-Key": "one"},
                        json.dumps({"collection": "items", "json_document": '{"x":1}',
                                    "schema_version": 1, "revision": 0}))
                    self.assertEqual(response.status, 200)
                    response, data = request("GET", f"/p/{token_a}/app/collections/items/records", headers)
                    self.assertEqual(response.status, 200)
                    self.assertIn(b"one", data)
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
                    await app.services.preview.revoke_ticket(ctx, ticket_a.id,
                        Mutation(idempotency_key="revoke-a"))
                    response, _ = request("GET", f"/p/{token_a}/")
                    self.assertEqual(response.status, 404)
                    conn.close()
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)

        asyncio.run(run())
