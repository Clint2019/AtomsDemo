"""Behavioral checks for T01 foundations, independent of unfinished business modules."""
import asyncio
import json
import os
import socket
import tempfile
import threading
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet

from atoms_demo.adapters.blob_store import LocalBlobStore
from atoms_demo.adapters.event_store import SQLiteEventStore
from atoms_demo.adapters.job_queue import SQLiteJobQueue
from atoms_demo.adapters.persistence import SQLiteUnitOfWorkFactory
from atoms_demo.adapters.secret_store import EncryptedSecretStore
from atoms_demo.api.server import handler_type
from atoms_demo.api.preview_runtime import create_server
from atoms_demo.config import Settings, load_settings
from atoms_demo.core.events import TaskEvent
from atoms_demo.core.types import EntityId, PageQuery, Scope


@dataclass(frozen=True)
class Sample:
    id: EntityId
    name: str
    revision: int = 0


class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.settings = Settings(database_url=f"sqlite:///{root / 'data.db'}", blob_directory=root / "blobs",
                                 public_origin="http://localhost:8000", preview_origin="http://localhost:8001",
                                 model_provider="", model_secret_name="", task_timeout_seconds=180)

    def tearDown(self):
        self.temporary.cleanup()

    def test_configuration_rejects_missing_unsafe_origin_and_limits(self):
        with self.assertRaisesRegex(ValueError, "Missing configuration"):
            load_settings({})
        base = {"ATOMS_DATABASE_URL": self.settings.database_url,
                "ATOMS_BLOB_DIRECTORY": str(self.settings.blob_directory),
                "ATOMS_PUBLIC_ORIGIN": "http://app.preview.localhost:8000",
                "ATOMS_PREVIEW_ORIGIN": "http://preview.localhost:8001"}
        self.assertEqual(load_settings(base).max_repairs, 2)
        self.assertEqual(load_settings(base).model_timeout_seconds, 600)
        self.assertEqual(load_settings({**base, "ATOMS_MODEL_TIMEOUT_SECONDS": "420"}).model_timeout_seconds, 420)
        with self.assertRaises(ValueError):
            load_settings({**base, "ATOMS_MODEL_TIMEOUT_SECONDS": "3601"})
        with self.assertRaisesRegex(ValueError, "embedded previews"):
            load_settings({**base, "ATOMS_PUBLIC_ORIGIN": "http://localhost:8000"})
        with self.assertRaisesRegex(ValueError, "embedded previews"):
            load_settings({**base, "ATOMS_PUBLIC_ORIGIN": "http://app.other.localhost:8000"})
        with self.assertRaisesRegex(ValueError, "embedded previews"):
            load_settings({**base, "ATOMS_PUBLIC_ORIGIN": "http://127.0.0.1:8000"})
        with self.assertRaisesRegex(ValueError, "differ"):
            load_settings({**base, "ATOMS_PUBLIC_ORIGIN": "http://localhost:80",
                           "ATOMS_PREVIEW_ORIGIN": "http://localhost"})
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            load_settings({**base, "ATOMS_PUBLIC_ORIGIN": "http://example.com"})
        with self.assertRaisesRegex(ValueError, "hostname"):
            load_settings({**base, "ATOMS_PUBLIC_ORIGIN": "https://exa mple.com"})
        with self.assertRaisesRegex(ValueError, "between"):
            load_settings({**base, "ATOMS_MAX_REPAIRS": "3"})
        https = {**base, "ATOMS_PUBLIC_ORIGIN": "https://app.example.com",
                 "ATOMS_PREVIEW_ORIGIN": "https://preview.example.com"}
        with self.assertRaisesRegex(ValueError, "ATOMS_PREVIEW_BIND_PORT"):
            load_settings(https)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            bind_port = probe.getsockname()[1]
        with self.assertRaisesRegex(ValueError, "loopback"):
            load_settings({**https, "ATOMS_PREVIEW_BIND_PORT": str(bind_port),
                           "ATOMS_PREVIEW_BIND_HOST": "0.0.0.0"})
        with self.assertRaisesRegex(ValueError, "loopback"):
            load_settings({**https, "ATOMS_PREVIEW_BIND_PORT": str(bind_port),
                           "ATOMS_PREVIEW_BIND_HOST": "::1"})
        secure = load_settings({**https, "ATOMS_PREVIEW_BIND_PORT": str(bind_port)})
        self.assertEqual((secure.preview_bind_host, secure.preview_bind_port),
                         ("127.0.0.1", bind_port))
        preview_server = create_server(None, secure)
        try:
            self.assertEqual(preview_server.server_address, ("127.0.0.1", bind_port))
        finally:
            preview_server.server_close()

    def test_transaction_rollback_scope_and_revision(self):
        async def run():
            factory = SQLiteUnitOfWorkFactory(Path(self.temporary.name) / "data.db")
            factory.register(Sample, "aggregate_sample", lambda data: Sample(**data))
            scope = Scope(workspace_id=EntityId("one"))
            other = Scope(workspace_id=EntityId("two"))
            async with factory() as uow:
                saved = await uow.repository(Sample).save(scope, Sample(id=EntityId("a"), name="first"), None)
                self.assertEqual(saved.revision, 1)
                stale_repository = uow.repository(Sample)
                await uow.commit()
                with self.assertRaises(Exception):
                    await stale_repository.save(scope, Sample(id=EntityId("b"), name="late"), None)
            async with factory() as uow:
                self.assertIsNone(await uow.repository(Sample).get(other, EntityId("a")))
                self.assertEqual((await uow.repository(Sample).list(scope, PageQuery())).items[0].name, "first")
                await uow.repository(Sample).save(scope, Sample(id=EntityId("a"), name="discard"), 1)
            async with factory() as uow:
                self.assertEqual((await uow.repository(Sample).get(scope, EntityId("a"))).name, "first")
                with self.assertRaisesRegex(ValueError, "revision"):
                    await uow.repository(Sample).save(scope, Sample(id=EntityId("a"), name="stale"), 0)
        asyncio.run(run())

    def test_blob_integrity_and_limit(self):
        async def run():
            blobs = LocalBlobStore(self.settings.blob_directory, 8)
            ref = await blobs.put(b"hello", "text/plain")
            self.assertEqual(await blobs.read(ref), b"hello")
            with self.assertRaises(ValueError):
                await blobs.put(b"too-long-content", "text/plain")
            path = blobs._path(ref.sha256)
            path.write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "integrity"):
                await blobs.read(ref)
        asyncio.run(run())

    def test_event_cursor_queue_lease_and_secret_revocation(self):
        async def run():
            path = Path(self.temporary.name) / "data.db"
            events = SQLiteEventStore(path)
            event = TaskEvent(event_id=EntityId("e1"), task_id=EntityId("t1"), project_id=EntityId("p1"),
                              sequence=1, kind="queued", message="Queued", created_at=datetime.now(timezone.utc))
            await events.append(event)
            self.assertEqual(len(await events.read(EntityId("t1"), 0)), 1)
            self.assertEqual(await events.read(EntityId("t1"), 1), ())
            with self.assertRaises(ValueError):
                await events.append(event)
            queue = SQLiteJobQueue(path, 60)
            await queue.enqueue(EntityId("t1"), "same")
            await queue.enqueue(EntityId("t1"), "same")
            self.assertEqual(await queue.lease("worker-1"), EntityId("t1"))
            self.assertIsNone(await queue.lease("worker-2"))
            with self.assertRaises(ValueError):
                await queue.acknowledge(EntityId("t1"), "worker-2")
            await queue.heartbeat(EntityId("t1"), "worker-1")
            await queue.acknowledge(EntityId("t1"), "worker-1")
            self.assertIsNone(await queue.lease("worker-2"))
            secrets = EncryptedSecretStore(path, Fernet.generate_key().decode())
            ref = await secrets.put(Scope(workspace_id=EntityId("one")), "private-value")
            self.assertEqual(await secrets.resolve(ref), "private-value")
            self.assertNotIn(b"private-value", path.read_bytes())
            await secrets.revoke(ref)
            with self.assertRaises(KeyError):
                await secrets.resolve(ref)
        asyncio.run(run())

    def test_http_routes_fail_explicitly(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_type(self.settings))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(f"http://127.0.0.1:{server.server_port}/healthz") as response:
                self.assertEqual(response.status, 200)
            with self.assertRaises(HTTPError) as result:
                urlopen(f"http://127.0.0.1:{server.server_port}/api/v1/projects")
            self.assertEqual(result.exception.code, 501)
            self.assertEqual(json.load(result.exception)["code"], "not_implemented")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
