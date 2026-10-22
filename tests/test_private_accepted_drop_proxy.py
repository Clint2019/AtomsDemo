"""Transport regression for the private accepted-response-loss fault proxy."""

from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from private_accepted_drop_proxy import DropHandler  # noqa: E402


PROJECT = "a" * 32
USER = "b" * 32
WORKSPACE = "c" * 32
TASK = "d" * 32


class FakeUpstream(BaseHTTPRequestHandler):
    posts = 0
    keys: set[str] = set()

    def log_message(self, *args: object) -> None:
        pass

    def _json(self, status: int, value: dict) -> None:
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.headers.get("Cookie") != "private_test=1":
            self._json(401, {})
        elif self.path == "/api/v1/identity/me":
            self._json(200, {"user_id": USER, "workspace_id": WORKSPACE})
        elif self.path == f"/api/v1/projects/{PROJECT}":
            self._json(200, {"owner_id": USER, "workspace_id": WORKSPACE,
                             "visibility": "private"})
        else:
            self._json(404, {})

    def do_POST(self) -> None:
        type(self).posts += 1
        type(self).keys.add(self.headers.get("Idempotency-Key", ""))
        self._json(202, {"id": TASK, "project_id": PROJECT, "state": "queued"})


def test_first_response_dropped_then_same_request_recovers() -> None:
    FakeUpstream.posts = 0
    FakeUpstream.keys = set()
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
    with tempfile.TemporaryDirectory() as directory:
        state_file = Path(directory) / "state.json"
        state = {"phase": "ready", "project_id": PROJECT, "user_id": USER,
                 "workspace_id": WORKSPACE, "key_sha256": "", "body_sha256": "",
                 "task_id": "", "accepted_posts": 0, "replay_posts": 0}
        state_file.write_text(json.dumps(state), encoding="utf-8")
        DropHandler.upstream_port = upstream.server_port
        DropHandler.blocked_project = PROJECT
        DropHandler.state_file = state_file
        DropHandler.state = state
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), DropHandler)
        threads = [threading.Thread(target=server.serve_forever, daemon=True)
                   for server in (upstream, proxy)]
        for thread in threads:
            thread.start()
        body = json.dumps({"prompt": "T08 私有 F04 响应丢失恢复测试：本地替身",
                           "attachment_ids": [], "model": "qwen3.7-plus",
                           "agent_mode": "default", "expected_head_revision": 0},
                          ensure_ascii=False).encode("utf-8")
        headers = {"Cookie": "private_test=1", "Idempotency-Key": "only-one-task",
                   "Origin": f"http://app.preview.localhost:{proxy.server_port}",
                   "Content-Type": "application/json"}
        path = f"/api/v1/projects/{PROJECT}/generate"
        try:
            first = http.client.HTTPConnection("127.0.0.1", proxy.server_port, timeout=5)
            try:
                first.request("POST", path, body=body, headers=headers)
                try:
                    first.getresponse()
                except http.client.RemoteDisconnected:
                    pass
                else:
                    raise AssertionError("First accepted response was delivered")
            finally:
                first.close()
            dropped = json.loads(state_file.read_text(encoding="utf-8"))
            assert dropped["phase"] == "dropped"
            assert dropped["task_id"] == TASK
            assert dropped["accepted_posts"] == 1
            assert FakeUpstream.posts == 1
            replay = http.client.HTTPConnection("127.0.0.1", proxy.server_port, timeout=5)
            try:
                replay.request("POST", path, body=body, headers=headers)
                response = replay.getresponse()
                assert response.status == 202
                assert json.loads(response.read())["id"] == TASK
            finally:
                replay.close()
            recovered = json.loads(state_file.read_text(encoding="utf-8"))
            assert recovered["phase"] == "replayed"
            assert recovered["replay_posts"] == 1
            assert FakeUpstream.posts == 2
            assert FakeUpstream.keys == {"only-one-task"}
            denied = http.client.HTTPConnection("127.0.0.1", proxy.server_port, timeout=5)
            try:
                denied.request("POST", path, body=body,
                               headers={**headers, "Idempotency-Key": "different-task"})
                response = denied.getresponse()
                assert response.status == 405
                response.read()
            finally:
                denied.close()
            assert FakeUpstream.posts == 2
        finally:
            proxy.shutdown()
            upstream.shutdown()
            proxy.server_close()
            upstream.server_close()
            for thread in threads:
                thread.join(timeout=5)
