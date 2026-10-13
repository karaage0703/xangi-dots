import base64
import concurrent.futures
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from xangi_dots.file_client import Client
from xangi_dots.files import CHUNK, MAX_FILE
from xangi_dots.mcp import rpc
from xangi_dots.server import make_server
from xangi_dots.store import Store

ORIGIN = {"platform": "discord", "channel": "file-test"}


class FileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "db.sqlite")

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def create(self, data, key="file", **kw):
        return self.store.create_file(
            key,
            "入力.bin",
            "application/octet-stream",
            len(data),
            hashlib.sha256(data).hexdigest(),
            **kw,
        )

    def upload(self, data, key="file", **kw):
        meta = self.create(data, key, **kw)
        for offset in range(0, len(data), CHUNK):
            self.store.write_file(
                meta["id"],
                offset,
                base64.b64encode(data[offset : offset + CHUNK]).decode(),
                "base64",
                **kw,
            )
        return self.store.finish_file(meta["id"], **kw)

    def job(self, key="job", attachments=None):
        return self.store.enqueue(key, "Process attachments", ORIGIN, attachments=attachments)

    def claim(self, job):
        return self.store.claim(job["id"], "worker")

    def test_binary_roundtrip_restart_and_job_scoping(self):
        data = bytes(range(256)) * 4096  # Exactly 1 MiB, including zero and non-UTF8 bytes.
        incoming = self.upload(data)
        job = self.job(attachments=[incoming["id"]])
        self.assertEqual(self.job(attachments=[incoming["id"]])["id"], job["id"])
        self.assertEqual(self.create(data)["id"], incoming["id"])
        claim = self.claim(job)
        received = bytearray()
        while len(received) < len(data):
            part = self.store.read_file(incoming["id"], len(received), **claim_args(claim))
            received.extend(base64.b64decode(part["data_base64"]))
            self.assertLess(len(json.dumps(part)), 70000)
        self.assertEqual(received, data)
        output = self.upload(data, "output", **claim_args(claim))
        self.store.complete(job["id"], claim["claim_token"], "File saved")
        self.store.db.close()
        self.store = Store(self.root / "db.sqlite")
        self.assertEqual(len(self.store.get(job["id"])["files"]), 2)
        self.assertEqual(
            self.store.read_file(output["id"], 0)["sha256"], hashlib.sha256(data).hexdigest()
        )
        other = self.claim(self.job("other"))
        with self.assertRaises(ValueError):
            self.store.read_file(incoming["id"], 0, **claim_args(other))
        with self.assertRaises(ValueError):
            self.store.write_file(incoming["id"], 0, "x", "utf-8", **claim_args(claim))
        self.store.delete_file(incoming["id"])
        self.assertEqual(len(self.store.get(job["id"])["files"]), 1)

    def test_atomic_binding_and_input_identity(self):
        incoming = self.upload(b"a")
        with self.assertRaises(ValueError):
            self.job(attachments=[incoming["id"], "file_absent"])
        self.assertIsNone(self.store.file_row(incoming["id"])["job_id"])
        self.assertEqual(self.store.jobs(), [])
        job = self.job(attachments=[incoming["id"]])
        with self.assertRaises(ValueError):
            self.job(attachments=[])
        with self.assertRaises(ValueError):
            self.job("other", [incoming["id"]])
        with self.assertRaises(ValueError):
            self.store.delete_file(incoming["id"])
        self.assertEqual(self.store.get(job["id"])["files"][0]["size"], 1)

    def test_chunks_retries_corruption_empty_and_completion(self):
        meta = self.create(b"abcd")
        for _ in range(2):
            self.store.write_file(meta["id"], 0, "ab", "utf-8")
        for offset, text in [(0, "xx"), (3, "d"), (2, "cde")]:
            with self.assertRaises(ValueError):
                self.store.write_file(meta["id"], offset, text, "utf-8")
        with self.assertRaises(ValueError):
            self.store.finish_file(meta["id"])
        with self.assertRaises(ValueError):
            self.job(attachments=[meta["id"]])
        with self.assertRaises(ValueError):
            self.store.read_file(meta["id"], 0)
        self.store.write_file(meta["id"], 2, "cd", "utf-8")
        for _ in range(2):
            self.store.finish_file(meta["id"])
        bad = self.create(b"good", "bad")
        self.store.write_file(bad["id"], 0, "evil", "utf-8")
        with self.assertRaises(ValueError):
            self.store.finish_file(bad["id"])
        empty = self.upload(b"", "empty")
        self.assertTrue(self.store.read_file(empty["id"], 0)["eof"])
        job = self.job()
        claim = self.claim(job)
        output = self.create(b"unfinished", "output", **claim_args(claim))
        with self.assertRaises(ValueError):
            self.store.complete(job["id"], claim["claim_token"], "done")
        self.store.cancel(job["id"])
        with self.assertRaises(ValueError):
            self.store.write_file(output["id"], 0, "unfinished", "utf-8", **claim_args(claim))
        self.store.delete_file(output["id"])

    def test_limits_names_quota_and_expiry(self):
        for name in ["../a", "/etc/passwd", "a\\b", "..", "a\n", "a\u202eb", "a:b"]:
            with self.assertRaises(ValueError):
                self.store.create_file(
                    "bad", name, "text/plain", 0, hashlib.sha256(b"").hexdigest()
                )
        with self.assertRaises(ValueError):
            self.create(b"x" * (MAX_FILE + 1))
        meta = self.create(b"a")
        for data, enc in [("!!", "base64"), ("abc", "invalid"), ("x" * (CHUNK + 1), "utf-8")]:
            with self.assertRaises(ValueError):
                self.store.write_file(meta["id"], 0, data, enc)
        with patch("xangi_dots.files.MAX_STORAGE", 1):
            with self.assertRaises(ValueError):
                self.create(b"x", "quota")
        claim = self.claim(self.job())
        for i in range(8):
            self.upload(b"", str(i), **claim_args(claim))
        with self.assertRaises(ValueError):
            self.upload(b"", "ninth", **claim_args(claim))
        with self.store.db:
            self.store.db.execute("UPDATE jobs SET deadline=0")
        with self.assertRaises(ValueError):
            self.store.create_file(
                "late", "a", "text/plain", 0, hashlib.sha256(b"").hexdigest(), **claim_args(claim)
            )

    def test_concurrent_chunk_retry_and_mcp_text(self):
        meta = self.create(b"abc")
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(
                pool.map(lambda _: self.store.write_file(meta["id"], 0, "abc", "utf-8"), range(8))
            )
        self.assertTrue(all(row["received"] == 3 for row in results))
        claim = self.claim(self.job())
        args = {
            **claim_args(claim),
            "upload_key": "text",
            "name": "レポート.md",
            "mime_type": "text/markdown",
            "text": "# 検証\n日本語のファイルです。\n",
        }
        for _ in range(2):
            result = rpc(
                self.store,
                None,
                {"method": "tools/call", "params": {"name": "save_text_file", "arguments": args}},
            )
            self.assertNotIn("isError", result)
        saved = result["structuredContent"]
        content = self.store.read_file(saved["id"], 0)
        self.assertEqual(base64.b64decode(content["data_base64"]).decode(), args["text"])
        args["text"] = "overwrite"
        self.assertTrue(
            rpc(
                self.store,
                None,
                {"method": "tools/call", "params": {"name": "save_text_file", "arguments": args}},
            )["isError"]
        )

    def test_mcp_cannot_select_parent_file_access_with_null_job(self):
        incoming = self.upload(b"private input")
        for args in [
            {"job_id": None, "claim_token": "x", "file_id": incoming["id"], "offset": 0},
            {"job_id": "job_absent", "claim_token": "x", "file_id": incoming["id"], "offset": True},
        ]:
            with self.assertRaises(ValueError):
                rpc(
                    self.store,
                    None,
                    {"method": "tools/call", "params": {"name": "read_file", "arguments": args}},
                )

    def test_http_and_local_file_client(self):
        server = make_server(self.store, None, "parent")
        thread = threading.Thread(target=server.serve_forever)
        thread.start()

        def request(path, method="GET", body=None):
            req = Request(
                f"http://127.0.0.1:{server.server_port}{path}",
                data=None if body is None else json.dumps(body).encode(),
                method=method,
                headers={"Content-Type": "application/json", "Authorization": "Bearer parent"},
            )
            with urlopen(req) as response:
                return json.load(response)

        try:
            client = Client("/test/xangi")
            client.request = request
            source = self.root / "test.bin"
            source.write_bytes(bytes(range(256)) * 300)
            meta = client.upload(source, "upload", legacy_chunks=True)
            self.assertEqual(client.upload(source, "upload", legacy_chunks=True)["id"], meta["id"])
            out = self.root / "received.bin"
            client.download(meta["id"], out, legacy_chunks=True)
            self.assertEqual(source.read_bytes(), out.read_bytes())
            self.assertEqual(out.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                client.download(meta["id"], out, legacy_chunks=True)
            link = self.root / "symlink.bin"
            link.symlink_to(out)
            with self.assertRaises(FileExistsError):
                client.download(meta["id"], link, legacy_chunks=True)
            with self.assertRaises(HTTPError):
                request("/files", "POST", {"job_id": "injection"})
            self.assertTrue(request(f"/files/{meta['id']}", "DELETE")["deleted"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


def claim_args(claim):
    return {key: claim[key] for key in ("job_id", "claim_token")}
