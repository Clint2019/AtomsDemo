"""A separate preview-origin HTTP surface for built artifacts and application data."""
from __future__ import annotations

import asyncio
import io
import json
import mimetypes
import ipaddress
import re
import secrets
import zipfile
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from atoms_demo.api.application import Application
from atoms_demo.api.serialization import encode_response
from atoms_demo.config import Settings
from atoms_demo.contracts.app_resources import AppRecord
from atoms_demo.contracts.preview import RuntimeIssue
from atoms_demo.core.errors import DomainError, ErrorCode
from atoms_demo.core.preview_origin import project_preview_origin, version_preview_origin
from atoms_demo.core.types import EntityId, Environment, Mutation, PageQuery, RequestContext

_MAX_FILES = 1000
_MAX_UNCOMPRESSED = 50_000_000
_SAFE_PATH = re.compile(r"[A-Za-z0-9_./@+~-]+")


def _asset(artifact: bytes, requested_path: str) -> tuple[bytes, str]:
    path = requested_path.lstrip("/") or "index.html"
    if (not _SAFE_PATH.fullmatch(path) or path.startswith("/") or
            any(part in {"", ".", ".."} for part in path.split("/"))):
        raise DomainError(ErrorCode.NOT_FOUND, "Preview file not found")
    fallback = False
    try:
        with zipfile.ZipFile(io.BytesIO(artifact)) as archive:
            files = archive.infolist()
            if (len(files) > _MAX_FILES or
                    sum(item.file_size for item in files) > _MAX_UNCOMPRESSED or
                    any(item.file_size > _MAX_UNCOMPRESSED for item in files)):
                raise DomainError(ErrorCode.INVALID_INPUT, "Preview artifact exceeds limits")
            for item in files:
                name = item.filename.rstrip("/")
                if (name and (not _SAFE_PATH.fullmatch(name) or
                    any(part in {"", ".", ".."} for part in name.split("/"))) or
                    ((item.external_attr >> 16) & 0o170000) == 0o120000):
                    raise DomainError(ErrorCode.INVALID_INPUT, "Unsafe preview artifact")
            try:
                content = archive.read(path)
            except KeyError:
                if "." in path.rsplit("/", 1)[-1]:
                    raise DomainError(ErrorCode.NOT_FOUND, "Preview file not found") from None
                content = archive.read("index.html")
                fallback = True
    except (zipfile.BadZipFile, RuntimeError, KeyError):
        raise DomainError(ErrorCode.INVALID_INPUT, "Preview artifact is not a valid site") from None
    media_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    if path == "index.html" or fallback or media_type == "text/html":
        media_type = "text/html; charset=utf-8"
    return content, media_type


