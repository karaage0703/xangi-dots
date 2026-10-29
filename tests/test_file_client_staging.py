"""Cross-workspace inputs use the native route without relaxing server access."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from xangi_dots.file_client import Client
from xangi_dots.native_files import NativeFiles
from xangi_dots.store import Store


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "db")
        self.addCleanup(self.store.db.close)
        self.native = NativeFiles(self.store, "test", self.root)
        self.client = Client("/test/xangi")
        self.calls = []
        self.client.request = self.request
        self.source = self.root / "input.zip"
        self.source.write_bytes(b"abc" * 1200000)

    def request(self, path, method="GET", body=None):
        self.calls.append((path, body))
        if path == "/files/import-local":
            return self.native.import_local(**body)
        if path.startswith("/jobs/"):
            return self.store.get(path[6:], True)
        route = urlsplit(path)
        return self.store.read_file(route.path[7:], int(parse_qs(route.query)["offset"][0]))

    def test_upload_stages_and_verifies_large_file_idempotently(self):
        meta = self.client.upload(self.source, "input")
        staging = Path(self.calls[0][1]["path"])
        self.assertNotEqual(staging, self.source)
        self.assertEqual(staging.parent.parent, Path("/tmp"))
        self.assertFalse(staging.exists())
        self.assertEqual(meta["size"], 3600000)
        self.assertEqual(meta["id"], self.client.upload(self.source, "input")["id"])
        self.assertTrue(self.calls[1][0].endswith("?offset=3600000"))

    def test_server_rejects_source_but_accepts_staged_copy(self):
        # Simulate a source from another workspace, independently of test cwd.
        original = self.native.local_path

        def restricted(path):
            if Path(path).resolve() == self.source.resolve():
                raise ValueError("File path must be in the workspace or /tmp")
            return original(path)

        with patch.object(self.native, "local_path", side_effect=restricted):
            with self.assertRaises(ValueError):
                self.native.import_local(str(self.source), "direct", "application/zip")
            self.assertEqual(self.client.upload(self.source, "staged")["status"], "ready")

    def test_verify_registered_job_and_reject_missing_attachment(self):
        meta = self.client.upload(self.source, "input")
        origin = {"platform": "discord", "channel": "test"}
        job = self.store.enqueue("with", "test", origin, attachments=[meta["id"]])
        self.assertTrue(self.client.verify_job(meta["id"], job["id"])["attachment_verified"])
        empty = self.store.enqueue("without", "test", origin)
        with self.assertRaisesRegex(ValueError, "attachment missing"):
            self.client.verify_job(meta["id"], empty["id"])

    def test_copy_mutation_stops_before_upload(self):
        import shutil

        original = shutil.copyfile

        def changed(source, target):
            result = original(source, target)
            Path(source).write_bytes(b"changed")
            return result

        with patch("xangi_dots.file_client.shutil.copyfile", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "Input changed"):
                self.client.upload(self.source, "input")
        self.assertEqual(self.calls, [])

    def test_response_hash_mismatch_stops_and_cleans_stage(self):
        def corrupt(path, method="GET", body=None):
            row = self.request(path, method, body)
            if method == "GET":
                row["sha256"] = "0" * 64
            return row

        self.client.request = corrupt
        with self.assertRaisesRegex(ValueError, "mismatch"):
            self.client.upload(self.source, "input")
        self.assertFalse(Path(self.calls[0][1]["path"]).exists())

    def test_native_error_does_not_fall_back_and_cleans_stage(self):
        paths = []

        def fail(path, method="GET", body=None):
            paths.append(Path(body["path"]))
            raise ValueError("import failed")

        self.client.request = fail
        with self.assertRaisesRegex(ValueError, "import failed"):
            self.client.upload(self.source, "input")
        self.assertEqual(len(paths), 1)
        self.assertFalse(paths[0].exists())


if __name__ == "__main__":
    unittest.main()
