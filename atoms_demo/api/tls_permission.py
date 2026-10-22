"""Loopback-only certificate permission check for public preview hosts.

Caddy asks this process before an on-demand TLS certificate is obtained. The
database is opened read-only, and no preview ticket or application secret is
exposed through this endpoint.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from atoms_demo.config import database_path, load_settings


def preview_host_parts(domain: str, base_origin: str) -> tuple[str, str, str] | None:
    """Return the kind, version and project hash labels for an exact preview host."""
    base = urlsplit(base_origin).hostname
    if base is None or domain != domain.lower():
        return None
    match = re.fullmatch(
        rf"(v|c)-([0-9a-f]{{32}})\.p-([0-9a-f]{{32}})\.{re.escape(base)}",
        domain,
    )
    return match.groups() if match else None


def certificate_allowed(domain: str, base_origin: str, database: Path) -> bool:
    """Allow only an existing project's successfully built version hostname."""
    parts = preview_host_parts(domain, base_origin)
    if parts is None or not database.is_file():
        return False
    kind, version_hash, project_hash = parts
    try:
        # mode=ro prevents a TLS handshake from creating or migrating a DB.
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro",
                                     uri=True, timeout=2)) as db:
            db.row_factory = sqlite3.Row
            for row in db.execute("SELECT id, payload FROM aggregate_version_locators"):
                if hashlib.sha256(row["id"].encode()).hexdigest()[:32] != version_hash:
                    continue
                locator = json.loads(row["payload"])
                project_id = locator["target_project_id"]
                workspace_id = locator["target_workspace_id"]
                if hashlib.sha256(project_id.encode()).hexdigest()[:32] != project_hash:
                    return False
                project = db.execute(
                    "SELECT payload FROM aggregate_projects WHERE id=? AND workspace_id=?",
                    (project_id, workspace_id),
                ).fetchone()
                build = db.execute(
                    "SELECT payload FROM aggregate_version_builds WHERE id=?",
                    (row["id"],),
                ).fetchone()
                if not project or not build:
                    return False
                status = json.loads(build["payload"])
                if status.get("build_status") != "success" or not status.get("artifact"):
                    return False
                if kind == "v":
                    return True
                project_data = json.loads(project["payload"])
                if project_data.get("visibility") != "public":
                    return False
                for entry in db.execute("SELECT payload FROM aggregate_catalog_entries"):
                    catalog = json.loads(entry["payload"])
                    if (catalog.get("target_project_id") == project_id and
                            catalog.get("version_id") == row["id"] and
                            catalog.get("author_id") == project_data.get("owner_id")):
                        return True
                return False
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        return False
    return False


def handler_type(base_origin: str, database: Path) -> type[BaseHTTPRequestHandler]:
    class PermissionHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query, keep_blank_values=True)
            domains = query.get("domain", [])
            allowed = (parsed.path == "/allow" and len(domains) == 1 and
                       len(domains[0]) <= 253 and
                       certificate_allowed(domains[0], base_origin, database))
            self.send_response(200 if allowed else 403)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

    return PermissionHandler


def main() -> None:
    settings = load_settings()
    if urlsplit(settings.preview_origin).scheme != "https":
        raise ValueError("TLS permission service requires an HTTPS preview origin")
    port = int(os.environ.get("ATOMS_TLS_PERMISSION_PORT", "8133"))
    if not 1 <= port <= 65535:
        raise ValueError("ATOMS_TLS_PERMISSION_PORT is invalid")
    server = ThreadingHTTPServer(("127.0.0.1", port),
        handler_type(settings.preview_origin, database_path(settings)))
    server.serve_forever()


if __name__ == "__main__":
    main()
