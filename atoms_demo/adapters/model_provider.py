"""A replaceable OpenAI-compatible adapter for Token Plan access."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from atoms_demo.config import Settings
from atoms_demo.contracts.generation import FileChangeSet, GenerationInput, RepairInput, SourceFile
from atoms_demo.core.errors import DomainError, ErrorCode, MeteredProviderError, UnsentModelError
from atoms_demo.ports.providers import ModelProvider
from atoms_demo.ports.runtime import BlobStore


TOKEN_PLAN_BASE_URL = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
_LOG = logging.getLogger(__name__)
_MAX_REQUEST_BYTES = 1_000_000
_MAX_RESPONSE_BYTES = 2_000_000
_FILE_CHANGES_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "atoms_file_changes",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "upserts": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"], "additionalProperties": False,
                }},
                "deleted_paths": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["summary", "upserts", "deleted_paths"],
            "additionalProperties": False,
        },
    },
}


class UnconfiguredModelProvider:
    async def generate(self, command: GenerationInput) -> FileChangeSet:
        raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Model provider is not configured")

    async def repair(self, command: RepairInput) -> FileChangeSet:
        raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Model provider is not configured")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class BailianTokenPlanProvider:
    def __init__(self, *, key: str, model: str, timeout_seconds: int,
                 max_output_tokens: int, blobs: BlobStore, base_url: str = TOKEN_PLAN_BASE_URL):
        if base_url != TOKEN_PLAN_BASE_URL:
            raise ValueError("Token Plan provider requires its official Beijing endpoint")
        if not key.startswith("sk-sp-") or not model:
            raise ValueError("Token Plan key (sk-sp-) and model are required")
        self._key = key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens
        self.blobs = blobs
        self._opener = build_opener(_NoRedirect)

    async def _content(self, command: GenerationInput, diagnostics: tuple[str, ...] = ()) -> str:
        if command.model != self.model:
            raise UnsentModelError(ErrorCode.INVALID_INPUT, "Model is not configured")
        attachments: list[dict[str, str]] = []
        for blob in command.attachments:
            if blob.media_type not in {"text/plain", "text/markdown", "application/json"}:
                raise UnsentModelError(ErrorCode.INVALID_INPUT, "This model accepts text attachments only")
            if blob.size_bytes > 100_000:
                raise UnsentModelError(ErrorCode.INVALID_INPUT, "Attachment is too large for model context")
            try:
                contents = (await self.blobs.read(blob)).decode("utf-8")
            except UnicodeDecodeError:
                raise UnsentModelError(ErrorCode.INVALID_INPUT, "Attachment must be UTF-8 text") from None
            except DomainError as exc:
                raise UnsentModelError(exc.code, exc.message) from None
            attachments.append({"media_type": blob.media_type, "content": contents})
        context = {
            "task_id": str(command.task_id), "request": command.prompt,
            "context_summary": command.context_summary, "agent_mode": command.agent_mode,
            "current_files": [{"path": file.path, "content": file.content} for file in command.files],
            "attachments": attachments, "build_diagnostics": list(diagnostics),
        }
        content = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        if len(content.encode("utf-8")) > _MAX_REQUEST_BYTES:
            raise UnsentModelError(ErrorCode.INVALID_INPUT, "Model context is too large")
        return content

    def _post(self, payload: bytes) -> dict:
        request = Request(TOKEN_PLAN_BASE_URL + "/chat/completions", data=payload,
                          method="POST", headers={"Authorization": f"Bearer {self._key}",
                                                  "Content-Type": "application/json",
                                                  "Accept": "application/json"})
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                data = response.read(_MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE,
                              f"Model provider returned HTTP {exc.code}") from None
        except TimeoutError:
            raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Model provider request timed out") from None
        except URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE,
                                  "Model provider request timed out") from None
            raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Model provider connection failed") from None
        except OSError:
            raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Model provider connection failed") from None
        if len(data) > _MAX_RESPONSE_BYTES:
            raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Model response is too large")
        try:
            result = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Model returned invalid JSON") from None
        if not isinstance(result, dict):
            raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Model returned invalid response")
        return result

    @staticmethod
    def _changes(response: dict) -> FileChangeSet:
        usage = response.get("usage")
        if not isinstance(usage, dict):
            raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Model returned missing usage")
        input_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
        output_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
        if (type(input_tokens) is not int or type(output_tokens) is not int or
                input_tokens < 0 or output_tokens < 0):
            raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Model returned invalid usage")
        try:
            message = response["choices"][0]["message"]["content"]
            if not isinstance(message, str):
                raise ValueError
            source = message.strip()
            if source.startswith("```json") and source.endswith("```"):
                source = source[7:-3].strip()
            elif source.startswith("```") and source.endswith("```"):
                source = source[3:-3].strip()
            changes = json.loads(source)
            if not isinstance(changes, dict) or not isinstance(changes.get("summary"), str):
                raise ValueError
            upserts = changes["upserts"]
            deleted = changes["deleted_paths"]
            if not isinstance(upserts, list) or not isinstance(deleted, list) or len(upserts) > 100:
                raise ValueError
            files = []
            for item in upserts:
                if (not isinstance(item, dict) or not isinstance(item.get("path"), str) or
                        not isinstance(item.get("content"), str) or
                        len(item["content"].encode("utf-8")) > 200_000):
                    raise ValueError
                files.append(SourceFile(path=item["path"], content=item["content"]))
            if any(not isinstance(path, str) for path in deleted):
                raise ValueError
            return FileChangeSet(summary=changes["summary"], upserts=tuple(files),
                                 deleted_paths=tuple(deleted), input_tokens=input_tokens,
                                 output_tokens=output_tokens)
        except (AttributeError, KeyError, IndexError, TypeError, ValueError):
            raise MeteredProviderError("Model returned invalid file changes",
                                       input_tokens, output_tokens) from None

    async def _complete(self, command: GenerationInput,
                        diagnostics: tuple[str, ...] = ()) -> FileChangeSet:
        content = await self._content(command, diagnostics)
        instruction = (
            "You generate source file changes for a React/Vite web application. "
            "Return only one JSON object with keys summary (string), upserts (array of "
            "{path,content}), deleted_paths (array of paths). Include runnable source files "
            "for a new application. For a repair, use the provided build diagnostics and "
            "current files; return only changed files. Do not include shell commands or "
            "Markdown. Keep all paths relative and do not write secrets. "
            "The fixed build recipe uses Vite with React, react-dom, lucide-react and "
            "TypeScript already installed. Include index.html and a relative source entrypoint. "
            "Do not add package.json, lockfiles, Vite/PostCSS config, build scripts or other dependencies. "
            "When the app saves user data, use the preview's same-origin application data API "
            "as the authoritative store, not localStorage. GET /app/collections/{collection}/records?limit=100 "
            "returns {items,next_cursor}; each item has id, json_document (a JSON string), "
            "schema_version and revision. Follow next_cursor for further pages. "
            "Use a globally random record ID such as crypto.randomUUID(), never a fixed ID or array index. "
            "PUT /app/records/{id} with Content-Type: application/json and an Idempotency-Key "
            "unique to each user action; send {collection,json_document:JSON.stringify(object),"
            "schema_version:1,revision:0} for a new record and the returned revision when updating. "
            "PUT returns the saved record object directly, for example "
            "{id,collection,json_document,schema_version,revision}; it does not wrap the "
            "object in a record property. Read response.id and response.revision directly. "
            "DELETE /app/records/{id} also "
            "requires an Idempotency-Key and returns 204 with no JSON body. "
            "On a network retry of the same action, reuse its key and identical request body. "
            "Use same-origin fetch with credentials included, handle HTTP errors, and retain "
            "existing records across reloads and compatible source versions. "
            "Collection names must be lowercase letters, digits or underscore starting with a letter. "
            "Do not hardcode a project ID, preview host or token."
        )
        payload = json.dumps({"model": self.model, "max_tokens": self.max_output_tokens,
                              "response_format": _FILE_CHANGES_FORMAT,
                              "messages": [{"role": "system", "content": instruction},
                                           {"role": "user", "content": content}]},
                             ensure_ascii=False).encode("utf-8")
        if len(payload) > _MAX_REQUEST_BYTES:
            raise UnsentModelError(ErrorCode.INVALID_INPUT, "Model request is too large")
        started = time.monotonic()
        phase = "transport"
        try:
            response = await asyncio.to_thread(self._post, payload)
            phase = "response_validation"
            return self._changes(response)
        except DomainError as exc:
            # These adapter messages are fixed strings; never log provider bodies or prompt data.
            _LOG.warning("Model call failed task_id=%s phase=%s elapsed_ms=%d code=%s reason=%s",
                         command.task_id, phase, int((time.monotonic() - started) * 1000),
                         exc.code.value, exc.message)
            raise
        except Exception as exc:
            _LOG.warning("Model call failed task_id=%s phase=%s elapsed_ms=%d error_type=%s",
                         command.task_id, phase, int((time.monotonic() - started) * 1000),
                         type(exc).__name__)
            raise

    async def generate(self, command: GenerationInput) -> FileChangeSet:
        return await self._complete(command)

    async def repair(self, command: RepairInput) -> FileChangeSet:
        return await self._complete(command.generation, command.diagnostics)


def create_adapter(settings: Settings, blobs: BlobStore | None = None) -> ModelProvider:
    if settings.model_provider != "aliyun_token_plan":
        return UnconfiguredModelProvider()
    if not settings.model_secret_name or not settings.model_name or blobs is None:
        return UnconfiguredModelProvider()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", settings.model_secret_name):
        raise ValueError("ATOMS_MODEL_SECRET_NAME must name one environment variable")
    key = os.environ.get(settings.model_secret_name, "").strip()
    if not key:
        return UnconfiguredModelProvider()
    return BailianTokenPlanProvider(key=key, model=settings.model_name,
        timeout_seconds=settings.model_timeout_seconds,
        max_output_tokens=settings.model_max_output_tokens, blobs=blobs,
        base_url=settings.model_base_url)
