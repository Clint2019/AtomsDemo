"""Immutable source snapshots, build metadata and the single successful head."""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from atoms_demo.contracts.versions import (FileEntry, Version, ProjectHead, CreateVersion,
                                           PromoteVersion, FileEdit, EditVersion, VersionAPI)
from atoms_demo.core.errors import DomainError, ErrorCode
from atoms_demo.core.types import BlobRef, EntityId, Page, PageQuery, RequestContext, Scope
from atoms_demo.ports.dependencies import VersionDependencies

_GLOBAL = Scope(workspace_id=None)
_ALLOWED_DEPENDENCIES = {"react", "react-dom", "lucide-react", "vite", "typescript"}
_EXPORT_RUNTIME = Path(__file__).resolve().parents[2] / "build-runtime"


@dataclass(frozen=True)
class VersionLocator:
    id: EntityId
    target_workspace_id: EntityId
    target_project_id: EntityId
    revision: int = 0


@dataclass(frozen=True)
class VersionBuildRecord:
    id: EntityId
    artifact: BlobRef | None
    build_status: str
    revision: int = 0


@dataclass(frozen=True)
class HeadRecord:
    id: EntityId
    project_id: EntityId
    version_id: EntityId | None
    revision: int = 0


@dataclass(frozen=True)
class VersionMutation:
    id: EntityId
    version_id: EntityId
    input_digest: EntityId
    revision: int = 0


@dataclass(frozen=True)
class PromotionMutation:
    id: EntityId
    project_id: EntityId
    input_digest: EntityId
    version_id: EntityId
    result_revision: int
    revision: int = 0


def _blob(data: dict | None) -> BlobRef | None:
    return BlobRef(**data) if data else None


def decode_version(data: dict) -> Version:
    return Version(**{**data, "files": tuple(FileEntry(path=item["path"], blob=_blob(item["blob"]))
                                                for item in data["files"]),
                      "source_archive": _blob(data["source_archive"]),
                      "artifact": _blob(data["artifact"]),
                      "created_at": datetime.fromisoformat(data["created_at"])})


def decode_build(data: dict) -> VersionBuildRecord:
    return VersionBuildRecord(**{**data, "artifact": _blob(data["artifact"])})


def _hash(value: str) -> EntityId:
    return EntityId(hashlib.sha256(value.encode("utf-8")).hexdigest())


def _path(raw: str) -> str:
    if (not isinstance(raw, str) or not raw or len(raw) > 240 or "\\" in raw or
            raw.startswith("/") or "//" in raw or ":" in raw or
            any(ord(char) < 32 or ord(char) == 127 for char in raw)):
        raise DomainError(ErrorCode.INVALID_INPUT, "Invalid source path")
    parts = raw.split("/")
    reserved = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                *(f"lpt{i}" for i in range(1, 10))}
    if (any(part in {"", ".", ".."} or part.endswith((" ", ".")) or
            part.split(".", 1)[0].lower() in reserved for part in parts) or
            parts[0] in {"node_modules", ".git", "dist"}):
        raise DomainError(ErrorCode.INVALID_INPUT, "Invalid source path")
    return raw


