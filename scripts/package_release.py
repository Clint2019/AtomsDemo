"""Build a minimal, secret-free server release from the local checkout."""
from __future__ import annotations

import hashlib
import gzip
import io
import json
import subprocess
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "runtime-data" / "deploy"


def files() -> list[Path]:
    selected = [ROOT / "main.py", ROOT / "pyproject.toml"]
    selected.extend(path for path in (ROOT / "atoms_demo").rglob("*.py") if path.is_file())
    selected.extend(ROOT / "build-runtime" / name for name in (
        "package.json", "package-lock.json", "export-vite.config.mjs", "local-app-data.mjs"))
    selected.extend(path for path in (ROOT / "frontend" / "dist").rglob("*") if path.is_file())
    if not (ROOT / "frontend" / "dist" / "index.html").is_file():
        raise RuntimeError("Build frontend first: cd frontend; npm run build")
    return sorted(selected, key=lambda path: path.relative_to(ROOT).as_posix())


def main() -> None:
    selected = files()
    source_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    source_paths = [path.relative_to(ROOT).as_posix() for path in selected
                    if not path.is_relative_to(ROOT / "frontend" / "dist")]
    tracked = set(subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--", *source_paths],
        cwd=ROOT).decode().split("\0"))
    if any(path not in tracked for path in source_paths):
        raise RuntimeError("Release source includes files absent from the Git commit")
    changed = subprocess.check_output(
        ["git", "diff", "--name-only", "HEAD", "--", *source_paths],
        cwd=ROOT, text=True).strip()
    if changed:
        raise RuntimeError("Commit source changes before packaging the release")
    digest = hashlib.sha256()
    for path in selected:
        relative = path.relative_to(ROOT).as_posix()
        content = path.read_bytes()
        digest.update(relative.encode() + b"\0" + hashlib.sha256(content).digest())
    release_id = digest.hexdigest()[:16]
    manifest = {"release_id": release_id, "source_commit": source_commit,
                "files": len(selected),
                "sha256": {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                           for path in selected}}
    OUTPUT.mkdir(parents=True, exist_ok=True)
    archive_path = OUTPUT / f"atoms-demo-{release_id}.tar.gz"
    with archive_path.open("wb") as output_file, gzip.GzipFile(
            fileobj=output_file, mode="wb", filename="", mtime=0) as compressed, \
            tarfile.open(fileobj=compressed, mode="w") as archive:
        for path in selected:
            relative = path.relative_to(ROOT).as_posix()
            entry = tarfile.TarInfo(relative)
            content = path.read_bytes()
            entry.size = len(content)
            entry.mode = 0o644
            entry.mtime = 0
            archive.addfile(entry, io.BytesIO(content))
        content = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode()
        entry = tarfile.TarInfo("release-manifest.json")
        entry.size = len(content)
        entry.mode = 0o644
        entry.mtime = 0
        archive.addfile(entry, io.BytesIO(content))
    print(json.dumps({"archive": str(archive_path), "release_id": release_id,
                      "source_commit": source_commit, "files": len(selected)},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
