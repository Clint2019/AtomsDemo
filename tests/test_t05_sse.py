"""A task event stream stays open for later events and checks access first."""
import http.client
import threading
import unittest
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from atoms_demo.api.server import handler_type
from atoms_demo.config import Settings
from atoms_demo.contracts.tasks import TaskState
from atoms_demo.core.errors import DomainError, ErrorCode
from atoms_demo.core.events import TaskEvent
from atoms_demo.core.types import EntityId, RequestContext


class Identity:
    def __init__(self):
        self.revoked = False

    async def authenticate(self, token):
        if self.revoked:
            raise DomainError(ErrorCode.UNAUTHENTICATED, "Session revoked")
        return RequestContext(actor_id=EntityId("owner"), session_id=EntityId("session"),
                              workspace_id=None, request_id="auth")


class Workspaces:
    async def selected_workspace(self, session_id):
        return EntityId("workspace")


class Tasks:
    def __init__(self):
        self.items = []
        self.state = TaskState.GENERATING
        self.lock = threading.Lock()

    async def events(self, ctx, task_id, after):
        if ctx.actor_id != EntityId("owner"):
            raise DomainError(ErrorCode.FORBIDDEN, "Not allowed")
        with self.lock:
            return tuple(item for item in self.items if item.sequence > after)

    async def get(self, ctx, task_id):
        return SimpleNamespace(state=self.state)

    def append(self, sequence, kind):
        with self.lock:
            self.items.append(TaskEvent(event_id=EntityId(f"event-{sequence}"),
                task_id=EntityId("task"), project_id=EntityId("project"),
                sequence=sequence, kind=kind, message=kind,
                created_at=datetime.now(timezone.utc)))


class SSETests(unittest.TestCase):
    def test_delayed_event_and_reconnect_cursor(self):
        tasks = Tasks()
        tasks.append(1, "queued")
        settings = Settings(database_url="sqlite:///unused.db", blob_directory=Path("unused"),
            public_origin="http://localhost:5173", preview_origin="http://preview.localhost:8001",
            model_provider="", model_secret_name="", task_timeout_seconds=30)
        identity = Identity()
        app = SimpleNamespace(services=SimpleNamespace(identity=identity,
            workspaces=Workspaces(), tasks=tasks))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_type(settings, app))
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request("GET", "/api/v1/tasks/task/events?after=0", headers={"Cookie": "session=s"})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Content-Type"), "text/event-stream; charset=utf-8")
            self.assertEqual(response.fp.readline(), b": connected\n")
            self.assertEqual(response.fp.readline(), b"\n")
            self.assertEqual(response.fp.readline(), b"id: 1\n")
            tasks.append(2, "generating")
            while (line := response.fp.readline()) != b"id: 2\n":
                self.assertTrue(line, "Stream closed before delayed event")
            tasks.state = TaskState.SUCCEEDED
            tasks.append(3, "succeeded")
            while (line := response.fp.readline()) != b"id: 3\n":
                self.assertTrue(line, "Stream closed before terminal event")
            connection.close()

            replay = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            replay.request("GET", "/api/v1/tasks/task/events?after=0", headers={
                "Cookie": "session=s", "Last-Event-ID": "2"})
            response = replay.getresponse()
            self.assertEqual(response.status, 200)
            lines = [response.fp.readline() for _ in range(3)]
            self.assertEqual(lines[-1], b"id: 3\n")
            replay.close()

            with tasks.lock:
                tasks.items = [item for item in tasks.items if item.kind != "succeeded"]
            fallback = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=6)
            fallback.request("GET", "/api/v1/tasks/task/events?after=2", headers={"Cookie": "session=s"})
            response = fallback.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.fp.readline(), b": connected\n")
            self.assertEqual(response.fp.readline(), b"\n")
            self.assertEqual(response.fp.readline(), b"event: terminal\n")
            self.assertIn(b'"state": "succeeded"', response.fp.readline())
            fallback.close()

            denied = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            denied.request("GET", "/api/v1/tasks/task/events?after=0")
            self.assertEqual(denied.getresponse().status, 403)
            denied.close()
        finally:
            tasks.state = TaskState.SUCCEEDED
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_revoked_session_stops_stream_and_capacity_is_bounded(self):
        tasks = Tasks()
        settings = Settings(database_url="sqlite:///unused.db", blob_directory=Path("unused"),
            public_origin="http://localhost:5173", preview_origin="http://preview.localhost:8001",
            model_provider="", model_secret_name="", task_timeout_seconds=30)
        identity = Identity()
        app = SimpleNamespace(services=SimpleNamespace(identity=identity,
            workspaces=Workspaces(), tasks=tasks))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_type(settings, app))
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connections = []
        try:
            for _ in range(2):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                connection.request("GET", "/api/v1/tasks/task/events", headers={"Cookie": "session=s"})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.fp.readline(), b": connected\n")
                connections.append((connection, response))
            third = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            third.request("GET", "/api/v1/tasks/task/events", headers={"Cookie": "session=s"})
            self.assertEqual(third.getresponse().status, 429)
            third.close()

            identity.revoked = True
            tasks.append(1, "generating")
            for connection, response in connections:
                self.assertEqual(response.fp.readline(), b"\n")
                self.assertEqual(response.fp.readline(), b"")
                connection.close()
        finally:
            tasks.state = TaskState.SUCCEEDED
            for connection, _ in connections:
                connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
