"""Assemble the secret-free Docker build context from a release archive."""
from __future__ import annotations

import json
import hashlib
import argparse
import re
import shutil
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "runtime-data" / "deploy" / "docker-context"
NAMES = ("Dockerfile", "entrypoint.py", "Caddyfile.private", "Caddyfile.public",
         "requirements.lock", "release.tar.gz")
IMAGE_TAG = re.compile(r"demo-(\d{8})-([1-9]\d*)\Z")


def choose_image_tag(requested: str | None) -> str:
    if requested is not None:
        match = IMAGE_TAG.fullmatch(requested)
        if match is None:
            raise ValueError("Image tag must be demo-YYYYMMDD-N (N starts at 1)")
        try:
            datetime.strptime(match.group(1), "%Y%m%d")
        except ValueError as exc:
            raise ValueError("Image tag contains an invalid calendar date") from exc
        return requested

    date = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")
    prefix = f"demo-{date}-"
    sequence = max((int(match.group(1)) for path in OUTPUT.glob(f"{prefix}*")
                    if path.is_dir() and
                    (match := re.fullmatch(rf"{re.escape(prefix)}([1-9]\d*)", path.name))),
                   default=0) + 1
    return f"{prefix}{sequence}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path,
                        help="Exact release archive; defaults to the newest local release")
    parser.add_argument("--image-tag",
                        help="Immutable readable tag, e.g. demo-20261022-1; defaults to today's next number")
    args = parser.parse_args()
    archives = sorted((ROOT / "runtime-data" / "deploy").glob("atoms-demo-*.tar.gz"),
                      key=lambda path: path.stat().st_mtime, reverse=True)
    archive = args.archive or (archives[0] if archives else None)
    if archive is None or not archive.is_file():
        raise RuntimeError("Run python scripts/package_release.py first or pass --archive")
    with tarfile.open(archive, "r:gz") as source:
        manifest = json.load(source.extractfile("release-manifest.json"))
    release_id = manifest["release_id"]
    source_files = {"release.tar.gz": archive,
                    "requirements.lock": ROOT / "deploy" / "requirements.lock"}
    for name in ("Dockerfile", "entrypoint.py", "Caddyfile.private", "Caddyfile.public"):
        source_files[name] = ROOT / "deploy" / "docker" / name
    digest = hashlib.sha256()
    for name in NAMES:
        digest.update(name.encode() + b"\0" + hashlib.sha256(source_files[name].read_bytes()).digest())
    image_tag = choose_image_tag(args.image_tag)
    context = OUTPUT / image_tag
    OUTPUT.mkdir(parents=True, exist_ok=True)
    if context.exists():
        raise RuntimeError(f"Image tag already has a build context: {image_tag}")
    context.mkdir()
    for name in NAMES:
        shutil.copy2(source_files[name], context / name)
    (context / ".dockerignore").write_text(
        "*\n" + "".join(f"!{name}\n" for name in NAMES), encoding="utf-8")
    print(json.dumps({"context": str(context), "image_tag": image_tag,
                      "content_sha256": digest.hexdigest(),
                      "release_id": release_id,
                      "archive": archive.name}))


if __name__ == "__main__":
    main()
