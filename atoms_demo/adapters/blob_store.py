"""Content-addressed local BlobStore with integrity checks."""
import hashlib
import os
import tempfile
import threading
from pathlib import Path
from uuid import uuid4

from atoms_demo.ports.runtime import BlobStore
from atoms_demo.config import Settings
from atoms_demo.core.types import BlobRef, EntityId
from atoms_demo.core.errors import DomainError, ErrorCode

_WRITE_LOCK = threading.Lock()


class LocalBlobStore:
    def __init__(self, directory: Path, max_bytes: int, max_total_bytes: int | None = None):
        self.directory = directory
        self.max_bytes = max_bytes
        self.max_total_bytes = max_total_bytes
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, digest: str) -> Path:
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("Invalid blob digest")
        return self.directory / digest[:2] / digest

    async def put(self, content: bytes, media_type: str) -> BlobRef:
        if not isinstance(content, bytes) or len(content) > self.max_bytes:
            raise ValueError("Blob content exceeds configured limit")
        if not media_type or len(media_type) > 255 or "\n" in media_type or "\r" in media_type:
            raise ValueError("Invalid media type")
        digest = hashlib.sha256(content).hexdigest()
        path = self._path(digest)
        with _WRITE_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                if self.max_total_bytes is not None:
                    used = sum(item.stat().st_size for item in self.directory.glob("*/*") if item.is_file())
                    if used + len(content) > self.max_total_bytes:
                        raise DomainError(ErrorCode.QUOTA_EXCEEDED, "Physical blob quota exceeded")
                descriptor, temporary = tempfile.mkstemp(dir=path.parent)
                try:
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
        return BlobRef(id=EntityId(digest), sha256=digest, size_bytes=len(content), media_type=media_type)

    async def read(self, reference: BlobRef) -> bytes:
        if str(reference.id) != reference.sha256:
            raise ValueError("Blob reference does not match its digest")
        content = self._path(reference.sha256).read_bytes()
        if len(content) != reference.size_bytes or hashlib.sha256(content).hexdigest() != reference.sha256:
            raise ValueError("Blob failed integrity check")
        return content

    async def delete(self, reference: BlobRef) -> None:
        if str(reference.id) != reference.sha256:
            raise ValueError("Blob reference does not match its digest")
        self._path(reference.sha256).unlink(missing_ok=True)


def create_adapter(settings: Settings) -> BlobStore:
    return LocalBlobStore(settings.blob_directory, settings.max_blob_bytes, settings.storage_quota_bytes)
