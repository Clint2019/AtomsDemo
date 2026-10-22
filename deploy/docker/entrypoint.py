"""Read a root-only mounted secret file, then drop privilege before serving."""
from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path


DATA = Path("/data")
SECRETS = Path("/run/atoms/secret.env")
SOCKET = Path("/var/run/docker.sock")
UID = 10001
GID = 10001


def main() -> None:
    if os.geteuid() != 0:
        raise RuntimeError("Container entrypoint must begin as root")
    if not DATA.is_dir() or DATA.stat().st_uid != UID:
        raise RuntimeError("/data must exist and be owned by UID 10001")
    docker_gid = int(os.environ["ATOMS_DOCKER_GID"])
    socket = SOCKET.stat()
    if not stat.S_ISSOCK(socket.st_mode) or socket.st_gid != docker_gid:
        raise RuntimeError("Docker socket group does not match ATOMS_DOCKER_GID")
    if not SECRETS.is_file():
        raise RuntimeError("Root-only secret.env mount is missing")
    for raw in SECRETS.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise RuntimeError("Invalid secret.env entry")
        os.environ[key] = value
    if not os.environ.get("ATOMS_SECRET_ENCRYPTION_KEY"):
        raise RuntimeError("Encryption key is missing")
    os.setgroups([docker_gid])
    os.setgid(GID)
    os.setuid(UID)
    os.execv(sys.executable, [sys.executable, "/opt/atoms-demo/main.py"])


if __name__ == "__main__":
    main()
