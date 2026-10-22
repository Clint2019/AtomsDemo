"""Fixed React/Vite recipe executed in an isolated local or SSH Docker container."""
from __future__ import annotations

import asyncio
import io
import json
import re
import shlex
import shutil
import stat
import subprocess
import tarfile
import threading
import time
import zipfile
from uuid import uuid4

from atoms_demo.ports.providers import BuildExecutor
from atoms_demo.ports.runtime import BlobStore
from atoms_demo.config import Settings
from atoms_demo.core.errors import DomainError, ErrorCode
from atoms_demo.contracts.builds import BuildInput, BuildResult, ScanResult
from atoms_demo.core.types import EntityId, BlobRef

IMAGE = "atoms-react-vite:v1"
_PATH = re.compile(r"[A-Za-z0-9_./@+~-]+")
_LIMIT = 20_971_520
_ALLOWED_PACKAGES = {"react", "react-dom", "lucide-react", "vite", "typescript"}
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
             *(f"lpt{i}" for i in range(1, 10))}


def _safe_name(name: str) -> bool:
    return (bool(_PATH.fullmatch(name)) and len(name) <= 240 and
            all(part not in {"", ".", ".."} and not part.endswith((" ", ".")) and
                part.split(".", 1)[0].lower() not in _RESERVED for part in name.split("/")) and
            name.split("/")[0] not in {"node_modules", ".git", "dist"})


def _entries(data: bytes, *, artifact: bool) -> list[tuple[str, bytes]]:
    if len(data) > _LIMIT:
        raise DomainError(ErrorCode.INVALID_INPUT, "Build archive exceeds limit")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > (1000 if artifact else 200):
                raise ValueError("Invalid file count")
            entries: list[tuple[str, bytes]] = []
            seen: set[str] = set()
            total = 0
            for info in infos:
                name = info.filename
                if (info.is_dir() or not _safe_name(name) or
                        stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK or
                        name.lower() in seen or info.file_size > _LIMIT):
                    raise ValueError("Unsafe archive entry")
                seen.add(name.lower())
                total += info.file_size
                if total > _LIMIT:
                    raise ValueError("Archive exceeds limit")
                content = archive.read(info)
                if len(content) != info.file_size:
                    raise ValueError("Invalid archive entry")
                entries.append((name, content))
            if any("/".join(name.split("/")[:index]).lower() in seen
                   for name, _ in entries for index in range(1, len(name.split("/")))):
                raise ValueError("File and directory collide")
            return entries
    except (zipfile.BadZipFile, RuntimeError, ValueError, OSError):
        raise DomainError(ErrorCode.INVALID_INPUT, "Invalid build archive") from None


def _validate_package(entries: list[tuple[str, bytes]]) -> None:
    package_bytes = dict(entries).get("package.json")
    if package_bytes is None:
        return
    try:
        package = json.loads(package_bytes)
        if not isinstance(package, dict) or set(package) - {
                "name", "version", "private", "type", "dependencies", "devDependencies"}:
            raise ValueError
        for key in ("dependencies", "devDependencies"):
            deps = package.get(key, {})
            if not isinstance(deps, dict) or any(name not in _ALLOWED_PACKAGES for name in deps):
                raise ValueError
    except (ValueError, UnicodeDecodeError, TypeError):
        raise DomainError(ErrorCode.INVALID_INPUT, "Unsupported package manifest") from None


def _source_tar(entries: list[tuple[str, bytes]]) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, content in entries:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o644
            info.uid = info.gid = 10001
            archive.addfile(info, io.BytesIO(content))
    return stream.getvalue()


def _pack_tar(data: bytes) -> bytes:
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as source:
            members = source.getmembers()
            if len(members) > 1100:
                raise ValueError("Too many build files")
            files = []
            total = 0
            for member in members:
                name = member.name.removeprefix("./")
                if member.isdir():
                    continue
                if name == ".atoms-result.json":
                    continue
                if not member.isfile() or not _safe_name(name):
                    raise ValueError("Unsafe build file")
                total += member.size
                if total > _LIMIT:
                    raise ValueError("Build output exceeds limit")
                file = source.extractfile(member)
                if file is None:
                    raise ValueError("Missing build file")
                files.append((name, file.read()))
            if "index.html" not in {name for name, _ in files}:
                raise ValueError("Build produced no preview site")
    except (tarfile.TarError, ValueError, OSError):
        raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Invalid build output") from None
    stream = io.BytesIO()
    total = 0
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files):
            total += len(content)
            archive.writestr(name, content)
    result = stream.getvalue()
    if len(result) > _LIMIT:
        raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Build artifact exceeds limit")
    return result