def package_source_export(version_id: EntityId, paths: tuple[str, ...], source: bytes) -> bytes:
    """Wrap an immutable source archive with a reproducible local Vite runner."""
    package = json.loads((_EXPORT_RUNTIME / "package.json").read_text(encoding="utf-8"))
    lock_bytes = (_EXPORT_RUNTIME / "package-lock.json").read_bytes()
    lock = json.loads(lock_bytes)
    if package.get("dependencies") != lock.get("packages", {}).get("", {}).get("dependencies"):
        raise RuntimeError("Export package and lock dependencies differ")
    package["scripts"] = {
        "dev": "vite --config vite.config.mjs",
        "build": "vite build --config vite.config.mjs",
        "preview": "vite preview --config vite.config.mjs",
    }
    readme = (
        f"# Atoms Demo 源码导出\n\n版本：`{version_id}`。原始版本文件位于 `source/`，"
        "内容与平台版本快照一致。\n\n"
        "## 本地运行\n\n需要 Node.js `^20.19.0 || >=22.12.0`（以及 npm）。在本目录执行：\n\n"
        "```sh\nnpm ci\nnpm run dev\n```\n\n"
        "打开终端显示的本机地址。生产构建与本地预览：\n\n"
        "```sh\nnpm run build\nnpm run preview\n```\n\n"
        "构建结果位于 `dist/`。开发与预览命令只监听本机回环地址。"
        "随包的本地应用数据适配器支持 `/app/collections/{collection}/records` "
        "和 `/app/records/{id}`，单用户记录保存在 `.atoms-local-data.json`；"
        "本地数据不与 Atoms Demo 平台同步。平台身份、权限、外部资源及其他专用 API "
        "仍须在 Atoms Demo 预览环境运行。此本地适配器仅用于导出 Demo 的开发和验证。\n"
    )
    result = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(source)) as original, zipfile.ZipFile(
            result, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        names = original.namelist()
        if len(names) != len(set(names)) or set(names) != set(paths):
            raise RuntimeError("Stored source archive differs from version files")
        archive.writestr("README.md", readme.encode("utf-8"))
        archive.writestr("package.json", (json.dumps(package, ensure_ascii=False,
                                                      indent=2) + "\n").encode("utf-8"))
        archive.writestr("package-lock.json", lock_bytes)
        archive.writestr("vite.config.mjs",
                         (_EXPORT_RUNTIME / "export-vite.config.mjs").read_bytes())
        archive.writestr("local-app-data.mjs",
                         (_EXPORT_RUNTIME / "local-app-data.mjs").read_bytes())
        archive.writestr(".gitignore", "node_modules/\ndist/\n.atoms-local-data.json\n")
        for name in sorted(names):
            if _path(name) != name:
                raise RuntimeError("Stored source path is invalid")
            archive.writestr("source/" + name, original.read(name))
    return result.getvalue()


class VersionService:
    def __init__(self, dependencies: VersionDependencies) -> None:
        self.dependencies = dependencies

    @staticmethod
    def _scope(ctx: RequestContext, project_id: EntityId) -> Scope:
        if ctx.workspace_id is None:
            raise DomainError(ErrorCode.FORBIDDEN, "Workspace selection required")
        return Scope(workspace_id=ctx.workspace_id, project_id=project_id)

    async def _version(self, uow, version_id: EntityId) -> tuple[Version, Scope] | None:
        locator = await uow.repository(VersionLocator).get(_GLOBAL, version_id)
        if locator is None:
            return None
        scope = Scope(workspace_id=locator.target_workspace_id, project_id=locator.target_project_id)
        version = await uow.repository(Version).get(scope, version_id)
        if version is None:
            return None
        build = await uow.repository(VersionBuildRecord).get(_GLOBAL, version_id)
        if build:
            version = replace(version, artifact=build.artifact, build_status=build.build_status)
        return version, scope

    async def head(self, ctx: RequestContext, project_id: EntityId) -> ProjectHead:
        await self.dependencies.authorization.require(ctx, "project:history", project_id)
        scope = self._scope(ctx, project_id)
        async with self.dependencies.uow() as uow:
            record = await uow.repository(HeadRecord).get(scope, project_id)
        return ProjectHead(project_id=project_id, version_id=record.version_id if record else None,
                           revision=record.revision if record else 0)

    async def get(self, ctx: RequestContext, version_id: EntityId) -> Version:
        async with self.dependencies.uow() as uow:
            located = await self._version(uow, version_id)
        if located is None:
            raise DomainError(ErrorCode.NOT_FOUND, "Version not found")
        version, _ = located
        await self.dependencies.authorization.require(ctx, "project:history", version.project_id)
        return version

    async def clone_source(self, ctx: RequestContext, version_id: EntityId) -> Version:
        async with self.dependencies.uow() as uow:
            located = await self._version(uow, version_id)
        if located is None:
            raise DomainError(ErrorCode.NOT_FOUND, "Version not found")
        version, _ = located
        await self.dependencies.authorization.require(ctx, "project:read", version.project_id)
        if version.build_status != "success" or version.artifact is None:
            raise DomainError(ErrorCode.INVALID_INPUT, "Only a successful version can be cloned")
        return version

    async def read_file(self, ctx: RequestContext, version_id: EntityId, path: str) -> str:
        version = await self.get(ctx, version_id)
        clean = _path(path)
        entry = next((item for item in version.files if item.path == clean), None)
        if entry is None:
            raise DomainError(ErrorCode.NOT_FOUND, "File not found")
        try:
            return (await self.dependencies.blobs.read(entry.blob)).decode("utf-8")
        except UnicodeDecodeError:
            raise DomainError(ErrorCode.INVALID_INPUT, "File is not UTF-8 text") from None

    async def list_versions(self, ctx: RequestContext, project_id: EntityId,
                            page: PageQuery) -> Page[Version]:
        await self.dependencies.authorization.require(ctx, "project:history", project_id)
        scope = self._scope(ctx, project_id)
        async with self.dependencies.uow() as uow:
            listing = await uow.repository(Version).list(scope, page)
            items = []
            for version in listing.items:
                build = await uow.repository(VersionBuildRecord).get(_GLOBAL, version.id)
                items.append(replace(version, artifact=build.artifact, build_status=build.build_status)
                             if build else version)
        return Page(items=tuple(items), next_cursor=listing.next_cursor)

    async def _validate_files(self, files: tuple[FileEntry, ...]) -> tuple[dict[str, bytes], str]:
        if not files or len(files) > 200:
            raise DomainError(ErrorCode.INVALID_INPUT, "Source file count is invalid")
        contents: dict[str, bytes] = {}
        total = 0
        for entry in files:
            path = _path(entry.path)
            if path in contents:
                raise DomainError(ErrorCode.INVALID_INPUT, "Duplicate source path")
            content = await self.dependencies.blobs.read(entry.blob)
            total += len(content)
            if total > 20_971_520:
                raise DomainError(ErrorCode.QUOTA_EXCEEDED, "Source snapshot exceeds limit")
            contents[path] = content
        if "package.json" in contents:
            try:
                package = json.loads(contents["package.json"].decode("utf-8"))
                if not isinstance(package, dict):
                    raise ValueError()
                if set(package) - {"name", "version", "private", "type", "dependencies", "devDependencies"}:
                    raise ValueError()
                if not isinstance(package.get("dependencies", {}), dict) or not isinstance(package.get("devDependencies", {}), dict):
                    raise ValueError()
                dependencies = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
                if not isinstance(dependencies, dict):
                    raise ValueError()
                for name, version in dependencies.items():
                    if name not in _ALLOWED_DEPENDENCIES or not isinstance(version, str) or not re.fullmatch(r"[~^]?\d+(?:\.\d+){0,2}", version):
                        raise ValueError()
            except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
                raise DomainError(ErrorCode.INVALID_INPUT, "Unsupported package dependencies") from None
        fingerprint = hashlib.sha256()
        for path in sorted(contents):
            fingerprint.update(path.encode("utf-8") + b"\0" + hashlib.sha256(contents[path]).digest())
        return contents, fingerprint.hexdigest()

    async def create_candidate(self, ctx: RequestContext, command: CreateVersion) -> Version:
        await self.dependencies.authorization.require(ctx, "project:write", command.project_id)
        scope = self._scope(ctx, command.project_id)
        contents, digest = await self._validate_files(command.files)
        if not command.mutation.idempotency_key:
            raise DomainError(ErrorCode.INVALID_INPUT, "Idempotency key required")
        mutation_id = _hash(f"version:create:{ctx.actor_id}:{command.project_id}:{command.mutation.idempotency_key}")
        input_digest = _hash(f"{command.base_version_id}:{digest}:{command.summary}")
        async with self.dependencies.uow() as uow:
            prior = await uow.repository(VersionMutation).get(scope, mutation_id)
            if prior:
                if prior.input_digest != input_digest:
                    raise DomainError(ErrorCode.CONFLICT, "Idempotency key was used with different source")
                located = await self._version(uow, prior.version_id)
                if located is None:
                    raise DomainError(ErrorCode.CONFLICT, "Version record is inconsistent")
                return located[0]
        if command.base_version_id:
            base = await self.get(ctx, command.base_version_id)
            if base.project_id != command.project_id:
                raise DomainError(ErrorCode.FORBIDDEN, "Base version belongs to another project")
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path, content in sorted(contents.items()):
                archive.writestr(path, content)
        source_archive = await self.dependencies.blobs.put(stream.getvalue(), "application/zip")
        version = Version(id=self.dependencies.ids.new(), project_id=command.project_id,
                          parent_id=command.base_version_id, files=command.files,
                          source_archive=source_archive, artifact=None, build_status="candidate",
                          summary=command.summary[:1000], created_at=self.dependencies.clock.now())
        async with self.dependencies.uow() as uow:
            prior = await uow.repository(VersionMutation).get(scope, mutation_id)
            if prior:
                if prior.input_digest != input_digest:
                    raise DomainError(ErrorCode.CONFLICT, "Idempotency key was used with different source")
                located = await self._version(uow, prior.version_id)
                if located is None:
                    raise DomainError(ErrorCode.CONFLICT, "Version record is inconsistent")
                return located[0]
            await uow.repository(Version).save(scope, version, None)
            await uow.repository(VersionLocator).save(_GLOBAL, VersionLocator(
                id=version.id, target_workspace_id=ctx.workspace_id, target_project_id=command.project_id), None)
            await uow.repository(VersionMutation).save(scope, VersionMutation(
                id=mutation_id, version_id=version.id, input_digest=input_digest), None)
            await uow.commit()
        return version

    async def edit_candidate(self, ctx: RequestContext, command: EditVersion) -> Version:
        base = await self.get(ctx, command.base_version_id)
        if base.project_id != command.project_id:
            raise DomainError(ErrorCode.FORBIDDEN, "Base version belongs to another project")
        files = {entry.path: entry for entry in base.files}
        for edit in command.edits:
            path = _path(edit.path)
            if edit.content is None:
                files.pop(path, None)
            else:
                blob = await self.dependencies.blobs.put(edit.content.encode("utf-8"), "text/plain")
                files[path] = FileEntry(path=path, blob=blob)
        return await self.create_candidate(ctx, CreateVersion(
            project_id=command.project_id, base_version_id=command.base_version_id,
            files=tuple(files.values()), summary=command.summary, mutation=command.mutation))

    async def record_build(self, ctx: RequestContext, version_id: EntityId,
                           artifact: BlobRef | None, success: bool) -> Version:
        version = await self.get(ctx, version_id)
        await self.dependencies.authorization.require(ctx, "project:write", version.project_id)
        if success and artifact is None:
            raise DomainError(ErrorCode.INVALID_INPUT, "Successful build requires an artifact")
        if artifact:
            await self.dependencies.blobs.read(artifact)
        async with self.dependencies.uow() as uow:
            existing = await uow.repository(VersionBuildRecord).get(_GLOBAL, version_id)
            if existing:
                if existing.artifact == artifact and existing.build_status == ("success" if success else "failed"):
                    return replace(version, artifact=artifact, build_status=existing.build_status)
                raise DomainError(ErrorCode.CONFLICT, "Build result already recorded")
            await uow.repository(VersionBuildRecord).save(_GLOBAL, VersionBuildRecord(
                id=version_id, artifact=artifact, build_status="success" if success else "failed"), None)
            await uow.commit()
        return replace(version, artifact=artifact, build_status="success" if success else "failed")

    async def promote(self, ctx: RequestContext, command: PromoteVersion) -> ProjectHead:
        await self.dependencies.authorization.require(ctx, "project:write", command.project_id)
        scope = self._scope(ctx, command.project_id)
        if not command.mutation.idempotency_key:
            raise DomainError(ErrorCode.INVALID_INPUT, "Idempotency key required")
        mutation_id = _hash(f"version:promote:{ctx.actor_id}:{command.project_id}:{command.mutation.idempotency_key}")
        input_digest = _hash(f"{command.version_id}:{command.expected_head_revision}:{command.task_id}")
        async with self.dependencies.uow() as uow:
            prior = await uow.repository(PromotionMutation).get(scope, mutation_id)
            if prior:
                if prior.input_digest != input_digest:
                    raise DomainError(ErrorCode.CONFLICT, "Idempotency key was used with different promotion")
                return ProjectHead(project_id=command.project_id, version_id=prior.version_id,
                                   revision=prior.result_revision)
            located = await self._version(uow, command.version_id)
            if located is None or located[0].project_id != command.project_id or located[0].build_status != "success":
                raise DomainError(ErrorCode.INVALID_INPUT, "Only a successful project version can be promoted")
            if command.task_id is not None:
                await self.dependencies.commit_fence.assert_valid(uow, ctx.workspace_id,
                    command.project_id, command.task_id, command.version_id,
                    command.lease_owner or "")
            repo = uow.repository(HeadRecord)
            current = await repo.get(scope, command.project_id)
            revision = current.revision if current else 0
            if revision != command.expected_head_revision:
                raise DomainError(ErrorCode.CONFLICT, "Project head revision changed")
            saved = await repo.save(scope, HeadRecord(id=command.project_id,
                                                       project_id=command.project_id,
                                                       version_id=command.version_id),
                                    revision if current else None)
            await uow.repository(PromotionMutation).save(scope, PromotionMutation(
                id=mutation_id, project_id=command.project_id, input_digest=input_digest,
                version_id=command.version_id, result_revision=saved.revision), None)
            await uow.commit()
        return ProjectHead(project_id=command.project_id, version_id=saved.version_id, revision=saved.revision)

    async def export(self, ctx: RequestContext, version_id: EntityId) -> BlobRef:
        return (await self.get(ctx, version_id)).source_archive

    async def download_export(self, ctx: RequestContext, version_id: EntityId) -> bytes:
        version = await self.get(ctx, version_id)
        source = await self.dependencies.blobs.read(version.source_archive)
        return package_source_export(version.id, tuple(entry.path for entry in version.files), source)
