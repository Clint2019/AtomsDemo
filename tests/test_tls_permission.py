"""Certificate issuance must be restricted to real, built preview versions."""
import hashlib
import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from atoms_demo.api.tls_permission import certificate_allowed, handler_type


def host(project: str, version: str, kind: str = "v") -> str:
    project_hash = hashlib.sha256(project.encode()).hexdigest()[:32]
    version_hash = hashlib.sha256(version.encode()).hexdigest()[:32]
    return f"{kind}-{version_hash}.p-{project_hash}.preview.example.com"


class TLSPermissionTests(unittest.TestCase):
    def test_only_existing_built_version_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "platform.db"
            with closing(sqlite3.connect(database)) as db:
                db.execute("CREATE TABLE aggregate_projects (id TEXT, workspace_id TEXT, payload TEXT)")
                db.execute("CREATE TABLE aggregate_version_locators (id TEXT, payload TEXT)")
                db.execute("CREATE TABLE aggregate_version_builds (id TEXT, payload TEXT)")
                db.execute("CREATE TABLE aggregate_catalog_entries (id TEXT, payload TEXT)")
                db.execute("INSERT INTO aggregate_projects VALUES ('project-a','workspace-a',?)", (
                    json.dumps({"visibility": "private", "owner_id": "owner-a"}),))
                db.execute("INSERT INTO aggregate_version_locators VALUES (?,?)", (
                    "version-a", json.dumps({"target_project_id": "project-a",
                                             "target_workspace_id": "workspace-a"})))
                db.execute("INSERT INTO aggregate_version_builds VALUES (?,?)", (
                    "version-a", json.dumps({"build_status": "success",
                                             "artifact": {"id": "blob-a"}})))
                db.commit()
            origin = "https://preview.example.com"
            self.assertTrue(certificate_allowed(host("project-a", "version-a"), origin, database))
            self.assertFalse(certificate_allowed(host("project-a", "version-a", "c"), origin, database))
            with closing(sqlite3.connect(database)) as db:
                db.execute("UPDATE aggregate_projects SET payload=? WHERE id='project-a'", (
                    json.dumps({"visibility": "public", "owner_id": "owner-a"}),))
                db.commit()
            self.assertFalse(certificate_allowed(host("project-a", "version-a", "c"), origin, database))
            with closing(sqlite3.connect(database)) as db:
                db.execute("INSERT INTO aggregate_catalog_entries VALUES ('entry-a',?)", (
                    json.dumps({"target_project_id": "project-a", "version_id": "version-a",
                                "author_id": "wrong-owner"}),))
                db.commit()
            self.assertFalse(certificate_allowed(host("project-a", "version-a", "c"), origin, database))
            with closing(sqlite3.connect(database)) as db:
                db.execute("UPDATE aggregate_catalog_entries SET payload=? WHERE id='entry-a'", (
                    json.dumps({"target_project_id": "project-a", "version_id": "version-a",
                                "author_id": "owner-a"}),))
                db.commit()
            self.assertTrue(certificate_allowed(host("project-a", "version-a", "c"), origin, database))
            self.assertFalse(certificate_allowed(host("project-a", "version-a", "x"), origin, database))
            self.assertFalse(certificate_allowed(host("project-b", "version-a"), origin, database))
            self.assertFalse(certificate_allowed(host("project-a", "version-b"), origin, database))
            self.assertFalse(certificate_allowed(host("project-a", "version-a") + ".evil.com",
                                                 origin, database))
            with closing(sqlite3.connect(database)) as db:
                db.execute("UPDATE aggregate_version_builds SET payload=? WHERE id='version-a'",
                           (json.dumps({"build_status": "failed", "artifact": None}),))
                db.commit()
            self.assertFalse(certificate_allowed(host("project-a", "version-a"), origin, database))
            self.assertFalse(certificate_allowed(host("project-a", "version-a", "c"), origin, database))

    def test_loopback_permission_endpoint_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "missing.db"
            from http.server import ThreadingHTTPServer
            server = ThreadingHTTPServer(("127.0.0.1", 0),
                handler_type("https://preview.example.com", database))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
                connection.request("GET", "/allow?domain=" + quote(host("project-a", "version-a")))
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                response.read()
                self.assertFalse(database.exists())
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