class UnconfiguredBuildExecutor:
    async def build(self, command: BuildInput) -> BuildResult:
        raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Isolated build executor is not configured")

    async def scan(self, version_id: EntityId, artifact: BlobRef) -> ScanResult:
        raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Isolated build executor is not configured")


class DockerBuildExecutor:
    def __init__(self, blobs: BlobStore, timeout_seconds: int, image: str = IMAGE,
                 docker_host: str = ""):
        self.blobs = blobs
        self.timeout_seconds = timeout_seconds
        self.image = image
        if docker_host and not docker_host.startswith("ssh://"):
            raise ValueError("Remote Docker requires SSH")
        self.docker_host = docker_host

    def _command(self, *args: str) -> list[str]:
        if not self.docker_host:
            return ["docker", *args]
        host = self.docker_host.removeprefix("ssh://")
        if not re.fullmatch(r"[A-Za-z0-9_.@-]+", host):
            raise ValueError("Invalid SSH Docker host")
        return ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", host,
                shlex.join(["docker", *args])]

    def _docker(self, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
        try:
            completed = subprocess.run(self._command(*args), capture_output=True,
                                       timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Docker engine is unavailable") from None
        if len(completed.stdout) > 65_536 or len(completed.stderr) > 65_536:
            raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Docker response exceeds limit")
        if completed.returncode:
            message = completed.stderr.decode("utf-8", "replace")[:1000]
            raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, f"Docker operation failed: {message}")
        return completed

    def _docker_tar_out(self, container: str) -> bytes:
        try:
            process = subprocess.Popen(self._command("exec", container, "tar", "-C", "/output",
                "-cf", "-", "."), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Docker export unavailable") from None
        chunks: list[bytes] = []
        total = 0
        overflow = False

        def drain() -> None:
            nonlocal total, overflow
            assert process.stdout is not None
            while chunk := process.stdout.read(65_536):
                total += len(chunk)
                if total > 26_214_400:
                    overflow = True
                    process.kill()
                    break
                chunks.append(chunk)

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        try:
            process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Docker export timed out") from None
        finally:
            reader.join(timeout=5)
        if overflow or reader.is_alive() or process.returncode:
            raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Docker export failed or exceeded limit")
        return b"".join(chunks)

    def _run_remote(self, entries: list[tuple[str, bytes]]) -> tuple[bool, bytes | None, str]:
        host = self.docker_host.removeprefix("ssh://")
        if not re.fullmatch(r"[A-Za-z0-9_.@-]+", host):
            raise ValueError("Invalid SSH Docker host")
        container = "atoms-build-" + uuid4().hex
        create = self._create_command(container)
        script = "\n".join((
            "set -eu",
            f"trap 'docker rm -f {container} >/dev/null 2>&1 || true' EXIT",
            shlex.join(["docker", *create]) + " >/dev/null",
            f"docker start {container} >/dev/null",
            f"docker exec -i {container} tar -C /opt/runner/site -xf -",
            f"timeout {self.timeout_seconds}s docker exec {container} node /opt/runner/run.mjs >/dev/null 2>&1 || true",
            f"docker exec {container} sh -c " + shlex.quote(
                "if [ -f /tmp/result.json ]; then cp /tmp/result.json /output/.atoms-result.json; "
                "else printf '{\"success\":false,\"diagnostic\":\"Build timed out\"}' > /output/.atoms-result.json; fi"),
            f"docker exec {container} tar -C /output -cf - .",
        ))
        command = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", host,
                   "sh -c " + shlex.quote(script)]
        try:
            completed = subprocess.run(command, input=_source_tar(entries), capture_output=True,
                                       timeout=self.timeout_seconds + 45, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Remote Docker build unavailable") from None
        if completed.returncode or len(completed.stdout) > 26_214_400:
            message = completed.stderr.decode("utf-8", "replace")[:1000]
            raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE,
                              f"Remote Docker build failed: {message}")
        try:
            with tarfile.open(fileobj=io.BytesIO(completed.stdout), mode="r:") as archive:
                result_file = archive.extractfile("./.atoms-result.json")
                if result_file is None:
                    raise ValueError("Missing build status")
                result = json.loads(result_file.read(4096))
        except (tarfile.TarError, ValueError, OSError, KeyError, json.JSONDecodeError):
            raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Invalid remote build status") from None
        if result.get("success") is not True:
            return False, None, str(result.get("diagnostic", "Build failed"))[:2000]
        return True, _pack_tar(completed.stdout), "Build succeeded"

    def _create_command(self, container: str) -> list[str]:
        return ["create", "--name", container,
                "--network", "none", "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges=true", "--pids-limit", "128",
                "--memory", "512m", "--cpus", "1", "--user", "10001:10001",
                "--log-driver", "none",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=8m,mode=1777",
                "--tmpfs", "/opt/runner/site:rw,nosuid,nodev,size=24m,mode=1777",
                "--tmpfs", "/output:rw,nosuid,nodev,size=24m,mode=1777",
                self.image, "sleep", str(self.timeout_seconds + 180)]

    def _run(self, source: bytes) -> tuple[bool, bytes | None, str]:
        if shutil.which("ssh" if self.docker_host else "docker") is None:
            raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Build transport is not installed")
        entries = _entries(source, artifact=False)
        _validate_package(entries)
        if self.docker_host:
            return self._run_remote(entries)
        container = "atoms-build-" + uuid4().hex
        command = self._create_command(container)
        try:
            self._docker(*command)
            self._docker("start", container)
            transferred = subprocess.run(self._command("exec", "-i", container, "tar", "-C",
                "/opt/runner/site", "-xf", "-"), input=_source_tar(entries),
                capture_output=True, timeout=60, check=False)
            if transferred.returncode:
                raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Docker source transfer failed")
            self._docker("exec", "-d", container, "node", "/opt/runner/run.mjs")
            deadline = time.monotonic() + self.timeout_seconds
            while time.monotonic() < deadline:
                try:
                    probe = subprocess.run(self._command("exec", container, "test", "-f",
                        "/tmp/result.json"), capture_output=True, timeout=10, check=False)
                except (OSError, subprocess.TimeoutExpired):
                    raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE,
                                      "Docker build status is unavailable") from None
                if probe.returncode == 0:
                    break
                time.sleep(0.5)
            else:
                return False, None, "Build timed out"
            result = json.loads(self._docker("exec", container, "cat", "/tmp/result.json").stdout)
            if result.get("success") is not True:
                return False, None, str(result.get("diagnostic", "Build failed"))[:2000]
            return True, _pack_tar(self._docker_tar_out(container)), "Build succeeded"
        finally:
            try:
                self._docker("rm", "-f", container, timeout=15)
            except DomainError:
                pass

    async def build(self, command: BuildInput) -> BuildResult:
        if command.template_id != "react-vite-v1":
            raise DomainError(ErrorCode.INVALID_INPUT, "Unsupported build recipe")
        source = await self.blobs.read(command.source_archive)
        success, artifact_bytes, diagnostic = await asyncio.to_thread(self._run, source)
        log = await self.blobs.put(diagnostic.encode("utf-8")[:20_000], "text/plain")
        artifact = await self.blobs.put(artifact_bytes, "application/zip") if artifact_bytes else None
        return BuildResult(version_id=command.version_id, success=success, artifact=artifact,
                           diagnostics=() if success else (diagnostic[:2000],), log=log)

    async def scan(self, version_id: EntityId, artifact: BlobRef) -> ScanResult:
        entries = _entries(await self.blobs.read(artifact), artifact=True)
        names = {name for name, _ in entries}
        findings = []
        if "index.html" not in names:
            findings.append("Preview artifact has no index.html")
        if any(name.endswith((".map", ".env", ".pem", ".key")) for name in names):
            findings.append("Preview artifact contains a prohibited file")
        return ScanResult(version_id=version_id, passed=not findings, findings=tuple(findings))


def create_adapter(settings: Settings, blobs: BlobStore | None = None) -> BuildExecutor:
    if settings.build_backend == "docker":
        if blobs is None:
            raise ValueError("Docker build requires BlobStore")
        return DockerBuildExecutor(blobs, settings.task_timeout_seconds,
                                   docker_host=settings.build_docker_host)
    return UnconfiguredBuildExecutor()