def handler_type(settings: Settings, application: Application) -> type[BaseHTTPRequestHandler]:
    origin = urlsplit(settings.preview_origin)
    try:
        ip_mode = (settings.public_ip_test_mode and
                   ipaddress.ip_address(origin.hostname or "").version == 4)
    except ValueError:
        ip_mode = False
    port_suffix = f":{origin.port}" if origin.port else ""
    host_pattern = re.compile(
        rf"(?:v|c)-[0-9a-f]{{32}}\.p-[0-9a-f]{{32}}\.{re.escape(origin.hostname)}{re.escape(port_suffix)}",
        re.IGNORECASE)
    secure = "; Secure" if origin.scheme == "https" else ""

    def app_cookie_name(environment: Environment) -> str:
        return "app_session_public_preview" if environment == Environment.PUBLIC_PREVIEW else "app_session"

    class PreviewHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            # Preview and share URLs carry bearer tokens; default access logs
            # include the complete path and would disclose them to operators.
            return

        def _assert_project_host(self, grant) -> None:
            expected = urlsplit(version_preview_origin(settings.preview_origin,
                grant.project_id, grant.version_id, grant.environment)).netloc
            if self.headers.get("Host", "").lower() != expected.lower():
                raise DomainError(ErrorCode.FORBIDDEN, "Preview project host mismatch")

        def _write(self, status: int, content: bytes, media_type: str,
                   cookies: tuple[str, ...] = (), location: str | None = None,
                   csp: str | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", media_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", csp or (
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; "
                f"object-src 'none'; base-uri 'none'; frame-ancestors {settings.public_origin}"))
            if ip_mode and self.headers.get("Origin") == "null":
                self.send_header("Access-Control-Allow-Origin", "null")
                self.send_header("Vary", "Origin")
            if location:
                self.send_header("Location", location)
            for cookie in cookies:
                self.send_header("Set-Cookie", cookie)
            try:
                self.end_headers()
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                self.close_connection = True

        def _error(self, error: DomainError) -> None:
            status = {ErrorCode.INVALID_INPUT: 422, ErrorCode.UNAUTHENTICATED: 401,
                ErrorCode.FORBIDDEN: 403, ErrorCode.NOT_FOUND: 404,
                ErrorCode.CONFLICT: 409}.get(error.code, 500)
            self._write(status, encode_response({"code": error.code.value,
                "message": error.message}), "application/json; charset=utf-8")

        def _cookies(self) -> SimpleCookie:
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
            except Exception:
                pass
            return cookie

        async def _app_context(self):
            cookies = self._cookies()
            ticket = cookies.get("preview_ticket")
            if ticket is None:
                raise DomainError(ErrorCode.UNAUTHENTICATED, "Preview session required")
            grant = await application.services.preview.resolve_ticket(ticket.value)
            self._assert_project_host(grant)
            app_session = cookies.get(app_cookie_name(grant.environment))
            if app_session is None:
                raise DomainError(ErrorCode.UNAUTHENTICATED, "Preview session required")
            context = await application.services.app_resources.authenticate_app(app_session.value)
            if context.project_id != grant.project_id or context.environment != grant.environment:
                raise DomainError(ErrorCode.FORBIDDEN, "Application session scope differs")
            return context

        async def _ip_app_context(self, token: str):
            grant = await application.services.preview.resolve_ticket(token)
            session_id = self.headers.get("X-Preview-Session", "")
            if not session_id:
                raise DomainError(ErrorCode.UNAUTHENTICATED, "Preview session required")
            context = await application.services.app_resources.authenticate_app(session_id)
            if context.project_id != grant.project_id or context.environment != grant.environment:
                raise DomainError(ErrorCode.FORBIDDEN, "Application session scope differs")
            return context

        def _ip_csp(self, token: str, nonce: str | None = None) -> str:
            base = f"{settings.preview_origin}/p/{token}/"
            scripts = f"'nonce-{nonce}' {base}" if nonce else base
            return ("sandbox allow-scripts allow-forms; default-src 'none'; "
                    f"script-src {scripts}; style-src 'unsafe-inline' {base}; "
                    f"img-src {base} data:; font-src {base} data:; "
                    f"connect-src {base}; object-src 'none'; base-uri 'none'; "
                    f"frame-ancestors {settings.public_origin}")

        def _ip_html(self, content: bytes, token: str, session_id: str,
                     nonce: str) -> bytes:
            # Generated applications use root-relative /app URLs. The bridge keeps
            # every request inside this ticket's path and sends a scoped session.
            script = ("<script nonce=\"" + nonce + "\">" +
                "(()=>{const prefix=" + json.dumps(f"/p/{token}") + ";" +
                "const session=" + json.dumps(session_id) + ";" +
                "const nativeFetch=window.fetch.bind(window);" +
                "window.fetch=(input,init={})=>{const raw=typeof input==='string'?input:input.url;" +
                "if(!raw.startsWith('/app/'))return nativeFetch(input,init);" +
                "const headers=new Headers(init.headers || (input instanceof Request?input.headers:undefined));" +
                "headers.set('X-Preview-Session',session);" +
                "return nativeFetch(prefix+raw,{...init,headers,credentials:'omit'});};" +
                "})();</script>")
            match = re.search(br"<head(?:\s[^>]*)?>", content, re.IGNORECASE)
            if match is None:
                raise DomainError(ErrorCode.INVALID_INPUT, "Preview HTML has no head")
            return content[:match.end()] + script.encode("utf-8") + content[match.end():]

        async def _dispatch_ip(self):
            path = urlsplit(self.path).path
            share = re.fullmatch(r"/s/([A-Za-z0-9_-]{1,128})/?", path)
            if self.command == "GET" and share:
                ticket = await application.services.preview.resolve_share(share.group(1))
                self._write(302, b"", "text/plain", location=ticket.url)
                return
            match = re.fullmatch(r"/p/([A-Za-z0-9_-]{1,128})(?:/(.*))?", path)
            if match is None:
                raise DomainError(ErrorCode.NOT_FOUND, "Preview route not found")
            token, tail = match.group(1), match.group(2) or ""
            if self.command == "OPTIONS" and tail.startswith("app/"):
                await application.services.preview.resolve_ticket(token)
                if self.headers.get("Origin") != "null":
                    raise DomainError(ErrorCode.FORBIDDEN, "Preview origin mismatch")
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "null")
                self.send_header("Access-Control-Allow-Methods", "GET, PUT, DELETE, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, Idempotency-Key, X-Preview-Session")
                self.send_header("Access-Control-Max-Age", "600")
                self.send_header("Vary", "Origin")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if tail.startswith("app/"):
                context = await self._ip_app_context(token)
                await self._dispatch_app("/" + tail, context)
                return
            if self.command != "GET":
                raise DomainError(ErrorCode.NOT_FOUND, "Preview route not found")
            artifact = await application.services.preview.serve_artifact(token)
            content, media_type = _asset(artifact, tail)
            if media_type.startswith("text/html"):
                session = await application.preview.app_session(token, None)
                nonce = secrets.token_urlsafe(16)
                content = self._ip_html(content, token, str(session.id), nonce)
                self._write(200, content, media_type, csp=self._ip_csp(token, nonce))
            else:
                self._write(200, content, media_type, csp=self._ip_csp(token))

        async def _dispatch_app(self, path: str, context) -> None:
            if self.command == "GET" and re.fullmatch(r"/app/collections/[a-z][a-z0-9_]{0,62}/records", path):
                query = parse_qs(urlsplit(self.path).query)
                try:
                    limit = int(query.get("limit", ["20"])[0])
                except ValueError:
                    raise DomainError(ErrorCode.INVALID_INPUT, "Invalid page limit") from None
                result = await application.services.app_resources.records(context,
                    path.split("/")[3], PageQuery(cursor=query.get("cursor", [None])[0], limit=limit))
                self._write(200, encode_response(result), "application/json; charset=utf-8")
                return
            if self.command == "PUT" and re.fullmatch(r"/app/records/[A-Za-z0-9_-]{1,128}", path):
                body = self._read_body()
                document = body.get("json_document")
                if not isinstance(document, str) or not isinstance(body.get("collection"), str):
                    raise DomainError(ErrorCode.INVALID_INPUT, "Invalid application record")
                result = await application.services.app_resources.put_record(context, AppRecord(
                    id=EntityId(path.rsplit("/", 1)[-1]), collection=body["collection"],
                    json_document=document, schema_version=body.get("schema_version"),
                    revision=body.get("revision", 0)), self._mutation())
                self._write(200, encode_response(result), "application/json; charset=utf-8")
                return
            if self.command == "DELETE" and re.fullmatch(r"/app/records/[A-Za-z0-9_-]{1,128}", path):
                await application.services.app_resources.delete_record(context,
                    EntityId(path.rsplit("/", 1)[-1]), self._mutation())
                self._write(204, b"", "application/json; charset=utf-8")
                return
            raise DomainError(ErrorCode.NOT_FOUND, "Application route not found")

        def _read_body(self) -> dict:
            length = self.headers.get("Content-Length", "0")
            if not length.isdigit() or int(length) > settings.max_request_bytes:
                raise DomainError(ErrorCode.INVALID_INPUT, "Invalid request size")
            if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                raise DomainError(ErrorCode.INVALID_INPUT, "Expected JSON")
            try:
                body = json.loads(self.rfile.read(int(length)))
            except (ValueError, UnicodeDecodeError):
                raise DomainError(ErrorCode.INVALID_INPUT, "Invalid JSON") from None
            if not isinstance(body, dict):
                raise DomainError(ErrorCode.INVALID_INPUT, "JSON object required")
            return body

        def _mutation(self) -> Mutation:
            key = self.headers.get("Idempotency-Key", "")
            if not key or len(key) > 128:
                raise DomainError(ErrorCode.INVALID_INPUT, "Idempotency-Key required")
            return Mutation(idempotency_key=key)

        async def _dispatch(self):
            path = urlsplit(self.path).path
            if self.command == "GET" and path.startswith("/share/"):
                token = path.removeprefix("/share/")
                if not token or "/" in token:
                    raise DomainError(ErrorCode.NOT_FOUND, "Share not found")
                ticket = await application.services.preview.resolve_share(token)
                grant = await application.services.preview.resolve_ticket(
                    ticket.url.rsplit("/", 1)[-1])
                self._assert_project_host(grant)
                self._write(302, b"", "text/plain", location=ticket.url)
                return
            if self.command == "GET" and path.startswith("/preview/"):
                token = path.removeprefix("/preview/")
                if not token or "/" in token:
                    raise DomainError(ErrorCode.NOT_FOUND, "Preview not found")
                grant = await application.services.preview.resolve_ticket(token)
                self._assert_project_host(grant)
                artifact = await application.services.preview.serve_artifact(token)
                cookie_name = app_cookie_name(grant.environment)
                previous = self._cookies().get(cookie_name)
                session = await application.preview.app_session(token,
                    previous.value if previous else None)
                content, media_type = _asset(artifact, "index.html")
                project_cookie_domain = urlsplit(project_preview_origin(
                    settings.preview_origin, grant.project_id)).hostname
                self._write(200, content, media_type, (
                    f"preview_ticket={token}; Path=/; HttpOnly; SameSite=Lax{secure}",
                    f"{cookie_name}={session.id}; Domain={project_cookie_domain}; Path=/; HttpOnly; SameSite=Lax{secure}"))
                return
            if path.startswith("/app/"):
                context = await self._app_context()
                await self._dispatch_app(path, context)
                return
            if self.command == "POST" and path == "/api/v1/preview/issues":
                cookies = self._cookies()
                ticket = cookies.get("preview_ticket")
                if ticket is None:
                    raise DomainError(ErrorCode.UNAUTHENTICATED, "Preview ticket required")
                body = self._read_body()
                grant = await application.services.preview.resolve_ticket(ticket.value)
                self._assert_project_host(grant)
                message = body.get("message")
                stack = body.get("stack")
                if not isinstance(message, str) or (stack is not None and not isinstance(stack, str)):
                    raise DomainError(ErrorCode.INVALID_INPUT, "Invalid preview issue")
                receipt = await application.services.preview.report_issue(
                    RequestContext(actor_id=None, session_id=None, workspace_id=None,
                                   request_id="preview-runtime"), RuntimeIssue(
                        preview_token=ticket.value, project_id=grant.project_id,
                        version_id=grant.version_id, message=message,
                        stack=stack, source_origin=version_preview_origin(
                            settings.preview_origin, grant.project_id, grant.version_id,
                            grant.environment)))
                self._write(201, encode_response(receipt), "application/json; charset=utf-8")
                return
            if self.command == "GET":
                ticket = self._cookies().get("preview_ticket")
                if ticket is None:
                    raise DomainError(ErrorCode.UNAUTHENTICATED, "Preview ticket required")
                grant = await application.services.preview.resolve_ticket(ticket.value)
                self._assert_project_host(grant)
                artifact = await application.services.preview.serve_artifact(ticket.value)
                content, media_type = _asset(artifact, path)
                self._write(200, content, media_type)
                return
            raise DomainError(ErrorCode.NOT_FOUND, "Preview route not found")

        def _handle(self) -> None:
            host = self.headers.get("Host", "").lower()
            if ip_mode:
                if host != origin.netloc.lower():
                    self._error(DomainError(ErrorCode.FORBIDDEN, "Preview host mismatch"))
                    return
                if self.headers.get("Origin") not in {None, "null"}:
                    self._error(DomainError(ErrorCode.FORBIDDEN, "Preview origin mismatch"))
                    return
                try:
                    asyncio.run(self._dispatch_ip())
                except DomainError as error:
                    self._error(error)
                except (ValueError, TypeError, KeyError):
                    self._error(DomainError(ErrorCode.INVALID_INPUT, "Invalid preview request"))
                except Exception:
                    self._error(DomainError(ErrorCode.INTERNAL, "Preview failed"))
                return
            if not host_pattern.fullmatch(host):
                self._error(DomainError(ErrorCode.FORBIDDEN, "Preview host mismatch"))
                return
            request_origin = f"{origin.scheme}://{host}"
            if self.headers.get("Origin") not in {None, request_origin}:
                self._error(DomainError(ErrorCode.FORBIDDEN, "Preview origin mismatch"))
                return
            try:
                asyncio.run(self._dispatch())
            except DomainError as error:
                self._error(error)
            except (ValueError, TypeError, KeyError):
                self._error(DomainError(ErrorCode.INVALID_INPUT, "Invalid preview request"))
            except Exception:
                self._error(DomainError(ErrorCode.INTERNAL, "Preview failed"))

        do_GET = _handle
        do_POST = _handle
        do_PUT = _handle
        do_DELETE = _handle
        do_OPTIONS = _handle

    return PreviewHandler


def create_server(application: Application, settings: Settings) -> ThreadingHTTPServer:
    origin = urlsplit(settings.preview_origin)
    if origin.scheme == "http" and not settings.public_ip_test_mode and not (
            origin.hostname == "localhost" or origin.hostname == "127.0.0.1" or
            origin.hostname.endswith(".localhost")):
        raise ValueError("Public HTTP preview origin must be local")
    if origin.scheme == "https" and settings.preview_bind_port is None:
        raise ValueError("HTTPS preview requires a dedicated loopback bind port")
    if settings.preview_bind_host != "127.0.0.1":
        raise ValueError("Preview runtime must listen on loopback")
    bind_port = settings.preview_bind_port or origin.port or 80
    if settings.preview_bind_host == settings.host and bind_port == settings.port:
        raise ValueError("Preview runtime and platform API cannot share a bind port")
    return ThreadingHTTPServer((settings.preview_bind_host, bind_port),
                               handler_type(settings, application))
