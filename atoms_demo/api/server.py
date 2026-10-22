"""Versioned HTTP transport. Business handlers are attached in later tasks."""
import asyncio
import base64
import hashlib
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass, replace
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4
from urllib.parse import urlsplit, parse_qs, quote

from atoms_demo.api.application import Application
from atoms_demo.api.routes import ROUTES
from atoms_demo.api.serialization import encode_error, encode_response
from atoms_demo.config import Settings
from atoms_demo.core.errors import DomainError, ErrorCode, ErrorResponse, FeatureNotImplemented
from atoms_demo.core.types import BlobRef, EntityId, Environment, Mutation, PageQuery, RequestContext, Scope
from atoms_demo.contracts.identity import Credentials, OAuthCallback, VerificationRequest, Session
from atoms_demo.contracts.workspaces import Role
from atoms_demo.contracts.profiles import Profile, Preferences
from atoms_demo.contracts.projects import CreateProject, ProjectQuery, Visibility
from atoms_demo.contracts.storage import Upload
from atoms_demo.contracts.conversations import AppendMessage, MessageRole
from atoms_demo.contracts.billing import Money, AccountKind
from atoms_demo.contracts.connectors import ConnectionCallback
from atoms_demo.contracts.versions import EditVersion, FileEdit
from atoms_demo.contracts.app_resources import AppResource, ResourceKind
from atoms_demo.contracts.catalog import CatalogEntry, CatalogQuery, ContentKind
from atoms_demo.workflows.generation import GenerateApplication
from atoms_demo.workflows.project_lifecycle import CloneProject
from atoms_demo.contracts.versions import PromoteVersion
from atoms_demo.contracts.publication import (ManualDeploymentResult,
    PublicationConfig, PublicationState)
from atoms_demo.contracts.tasks import TaskState


@dataclass(frozen=True)
class RawResponse:
    body: bytes
    content_type: str
    attachment: bool = True
    filename: str | None = None


def attachment_disposition(filename: str | None) -> str:
    name = re.sub(r'[\x00-\x1f\x7f/\\<>:"|?*]', "_", filename or "download").strip(" .")[:180]
    name = name or "download"
    fallback = re.sub(r"[^A-Za-z0-9._-]", "_", name.encode("ascii", "ignore").decode())
    if fallback.startswith("."):
        fallback = "download" + fallback
    fallback = fallback.strip(" ._") or "download"
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(name, safe="")}'

_ROUTES = tuple((route, re.compile("^/api/v1" + re.sub(r"\{[^/]+\}", r"[^/]+", route.path) + "$"))
                for route in ROUTES)


