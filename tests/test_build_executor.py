"""Trust-boundary checks for source and preview ZIPs before Docker is available."""
import asyncio
import io
import json
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from atoms_demo.adapters.blob_store import LocalBlobStore
from atoms_demo.adapters.build_executor import (
    DockerBuildExecutor, _entries, _pack_tar, _source_tar, _validate_package,
)
from atoms_demo.core.errors import DomainError, ErrorCode
from atoms_demo.core.types import EntityId


def archive(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as result:
        for name, content in files.items():
            result.writestr(name, content)
    return stream.getvalue()


class BuildArchiveTests(unittest.TestCase):
    def test_rejects_unsafe_source_before_extraction(self):
        for name in ("../escape", "src/../escape", "node_modules/pkg/x.js", "C:/escape",
                     "CON", "NUL.txt", "file."):
            with self.subTest(name=name), self.assertRaises(DomainError):
                _entries(archive({name: "bad"}), artifact=False)
        with self.assertRaises(DomainError):
            _entries(archive({"a": "file", "a/b.js": "nested"}), artifact=False)

    def test_package_scripts_and_unlisted_dependencies_rejected(self):
        for package in ({"scripts": {"build": "touch /tmp/unsafe"}},
                        {"dependencies": {"unsafe-package": "1.0.0"}}):
            with self.subTest(package=package), self.assertRaises(DomainError):
                _validate_package(_entries(archive({"package.json": json.dumps(package)}), artifact=False))

    def test_exported_tar_is_checked_before_preview_zip(self):
        packed = _pack_tar(_source_tar([("index.html", b"<h1>ok</h1>"),
                                        ("assets/app.js", b"console.log(1)")]))
        self.assertEqual({name for name, _ in _entries(packed, artifact=True)},
                         {"index.html", "assets/app.js"})
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as result:
            link = tarfile.TarInfo("index.html")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            result.addfile(link)
        with self.assertRaises(DomainError):
            _pack_tar(stream.getvalue())

    def test_scan_requires_site_and_rejects_secret_files(self):
        async def run():
            with tempfile.TemporaryDirectory() as temporary:
                blobs = LocalBlobStore(Path(temporary), 20_971_520)
                executor = DockerBuildExecutor(blobs, 30)
                for files in ({"assets/app.js": "ok"}, {"index.html": "ok", ".env": "secret"}):
                    reference = await blobs.put(archive(files), "application/zip")
                    result = await executor.scan(EntityId("version"), reference)
                    self.assertFalse(result.passed)
                reference = await blobs.put(archive({"index.html": "<h1>ok</h1>"}), "application/zip")
                result = await executor.scan(EntityId("version"), reference)
                self.assertTrue(result.passed)
        asyncio.run(run())

    def test_build_requires_fixed_recipe(self):
        async def run():
            with tempfile.TemporaryDirectory() as temporary:
                executor = DockerBuildExecutor(LocalBlobStore(Path(temporary), 20_971_520), 30)
                from atoms_demo.contracts.builds import BuildInput
                with self.assertRaises(DomainError) as caught:
                    await executor.build(BuildInput(task_id=EntityId("task"), version_id=EntityId("version"),
                        source_archive=None, template_id="custom"))
                self.assertEqual(caught.exception.code, ErrorCode.INVALID_INPUT)
        asyncio.run(run())