def handler_type(settings: Settings, application: Application | None = None) -> type[BaseHTTPRequestHandler]:
    stream_limit = threading.BoundedSemaphore(16)
    stream_lock = threading.Lock()
    active_sessions: dict[str, int] = {}

    class Handler(BaseHTTPRequestHandler):
        def _write(self, status: int, payload: bytes, request_id: str,
                   cookies: tuple[str, ...] = (), content_type: str = "application/json; charset=utf-8",
                   attachment: bool = False, filename: str | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-Request-ID", request_id)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if attachment:
                self.send_header("Content-Disposition", attachment_disposition(filename))
            origin = self.headers.get("Origin")
            if origin == settings.public_origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Credentials", "true")
                self.send_header("Vary", "Origin")
            for cookie in cookies:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(payload)

        def _error(self, status: int, code: ErrorCode, message: str, request_id: str) -> None:
            self._write(status, encode_error(ErrorResponse(code=code, message=message, request_id=request_id)), request_id)

        def _cookies(self) -> SimpleCookie:
            cookies = SimpleCookie()
            try:
                cookies.load(self.headers.get("Cookie", ""))
            except Exception:
                pass
            return cookies

        def _mutation(self, body: dict) -> Mutation:
            key = self.headers.get("Idempotency-Key", "").strip()
            if not key or len(key) > 128:
                raise DomainError(ErrorCode.INVALID_INPUT, "Idempotency-Key header is required")
            revision = body.get("expected_revision")
            if revision is not None and (type(revision) is not int or revision < 0):
                raise DomainError(ErrorCode.INVALID_INPUT, "Invalid expected revision")
            return Mutation(idempotency_key=key, expected_revision=revision)

        @staticmethod
        def _string(body: dict, name: str) -> str:
            value = body.get(name)
            if not isinstance(value, str):
                raise DomainError(ErrorCode.INVALID_INPUT, f"{name} must be a string")
            return value

        @staticmethod
        def _boolean(body: dict, name: str) -> bool:
            value = body.get(name)
            if not isinstance(value, bool):
                raise DomainError(ErrorCode.INVALID_INPUT, f"{name} must be a boolean")
            return value

        def _page(self) -> PageQuery:
            query = parse_qs(urlsplit(self.path).query)
            try:
                limit = int(query.get("limit", ["20"])[0])
            except ValueError:
                raise DomainError(ErrorCode.INVALID_INPUT, "Invalid page limit") from None
            if not 1 <= limit <= 100:
                raise DomainError(ErrorCode.INVALID_INPUT, "Invalid page limit")
            return PageQuery(cursor=query.get("cursor", [None])[0], limit=limit)

        def _session_cookies(self, session: Session) -> tuple[str, ...]:
            secure = "; Secure" if settings.public_origin.startswith("https://") else ""
            csrf = secrets.token_urlsafe(32)
            return (f"session={session.id}; Path=/; HttpOnly; SameSite=Lax{secure}",
                    f"csrf_token={csrf}; Path=/; SameSite=Lax{secure}")

        def _stream_task_events(self, ctx: RequestContext, path: str,
                                request_id: str) -> None:
            assert application is not None
            task_id = EntityId(path.split("/")[4])
            query = parse_qs(urlsplit(self.path).query)
            raw_after = self.headers.get("Last-Event-ID") or query.get("after", ["0"])[0]
            if not raw_after.isdigit() or len(raw_after) > 18:
                raise DomainError(ErrorCode.INVALID_INPUT, "Invalid event cursor")
            cursor = int(raw_after)
            # Authorize before writing headers so invalid sessions receive HTTP errors.
            asyncio.run(application.services.tasks.events(ctx, task_id, cursor))
            if ctx.session_id is None or not stream_limit.acquire(blocking=False):
                raise DomainError(ErrorCode.QUOTA_EXCEEDED, "Too many event streams")
            session_key = str(ctx.session_id)
            with stream_lock:
                if active_sessions.get(session_key, 0) >= 2:
                    stream_limit.release()
                    raise DomainError(ErrorCode.QUOTA_EXCEEDED, "Too many session event streams")
                active_sessions[session_key] = active_sessions.get(session_key, 0) + 1
            try:
                self.connection.settimeout(5)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache, no-transform")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("X-Request-ID", request_id)
                self.send_header("X-Content-Type-Options", "nosniff")
                origin = self.headers.get("Origin")
                if origin == settings.public_origin:
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Access-Control-Allow-Credentials", "true")
                    self.send_header("Vary", "Origin")
                self.end_headers()
                self.close_connection = True
                deadline = time.monotonic() + 25
                next_heartbeat = time.monotonic() + 10
                terminal_event_seen = False
                terminal_since = None
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while time.monotonic() < deadline:
                    fresh = asyncio.run(self._context(request_id))
                    if (fresh.actor_id != ctx.actor_id or fresh.session_id != ctx.session_id or
                            fresh.workspace_id != ctx.workspace_id):
                        return
                    pending = asyncio.run(application.services.tasks.events(fresh, task_id, cursor))
                    for event in pending:
                        if event.sequence <= cursor:
                            continue
                        self.wfile.write(b"id: " + str(event.sequence).encode() + b"\n" +
                            b"event: " + event.kind.encode("ascii") + b"\n" +
                            b"data: " + encode_response(event) + b"\n\n")
                        cursor = event.sequence
                        terminal_event_seen |= event.kind in {"succeeded", "failed"}
                    if pending:
                        self.wfile.flush()
                    task = asyncio.run(application.services.tasks.get(fresh, task_id))
                    if task.state in {TaskState.SUCCEEDED, TaskState.FAILED} and terminal_event_seen:
                        return
                    if task.state in {TaskState.SUCCEEDED, TaskState.FAILED,
                                      TaskState.CANCELLED, TaskState.INTERRUPTED}:
                        terminal_since = terminal_since or time.monotonic()
                        if (task.state in {TaskState.CANCELLED, TaskState.INTERRUPTED} or
                                time.monotonic() - terminal_since >= 2):
                            # A crash can leave the authoritative terminal state without
                            # its best-effort event. Tell clients to stop reconnecting.
                            self.wfile.write(b"event: terminal\n" + b"data: " +
                                json.dumps({"state": task.state.value,
                                            "last_sequence": cursor}).encode() + b"\n\n")
                            self.wfile.flush()
                            return
                    else:
                        terminal_since = None
                    if time.monotonic() >= next_heartbeat:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        next_heartbeat = time.monotonic() + 10
                    time.sleep(1)
            except (BrokenPipeError, ConnectionResetError, OSError, DomainError):
                return
            finally:
                with stream_lock:
                    active_sessions[session_key] -= 1
                    if active_sessions[session_key] == 0:
                        del active_sessions[session_key]
                stream_limit.release()

        async def _context(self, request_id: str) -> RequestContext:
            assert application is not None
            cookies = self._cookies()
            token = cookies.get("session")
            binding = cookies.get("oauth_client")
            binding_value = binding.value if binding else None
            if token is None:
                return RequestContext(actor_id=None, session_id=None, workspace_id=None,
                                      request_id=request_id, client_binding=binding_value)
            try:
                ctx = await application.services.identity.authenticate(token.value)
            except DomainError:
                return RequestContext(actor_id=None, session_id=None, workspace_id=None,
                                      request_id=request_id, client_binding=binding_value)
            workspace_id = await application.services.workspaces.selected_workspace(ctx.session_id)
            return replace(ctx, workspace_id=workspace_id, request_id=request_id, client_binding=binding_value)

        async def _dispatch(self, route, path: str, body: dict, ctx: RequestContext,
                            raw_body: bytes = b""):
            assert application is not None
            services = application.services
            operation = (route.owner, route.operation)
            if operation == ("identity", "me"):
                return 200, await services.identity.me(ctx), ()
            if operation == ("identity", "check_email"):
                return 200, await services.identity.check_email(ctx, self._string(body, "email")), ()
            if operation == ("identity", "register"):
                credentials = Credentials(email=self._string(body, "email"), password=self._string(body, "password"),
                                          captcha_token=body.get("captcha_token"),
                                          accepted_terms=self._boolean(body, "accepted_terms"))
                return 201, await services.identity.register(ctx, credentials), ()
            if operation == ("identity", "login"):
                credentials = Credentials(email=self._string(body, "email"), password=self._string(body, "password"),
                                          captcha_token=body.get("captcha_token"),
                                          accepted_terms=body.get("accepted_terms", False))
                session = await services.identity.login(ctx, credentials)
                return 200, {"user_id": session.user_id, "expires_at": session.expires_at.isoformat()}, self._session_cookies(session)
            if operation == ("identity", "request_verification"):
                command = VerificationRequest(email=self._string(body, "email"), mutation=self._mutation(body))
                return 200, await services.identity.request_verification(ctx, command), ()
            if operation == ("identity", "verify"):
                session = await services.identity.verify(ctx, self._string(body, "token"))
                return 200, {"user_id": session.user_id, "expires_at": session.expires_at.isoformat()}, self._session_cookies(session)
            if operation == ("identity", "oauth_start"):
                provider = path.rsplit("/", 1)[-1]
                binding = ctx.client_binding or secrets.token_urlsafe(32)
                url = await services.identity.oauth_start(replace(ctx, client_binding=binding), provider)
                secure = "; Secure" if settings.public_origin.startswith("https://") else ""
                return 200, {"url": url}, (f"oauth_client={binding}; Path=/; Max-Age=600; HttpOnly; SameSite=Lax{secure}",)
            if operation == ("identity", "oauth_callback"):
                session = await services.identity.oauth_callback(ctx, OAuthCallback(
                    provider=self._string(body, "provider"), code=self._string(body, "code"),
                    state=self._string(body, "state")))
                return 200, {"user_id": session.user_id, "expires_at": session.expires_at.isoformat()}, (
                    *self._session_cookies(session), "oauth_client=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
            if operation == ("identity", "logout"):
                await services.identity.logout(ctx)
                return 204, None, ("session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax",
                                   "csrf_token=; Path=/; Max-Age=0; SameSite=Lax")
            if operation == ("identity", "change_email"):
                return 200, await services.identity.change_email(ctx, self._string(body, "email"), self._mutation(body)), ()
            if operation == ("workspaces", "create"):
                return 201, await services.workspaces.create(ctx, self._string(body, "name"), self._mutation(body)), ()
            if operation == ("workspaces", "list_workspaces"):
                return 200, await services.workspaces.list_workspaces(ctx, self._page()), ()
            if operation == ("workspaces", "select"):
                workspace_id = EntityId(path.split("/")[4])
                return 200, await services.workspaces.select(ctx, workspace_id), ()
            if operation == ("workspaces", "members"):
                return 200, await services.workspaces.members(ctx, self._page()), ()
            if operation == ("workspaces", "invite"):
                return 201, await services.workspaces.invite(ctx, self._string(body, "email"), Role(self._string(body, "role")),
                                                              self._mutation(body)), ()
            if operation == ("workspaces", "accept_invitation"):
                return 201, await services.workspaces.accept_invitation(ctx, self._string(body, "token")), ()
            if operation == ("workspaces", "change_role"):
                member_id = EntityId(path.rsplit("/", 1)[-1])
                return 200, await services.workspaces.change_role(ctx, member_id, Role(self._string(body, "role")),
                                                                   self._mutation(body)), ()
            if operation == ("workspaces", "remove_member"):
                member_id = EntityId(path.rsplit("/", 1)[-1])
                await services.workspaces.remove_member(ctx, member_id, self._mutation(body))
                return 204, None, ()
            if operation == ("profiles", "get"):
                return 200, await services.profiles.get(ctx, EntityId(path.rsplit("/", 1)[-1])), ()
            if operation == ("profiles", "avatar"):
                avatar = await services.profiles.avatar(ctx, EntityId(path.split("/")[4]))
                return 200, RawResponse(avatar.content, avatar.media_type, False), ()
            if operation == ("profile", "set_avatar"):
                return 200, await application.profile.set_avatar(ctx,
                    EntityId(self._string(body, "object_id")), self._mutation(body)), ()
            if operation == ("profiles", "update"):
                if ctx.actor_id is None:
                    raise DomainError(ErrorCode.UNAUTHENTICATED, "Sign in required")
                if body.get("user_id", str(ctx.actor_id)) != str(ctx.actor_id):
                    raise DomainError(ErrorCode.FORBIDDEN, "Cannot edit another profile")
                if body.get("avatar") is not None:
                    raise FeatureNotImplemented("Avatar upload requires T03 storage integration")
                profile = Profile(user_id=ctx.actor_id, display_name=self._string(body, "display_name"),
                                  avatar=None, public_slug=self._string(body, "public_slug"),
                                  revision=body.get("revision", 0))
                return 200, await services.profiles.update(ctx, profile, self._mutation(body)), ()
            if operation == ("profiles", "preferences"):
                return 200, await services.profiles.preferences(ctx), ()
            if operation == ("profiles", "save_preferences"):
                if ctx.actor_id is None:
                    raise DomainError(ErrorCode.UNAUTHENTICATED, "Sign in required")
                preferences = Preferences(user_id=ctx.actor_id, locale=self._string(body, "locale"),
                                          default_model=self._string(body, "default_model"),
                                          default_visibility=self._string(body, "default_visibility"),
                                          credit_reminders=self._boolean(body, "credit_reminders"),
                                          sound=self._boolean(body, "sound"),
                                          email_notifications=self._boolean(body, "email_notifications"),
                                          branding=self._boolean(body, "branding"), revision=body.get("revision", 0))
                return 200, await services.profiles.save_preferences(ctx, preferences, self._mutation(body)), ()
            if operation == ("projects", "create"):
                if body.get("source_version_id") is not None:
                    raise DomainError(ErrorCode.INVALID_INPUT, "Use clone workflow for a source version")
                command = CreateProject(name=self._string(body, "name"),
                                        visibility=Visibility(self._string(body, "visibility")),
                                        mutation=self._mutation(body))
                return 201, await services.projects.create(ctx, command), ()
            if operation == ("projects", "list_projects"):
                query = parse_qs(urlsplit(self.path).query)
                favorites = query.get("favorites_only", ["false"])[0]
                if favorites not in {"true", "false"}:
                    raise DomainError(ErrorCode.INVALID_INPUT, "Invalid favorites_only value")
                return 200, await services.projects.list_projects(ctx, ProjectQuery(
                    page=self._page(), favorites_only=favorites == "true")), ()
            if operation == ("projects", "get"):
                return 200, await services.projects.get(ctx, EntityId(path.rsplit("/", 1)[-1])), ()
            if operation == ("projects", "rename"):
                project_id = EntityId(path.split("/")[4])
                return 200, await services.projects.rename(ctx, project_id, self._string(body, "name"),
                                                            self._mutation(body)), ()
            if operation == ("projects", "set_visibility"):
                project_id = EntityId(path.split("/")[4])
                visibility = Visibility(self._string(body, "visibility"))
                return 200, await services.projects.set_visibility(ctx, project_id, visibility,
                                                                    self._mutation(body)), ()
            if operation == ("projects", "set_favorite"):
                project_id = EntityId(path.split("/")[4])
                await services.projects.set_favorite(ctx, project_id, self._boolean(body, "favorite"),
                                                    self._mutation(body))
                return 204, None, ()
            if operation == ("storage", "upload"):
                project_id = body.get("project_id")
                if project_id is not None and not isinstance(project_id, str):
                    raise DomainError(ErrorCode.INVALID_INPUT, "Invalid project_id")
                try:
                    encoded = self._string(body, "content_base64")
                    content = base64.b64decode(encoded, validate=True)
                except (ValueError, base64.binascii.Error):
                    raise DomainError(ErrorCode.INVALID_INPUT, "Invalid base64 content") from None
                upload = Upload(scope=Scope(workspace_id=ctx.workspace_id,
                                      project_id=EntityId(project_id) if project_id else None),
                                purpose=self._string(body, "purpose"),
                                display_name=self._string(body, "display_name"),
                                media_type=self._string(body, "media_type"), content=content,
                                mutation=self._mutation(body))
                return 201, await services.storage.upload(ctx, upload), ()
            if operation == ("storage", "list_objects"):
                query = parse_qs(urlsplit(self.path).query)
                project_id = query.get("project_id", [None])[0]
                return 200, await services.storage.list_objects(ctx, Scope(
                    workspace_id=ctx.workspace_id,
                    project_id=EntityId(project_id) if project_id else None), self._page()), ()
            if operation == ("storage", "get"):
                return 200, await services.storage.get(ctx, EntityId(path.rsplit("/", 1)[-1])), ()
            if operation == ("storage", "download"):
                object_id = EntityId(path.split("/")[5])
                stored = await services.storage.get(ctx, object_id)
                return 200, RawResponse(await services.storage.download(ctx, object_id),
                                        stored.blob.media_type, filename=stored.display_name), ()
            if operation == ("storage", "delete"):
                await services.storage.delete(ctx, EntityId(path.rsplit("/", 1)[-1]), self._mutation(body))
                return 204, None, ()
            if operation == ("storage", "usage"):
                query = parse_qs(urlsplit(self.path).query)
                project_id = query.get("project_id", [None])[0]
                return 200, await services.storage.usage(ctx, EntityId(project_id) if project_id else None), ()
            if operation == ("storage", "set_pay_as_you_go"):
                return 200, await services.storage.set_pay_as_you_go(
                    ctx, self._boolean(body, "enabled"), self._mutation(body)), ()
            if operation == ("conversations", "append"):
                project_id = EntityId(path.split("/")[4])
                if body.get("role", "user") != "user":
                    raise DomainError(ErrorCode.FORBIDDEN, "Only user messages may be submitted")
                attachment_ids = body.get("attachment_ids", [])
                if not isinstance(attachment_ids, list) or not all(isinstance(item, str) for item in attachment_ids):
                    raise DomainError(ErrorCode.INVALID_INPUT, "Invalid attachment_ids")
                return 201, await services.conversations.append(ctx, AppendMessage(
                    project_id=project_id, role=MessageRole.USER,
                    content=self._string(body, "content"),
                    attachment_ids=tuple(EntityId(item) for item in attachment_ids),
                    task_id=None, mutation=self._mutation(body))), ()
            if operation == ("conversations", "history"):
                recent = parse_qs(urlsplit(self.path).query).get("recent", ["false"])[0]
                if recent not in {"true", "false"}:
                    raise DomainError(ErrorCode.INVALID_INPUT, "Invalid recent value")
                history = (services.conversations.history_recent if recent == "true"
                           else services.conversations.history)
                return 200, await history(ctx, EntityId(path.split("/")[4]), self._page()), ()
            if operation == ("conversations", "search"):
                terms = parse_qs(urlsplit(self.path).query).get("q", [])
                if len(terms) != 1:
                    raise DomainError(ErrorCode.INVALID_INPUT, "One message search query is required")
                return 200, await services.conversations.search(
                    ctx, EntityId(path.split("/")[4]), terms[0], self._page()), ()
            if operation == ("versions", "head"):
                return 200, await services.versions.head(ctx, EntityId(path.split("/")[4])), ()
            if operation == ("versions", "list_versions"):
                return 200, await services.versions.list_versions(
                    ctx, EntityId(path.split("/")[4]), self._page()), ()
            if operation == ("versions", "get"):
                return 200, await services.versions.get(ctx, EntityId(path.rsplit("/", 1)[-1])), ()
            if operation == ("versions", "read_file"):
                query = parse_qs(urlsplit(self.path).query)
                file_path = query.get("path", [None])[0]
                if not file_path:
                    raise DomainError(ErrorCode.INVALID_INPUT, "File path required")
                return 200, {"content": await services.versions.read_file(
                    ctx, EntityId(path.split("/")[4]), file_path)}, ()
            if operation == ("versions", "export"):
                version_id = EntityId(path.split("/")[4])
                return 200, RawResponse(await services.versions.download_export(
                    ctx, version_id), "application/zip", filename=f"{version_id}.zip"), ()
            if operation == ("billing", "plans"):
                query = parse_qs(urlsplit(self.path).query)
                return 200, await services.billing.plans(ctx, query.get("interval", ["month"])[0]), ()
            if operation == ("billing", "subscription"):
                return 200, await services.billing.subscription(ctx), ()
            if operation == ("billing", "checkout"):
                return 201, await services.billing.checkout(
                    ctx, EntityId(self._string(body, "plan_id")), self._mutation(body)), ()
            if operation == ("billing", "top_up"):
                amount = Money(minor_units=body.get("minor_units"), currency=self._string(body, "currency"))
                return 201, await services.billing.top_up(ctx, amount, self._mutation(body)), ()
            if operation == ("billing", "balances"):
                return 200, await services.billing.balances(ctx), ()
            if operation == ("billing", "ledger"):
                query = parse_qs(urlsplit(self.path).query)
                account = AccountKind(query.get("account", ["build_credits"])[0])
                project_id = query.get("project_id", [None])[0]
                return 200, await services.billing.ledger(ctx, account, self._page(),
                    EntityId(project_id) if project_id else None), ()
            if operation == ("billing", "handle_webhook"):
                signature = self.headers.get("X-Payment-Signature", "")
                return 200, await services.billing.handle_webhook(raw_body, signature), ()
            if operation == ("billing", "invoices"):
                return 200, await services.billing.invoices(ctx, self._page()), ()
            if operation == ("connectors", "list_connections"):
                return 200, await services.connectors.list_connections(ctx, self._page()), ()
            if operation == ("connectors", "authorize"):
                provider = path.split("/")[4]
                binding = self._cookies().get("connector_client")
                binding_value = binding.value if binding else secrets.token_urlsafe(32)
                url = await services.connectors.authorize(replace(ctx, client_binding=binding_value), provider)
                secure = "; Secure" if settings.public_origin.startswith("https://") else ""
                return 200, {"url": url}, (
                    f"connector_client={binding_value}; Path=/; Max-Age=600; HttpOnly; SameSite=Lax{secure}",)
            if operation == ("connectors", "callback"):
                binding = self._cookies().get("connector_client")
                connector_ctx = replace(ctx, client_binding=binding.value if binding else None)
                connector = await services.connectors.callback(connector_ctx, ConnectionCallback(
                    provider=self._string(body, "provider"), code=self._string(body, "code"),
                    state=self._string(body, "state")))
                return 201, connector, ("connector_client=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax",)
            if operation == ("connectors", "bind"):
                parts = path.split("/")
                return 200, await services.connectors.bind(ctx, EntityId(parts[4]), EntityId(parts[6]),
                    self._mutation(body)), ()
            if operation == ("connectors", "revoke"):
                await services.connectors.revoke(ctx, EntityId(path.rsplit("/", 1)[-1]),
                    self._mutation(body))
                return 204, None, ()
            if operation == ("tasks", "get"):
                return 200, await services.tasks.get(ctx, EntityId(path.rsplit("/", 1)[-1])), ()
            if operation == ("tasks", "approval"):
                return 200, await services.tasks.approval(ctx, EntityId(path.split("/")[4])), ()
            if operation == ("tasks", "approve"):
                indices = body.get("selected_indices")
                if (not isinstance(indices, list) or not indices or
                        any(type(index) is not int for index in indices)):
                    raise DomainError(ErrorCode.INVALID_INPUT, "Invalid selected_indices")
                note = body.get("additional_instructions", "")
                if not isinstance(note, str):
                    raise DomainError(ErrorCode.INVALID_INPUT, "Invalid additional_instructions")
                return 200, await services.tasks.approve(ctx, EntityId(path.split("/")[4]),
                    tuple(indices), note, self._mutation(body)), ()
            if operation == ("tasks", "active_for_project"):
                return 200, await services.tasks.active_for_project(
                    ctx, EntityId(path.split("/")[4])), ()
            if operation == ("tasks", "latest_for_project"):
                return 200, await services.tasks.latest_for_project(
                    ctx, EntityId(path.split("/")[4])), ()
            if operation == ("tasks", "cancel"):
                return 200, await application.generation.cancel(ctx,
                    EntityId(path.split("/")[4]), self._mutation(body)), ()
            if operation == ("generation", "models"):
                return 200, await services.generation.models(ctx), ()
            if operation == ("generation", "submit"):
                raw_attachments = body.get("attachment_ids", [])
                if not isinstance(raw_attachments, list) or any(not isinstance(item, str) for item in raw_attachments):
                    raise DomainError(ErrorCode.INVALID_INPUT, "Invalid attachment_ids")
                revision = body.get("expected_head_revision")
                if type(revision) is not int or revision < 0:
                    raise DomainError(ErrorCode.INVALID_INPUT, "Invalid expected_head_revision")
                confirmed_build_intent = body.get("confirmed_build_intent", False)
                if type(confirmed_build_intent) is not bool:
                    raise DomainError(ErrorCode.INVALID_INPUT, "Invalid confirmed_build_intent")
                command = GenerateApplication(project_id=EntityId(path.split("/")[4]),
                    prompt=self._string(body, "prompt"),
                    attachment_ids=tuple(EntityId(item) for item in raw_attachments),
                    model=self._string(body, "model"), agent_mode=self._string(body, "agent_mode"),
                    expected_head_revision=revision, mutation=self._mutation(body),
                    confirmed_build_intent=confirmed_build_intent)
                return 202, await application.generation.submit(ctx, command), ()
            if operation == ("generation", "edit"):
                raw_edits = body.get("edits")
                if not isinstance(raw_edits, list) or any(
                    not isinstance(item, dict) or not isinstance(item.get("path"), str) or
                    (item.get("content") is not None and not isinstance(item.get("content"), str))
                    for item in raw_edits):
                    raise DomainError(ErrorCode.INVALID_INPUT, "Invalid edits")
                command = EditVersion(project_id=EntityId(path.split("/")[4]),
                    base_version_id=EntityId(self._string(body, "base_version_id")),
                    edits=tuple(FileEdit(path=item["path"], content=item.get("content"))
                                for item in raw_edits),
                    summary=self._string(body, "summary"), mutation=self._mutation(body))
                return 202, await application.generation.edit(ctx, command), ()
            if operation == ("catalog", "search"):
                query = parse_qs(urlsplit(self.path).query)
                kind = query.get("kind", [None])[0]
                saved_only = query.get("saved_only", ["false"])[0]
                if saved_only not in {"true", "false"}:
                    raise DomainError(ErrorCode.INVALID_INPUT, "Invalid saved_only value")
                return 200, await services.catalog.search(ctx, CatalogQuery(
                    page=self._page(), kind=ContentKind(kind) if kind else None,
                    category=query.get("category", [None])[0],
                    author_id=EntityId(query["author_id"][0]) if "author_id" in query else None,
                    saved_only=saved_only == "true",
                    sort=query.get("sort", ["latest"])[0])), ()
            if operation == ("catalog", "categories"):
                query = parse_qs(urlsplit(self.path).query)
                return 200, await services.catalog.categories(ctx,
                    ContentKind(query.get("kind", ["public_project"])[0])), ()
            if operation == ("catalog", "detail"):
                return 200, await services.catalog.detail(ctx,
                    EntityId(path.rsplit("/", 1)[-1])), ()
            if operation == ("catalog", "save"):
                await services.catalog.save(ctx, EntityId(path.split("/")[4]),
                    self._boolean(body, "saved"), self._mutation(body))
                return 204, None, ()
            if operation == ("catalog", "record_view"):
                await services.catalog.record_view(ctx, EntityId(path.split("/")[4]),
                    self._mutation(body))
                return 204, None, ()
            if operation == ("catalog", "publish_entry"):
                if ctx.actor_id is None:
                    raise DomainError(ErrorCode.UNAUTHENTICATED, "Sign in required")
                mutation = self._mutation(body)
                project_id = EntityId(self._string(body, "project_id"))
                entry_id = EntityId(hashlib.sha256(
                    f"catalog:entry:{ctx.actor_id}:{project_id}:{mutation.idempotency_key}".encode()).hexdigest())
                entry = CatalogEntry(id=entry_id,
                    kind=ContentKind(self._string(body, "kind")),
                    project_id=project_id,
                    version_id=EntityId(self._string(body, "version_id")),
                    author_id=ctx.actor_id, title=self._string(body, "title"),
                    category=self._string(body, "category"),
                    description=self._string(body, "description"),
                    views=0, saves=0, revision=0)
                return 201, await services.catalog.publish_entry(ctx, entry, mutation), ()
            if operation == ("lifecycle", "clone"):
                source_entry = body.get("source_entry_id")
                if source_entry is not None and not isinstance(source_entry, str):
                    raise DomainError(ErrorCode.INVALID_INPUT, "Invalid source_entry_id")
                return 201, await application.lifecycle.clone(ctx, CloneProject(
                    source_version_id=EntityId(self._string(body, "source_version_id")),
                    target_name=self._string(body, "target_name"),
                    source_entry_id=EntityId(source_entry) if source_entry else None,
                    mutation=self._mutation(body))), ()
            if operation == ("lifecycle", "restore"):
                revision = body.get("expected_head_revision")
                if type(revision) is not int or revision < 0:
                    raise DomainError(ErrorCode.INVALID_INPUT, "Invalid head revision")
                return 200, await application.lifecycle.restore(ctx, PromoteVersion(
                    project_id=EntityId(path.split("/")[4]),
                    version_id=EntityId(self._string(body, "version_id")),
                    expected_head_revision=revision, task_id=None,
                    mutation=self._mutation(body))), ()
            if operation == ("preview", "open_catalog"):
                return 200, await application.preview.open_catalog(
                    ctx, EntityId(path.split("/")[4])), ()
            if operation == ("preview", "open"):
                return 201, await application.preview.open(ctx,
                    EntityId(path.split("/")[4]),
                    EntityId(self._string(body, "version_id"))), ()
            if operation == ("preview", "revoke_ticket"):
                await services.preview.revoke_ticket(ctx,
                    EntityId(path.rsplit("/", 1)[-1]), self._mutation(body))
                return 204, None, ()
            if operation == ("preview", "share"):
                return 201, await application.preview.share(ctx,
                    EntityId(path.split("/")[4]),
                    EntityId(self._string(body, "version_id")), self._mutation(body)), ()
            if operation == ("preview", "revoke_share"):
                await services.preview.revoke_share(ctx,
                    EntityId(path.rsplit("/", 1)[-1]), self._mutation(body))
                return 204, None, ()
            if operation == ("preview", "resolve_share"):
                return 200, await services.preview.resolve_share(
                    path.rsplit("/", 1)[-1]), ()
            if operation == ("app_resources", "resources"):
                query = parse_qs(urlsplit(self.path).query)
                environment = Environment(query.get("environment", ["development"])[0])
                return 200, await services.app_resources.resources(ctx,
                    EntityId(path.split("/")[4]), environment), ()
            if operation == ("app_resources", "configure"):
                resource = AppResource(id=EntityId(self._string(body, "id")),
                    project_id=EntityId(path.split("/")[4]),
                    environment=Environment(self._string(body, "environment")),
                    kind=ResourceKind(self._string(body, "kind")),
                    name=self._string(body, "name"),
                    status=self._string(body, "status"), secret_ref=None,
                    revision=body.get("revision", 0))
                return 200, await services.app_resources.configure(ctx, resource,
                    self._mutation(body)), ()
            if operation == ("app_resources", "set_secret"):
                return 200, await services.app_resources.set_secret(ctx,
                    EntityId(path.split("/")[4]), self._string(body, "secret"),
                    self._mutation(body)), ()
            if operation == ("app_resources", "list_app_users"):
                return 200, await services.app_resources.list_app_users(ctx,
                    EntityId(path.split("/")[4]), self._page()), ()
            if operation == ("publication", "prepare"):
                command = PublicationConfig(project_id=EntityId(path.split("/")[4]),
                    version_id=EntityId(self._string(body, "version_id")),
                    environment=Environment(self._string(body, "environment")),
                    domain=body.get("domain"), sync_db=self._boolean(body, "sync_db"),
                    migrate_data=self._boolean(body, "migrate_data"),
                    mutation=self._mutation(body))
                return 200, await application.publication.prepare(ctx, command), ()
            if operation == ("publication", "get"):
                return 200, await services.publication.get(ctx,
                    EntityId(path.split("/")[4])), ()
            if operation == ("publication", "request_manual"):
                return 200, await services.publication.request_manual(ctx,
                    EntityId(path.split("/")[4]), self._mutation(body)), ()
            if operation == ("publication", "verify_manual_result"):
                if ctx.actor_id is None:
                    raise DomainError(ErrorCode.UNAUTHENTICATED, "Sign in required")
                evidence = body.get("evidence")
                if not isinstance(evidence, dict):
                    raise DomainError(ErrorCode.INVALID_INPUT, "Evidence reference required")
                command = ManualDeploymentResult(publication_id=EntityId(path.split("/")[4]),
                    public_url=self._string(body, "public_url"),
                    evidence=BlobRef(**evidence), operator_id=ctx.actor_id,
                    mutation=self._mutation(body))
                return 200, await services.publication.verify_manual_result(ctx, command), ()
            if operation == ("publication", "change_state"):
                return 200, await services.publication.change_state(ctx,
                    EntityId(path.split("/")[4]),
                    PublicationState(self._string(body, "target")), self._mutation(body)), ()
            if operation == ("dashboard", "project"):
                return 200, await application.dashboard.project(ctx,
                    EntityId(path.split("/")[4])), ()
            raise FeatureNotImplemented(f"{route.owner}.{route.operation} is not implemented")

        def do_OPTIONS(self) -> None:
            request_id = uuid4().hex
            origin = self.headers.get("Origin")
            if origin != settings.public_origin:
                self._error(403, ErrorCode.FORBIDDEN, "Origin is not allowed", request_id)
                return
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,PATCH,DELETE,OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type,X-CSRF-Token,Idempotency-Key")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _handle(self) -> None:
            request_id = uuid4().hex
            path = urlsplit(self.path).path
            if path == "/healthz" and self.command == "GET":
                self._write(200, b'{"status":"ready"}', request_id)
                return
            if path == "/api/v1/identity/auth-config" and self.command == "GET":
                origin = self.headers.get("Origin")
                if origin and origin != settings.public_origin:
                    self._error(403, ErrorCode.FORBIDDEN, "Origin is not allowed", request_id)
                    return
                auth_config = {
                    "captcha_mode": "turnstile" if settings.captcha_provider == "turnstile" else
                        "private_test" if settings.private_test_mode else "unavailable",
                    "mail_mode": "local_capture" if settings.private_test_mode else "unavailable",
                }
                if settings.captcha_provider == "turnstile":
                    auth_config["captcha_site_key"] = settings.turnstile_site_key
                self._write(200, encode_response(auth_config), request_id)
                return
            route = next((item for item, pattern in _ROUTES
                          if item.method == self.command and pattern.fullmatch(path)), None)
            if route is None:
                self._error(404, ErrorCode.NOT_FOUND, "Route not found", request_id)
                return
            origin = self.headers.get("Origin")
            if origin and origin != settings.public_origin:
                self._error(403, ErrorCode.FORBIDDEN, "Origin is not allowed", request_id)
                return
            body = {}
            raw_body = b""
            if self.command != "GET":
                length = self.headers.get("Content-Length", "0")
                if not length.isdigit() or int(length) > settings.max_request_bytes:
                    self._error(422, ErrorCode.INVALID_INPUT, "Request body exceeds limit", request_id)
                    return
                raw_body = self.rfile.read(int(length))
                body = raw_body
                if body and self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    self._error(422, ErrorCode.INVALID_INPUT, "Expected application/json", request_id)
                    return
                if body:
                    try:
                        body = json.loads(body)
                        if not isinstance(body, dict):
                            raise json.JSONDecodeError("Object required", "", 0)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        self._error(422, ErrorCode.INVALID_INPUT, "Invalid JSON body", request_id)
                        return
                cookies = self._cookies()
                if cookies.get("session") is not None:
                    csrf_cookie = cookies.get("csrf_token")
                    csrf_header = self.headers.get("X-CSRF-Token")
                    if csrf_cookie is None or not csrf_header or not secrets.compare_digest(csrf_cookie.value, csrf_header):
                        self._error(403, ErrorCode.FORBIDDEN, "CSRF token invalid", request_id)
                        return
            if application is None:
                self._error(501, ErrorCode.NOT_IMPLEMENTED,
                            f"{route.owner}.{route.operation} is not implemented", request_id)
                return
            try:
                ctx = asyncio.run(self._context(request_id))
                if (route.owner, route.operation) == ("tasks", "events"):
                    self._stream_task_events(ctx, path, request_id)
                    return
                status, result, response_cookies = asyncio.run(self._dispatch(route, path, body, ctx, raw_body))
                if isinstance(result, RawResponse):
                    self._write(status, result.body, request_id, response_cookies,
                                result.content_type, result.attachment, result.filename)
                else:
                    response = b"" if status == 204 else encode_response(result)
                    self._write(status, response, request_id, response_cookies)
            except DomainError as exc:
                status = {ErrorCode.INVALID_INPUT: 422, ErrorCode.UNAUTHENTICATED: 401,
                          ErrorCode.FORBIDDEN: 403, ErrorCode.NOT_FOUND: 404,
                          ErrorCode.CONFLICT: 409, ErrorCode.QUOTA_EXCEEDED: 429,
                          ErrorCode.PROVIDER_UNAVAILABLE: 503}.get(exc.code, 500)
                self._error(status, exc.code, exc.message, request_id)
            except (KeyError, TypeError, ValueError, AttributeError):
                self._error(422, ErrorCode.INVALID_INPUT, "Invalid request payload", request_id)
            except FeatureNotImplemented:
                self._error(501, ErrorCode.NOT_IMPLEMENTED,
                            f"{route.owner}.{route.operation} is not implemented", request_id)
            except RuntimeError:
                self._error(503, ErrorCode.PROVIDER_UNAVAILABLE, "Required provider is unavailable", request_id)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # A browser reload may close an SSE or JSON response mid-write.
                # Headers were already sent, so no second error response is possible.
                return
            except Exception:
                self._error(500, ErrorCode.INTERNAL, "Internal server error", request_id)

        do_GET = _handle
        do_POST = _handle
        do_PUT = _handle
        do_PATCH = _handle
        do_DELETE = _handle

    return Handler


def serve(application: Application, settings: Settings) -> None:
    from atoms_demo.api.preview_runtime import create_server
    from atoms_demo.api.task_worker import run_forever
    stop = threading.Event()
    worker = threading.Thread(target=lambda: asyncio.run(run_forever(
        application, settings.queue_lease_seconds, stop)), name="atoms-task-worker", daemon=True)
    preview_server = create_server(application, settings)
    preview_thread = threading.Thread(target=preview_server.serve_forever,
        name="atoms-preview-server", daemon=True)
    worker.start()
    preview_thread.start()
    try:
        with ThreadingHTTPServer((settings.host, settings.port), handler_type(settings, application)) as server:
            server.serve_forever()
    finally:
        stop.set()
        preview_server.shutdown()
        preview_server.server_close()
        preview_thread.join(timeout=2)
        worker.join(timeout=2)
