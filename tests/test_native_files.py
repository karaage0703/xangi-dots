import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from xangi_dots.file_client import Client
from xangi_dots.mcp import TOOLS, rpc
from xangi_dots.native_files import MAX_NATIVE_FILE, NativeFiles, download_native
from xangi_dots.server import make_server
from xangi_dots.store import Store


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "db")
        self.native = NativeFiles(self.store, "key", self.root, "https://example.test/prefix")

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def job(self, attachments=None):
        job = self.store.enqueue(
            "one",
            "native test",
            {"platform": "discord", "channel": "test"},
            attachments=attachments,
        )
        claim = self.store.claim(job["id"], "claim")
        return {k: claim[k] for k in ("job_id", "claim_token")}

    def test_five_mib_native_file_roundtrip_and_restart(self):
        raw = b"abcde" * 1024 * 1024
        source = self.root / "input.bin"
        source.write_bytes(raw)
        incoming = self.native.import_local(str(source), "in", "application/octet-stream")
        scope = self.job([incoming["id"]])
        link = self.native.get_file_link(**scope, file_id=incoming["id"])
        target = urlsplit(link["download_url"])
        stream, size = self.native.download(
            target.path.removeprefix("/prefix") + "?" + target.query
        )
        with stream:
            self.assertEqual(stream.read(), raw)
        self.assertEqual(size, len(raw))
        self.native.downloader = lambda _, stream: stream.write(raw + b"receipt")
        args = {
            **scope,
            "upload_key": "out",
            "name": "out.bin",
            "mime_type": "application/octet-stream",
            "file": {
                "download_url": "https://files.oaiusercontent.com/signed",
                "file_id": "native-id",
            },
        }
        reply = rpc(
            self.store,
            None,
            {"method": "tools/call", "params": {"name": "receive_file", "arguments": args}},
            self.native,
        )
        self.assertNotIn("isError", reply)
        output = reply["structuredContent"]
        self.assertEqual(output["size"], len(raw) + 7)
        self.assertEqual(self.native.receive_file(**args)["id"], output["id"])
        self.store.complete(scope["job_id"], scope["claim_token"], "saved")
        self.store.db.close()
        self.store = Store(self.root / "db")
        native = NativeFiles(self.store, "key", self.root)
        out = self.root / "received.bin"
        result = native.export_local(output["id"], str(out))
        self.assertEqual(out.read_bytes(), raw + b"receipt")
        self.assertEqual(result["sha256"], hashlib.sha256(out.read_bytes()).hexdigest())
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            native.export_local(output["id"], str(out))

    def test_ticket_tampering_expiry_cancel_and_key_rotation(self):
        incoming = self.native.save(b"hello", "in", "in.txt", "text/plain")
        scope = self.job([incoming["id"]])
        url = self.native.get_file_link(**scope, file_id=incoming["id"])["download_url"]
        u = urlsplit(url)
        path = u.path.removeprefix("/prefix") + "?" + u.query
        for bad in [path + "a", path.replace(incoming["id"], "file_absent"), path + "&expires=1"]:
            with self.assertRaises(ValueError):
                self.native.download(bad)
        with patch("xangi_dots.native_files.time.time", return_value=10**12):
            with self.assertRaises(ValueError):
                self.native.download(path)
        with self.assertRaises(ValueError):
            NativeFiles(self.store, "rotated", self.root).download(path)
        self.store.cancel(scope["job_id"])
        with self.assertRaises(ValueError):
            self.native.download(path)

    def test_native_limits_paths_and_claim_recheck(self):
        with patch("xangi_dots.native_files.MAX_NATIVE_FILE", 16):
            with self.assertRaises(ValueError):
                self.native.save(b"x" * 17, "big", "big.bin", "application/octet-stream")
        oversized = self.root / "too-large.bin"
        with oversized.open("wb") as stream:
            stream.truncate(MAX_NATIVE_FILE + 1)
        with self.assertRaises(ValueError):
            self.native.import_local(str(oversized), "oversize", "application/octet-stream")
        with self.assertRaises(ValueError):
            self.native.import_local("/etc/passwd", "outside", "text/plain")
        with self.assertRaises(ValueError):
            self.native.import_local("relative.txt", "relative", "text/plain")
        link = self.root / "link"
        link.symlink_to("/etc/passwd")
        with self.assertRaises(ValueError):
            self.native.import_local(str(link), "link", "text/plain")
        scope = self.job()

        def cancel_while_downloading(_, stream):
            self.store.cancel(scope["job_id"])
            stream.write(b"late")

        self.native.downloader = cancel_while_downloading
        with self.assertRaises(ValueError):
            self.native.receive_file(
                **scope,
                upload_key="late",
                name="late.txt",
                mime_type="text/plain",
                file={"download_url": "unused"},
            )
        self.assertEqual(self.store.get(scope["job_id"])["files"], [])

    def test_native_schema_and_url_transport_guards(self):
        tool = next(t for t in TOOLS if t["name"] == "receive_file")
        self.assertEqual(tool["_meta"]["openai/fileParams"], ["file"])
        schema = tool["inputSchema"]["properties"]["file"]
        self.assertEqual(set(schema["required"]), {"download_url", "file_id"})
        self.assertEqual(
            set(schema["properties"]), {"download_url", "file_id", "mime_type", "file_name"}
        )
        for url in [
            "http://x.oaiusercontent.com/file",
            "https://evil.test/file",
            "https://oaiusercontent.com.evil.test/file",
            "https://u:p@x.oaiusercontent.com/file",
            "https://x.oaiusercontent.com:444/file",
        ]:
            with self.assertRaises(ValueError):
                download_native(url, io.BytesIO())
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaises(ValueError):
                download_native("https://x.oaiusercontent.com/file", io.BytesIO())

        class Response(io.BytesIO):
            status = 200
            headers = {"Content-Length": "4"}

            def getheader(self, name, default=None):
                return self.headers.get(name, default)

        class Connection:
            def __init__(self, *args, **kwargs):
                pass

            def request(self, *args, **kwargs):
                pass

            def getresponse(self):
                return Response(b"data")

            def close(self):
                pass

        with (
            patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("8.8.8.8", 443))]),
            patch("http.client.HTTPSConnection", Connection),
        ):
            self.assertEqual(download_native("https://x.oaiusercontent.com/file", io.BytesIO()), 4)
            with patch.object(Response, "status", 302):
                with self.assertRaises(ValueError):
                    download_native("https://x.oaiusercontent.com/file", io.BytesIO())
            with patch.object(Response, "headers", {"Content-Length": str(MAX_NATIVE_FILE + 1)}):
                with self.assertRaises(ValueError):
                    download_native("https://x.oaiusercontent.com/file", io.BytesIO())

    def test_http_native_helper_and_worker_scope(self):
        admin = make_server(self.store, None, "parent", native=self.native)
        worker = make_server(self.store, None, "worker", worker=True, native=self.native)
        threads = [threading.Thread(target=s.serve_forever) for s in (admin, worker)]
        for t in threads:
            t.start()

        def request(path, method="GET", body=None):
            req = Request(
                f"http://127.0.0.1:{admin.server_port}{path}",
                method=method,
                data=None if body is None else json.dumps(body).encode(),
                headers={"Authorization": "Bearer parent", "Content-Type": "application/json"},
            )
            with urlopen(req) as response:
                return json.load(response)

        try:
            client = Client("/test/xangi")
            client.request = request
            src = self.root / "src.bin"
            with src.open("wb") as stream:
                for _ in range(80):
                    stream.write(b"a" * 1024 * 1024)
            meta = client.upload(src, "input")
            out = self.root / "out.bin"
            client.download(meta["id"], out)
            self.assertEqual(src.read_bytes(), out.read_bytes())
            scope = self.job([meta["id"]])
            u = urlsplit(self.native.get_file_link(**scope, file_id=meta["id"])["download_url"])
            with urlopen(
                f"http://127.0.0.1:{worker.server_port}"
                + u.path.removeprefix("/prefix")
                + "?"
                + u.query
            ) as response:
                self.assertEqual(response.read(), src.read_bytes())
            with self.assertRaises(HTTPError):
                urlopen(
                    Request(
                        f"http://127.0.0.1:{worker.server_port}/files/import-local",
                        data=b"{}",
                        headers={"Content-Type": "application/json"},
                    )
                )
        finally:
            for s in (admin, worker):
                s.shutdown()
                s.server_close()
            for t in threads:
                t.join()

    def test_large_streaming_roundtrip_without_blob_and_bounded_memory(self):
        import tracemalloc

        source = self.root / "large.bin"
        block = bytes(range(256)) * 4096
        with source.open("wb") as stream:
            for _ in range(80):
                stream.write(block)
        tracemalloc.start()
        incoming = self.native.import_local(str(source), "large", "application/octet-stream")
        scope = self.job([incoming["id"]])
        u = urlsplit(self.native.get_file_link(**scope, file_id=incoming["id"])["download_url"])
        stream, size = self.native.download(u.path.removeprefix("/prefix") + "?" + u.query)
        with stream:
            received = self.native.save_stream(
                stream, "returned", "returned.bin", "application/octet-stream", **scope
            )
        target = self.root / "large-return.bin"
        self.native.export_local(received["id"], str(target))
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertEqual(size, 80 * 1024 * 1024)
        self.assertLess(peak, 12 * 1024 * 1024)
        with source.open("rb") as a, target.open("rb") as b:
            self.assertEqual(
                hashlib.file_digest(a, "sha256").digest(), hashlib.file_digest(b, "sha256").digest()
            )
        self.assertEqual(
            self.store.db.execute("SELECT sum(length(data)) FROM files").fetchone()[0], 0
        )
        self.assertLess((self.root / "db").stat().st_size, 1024 * 1024)
        self.store.complete(scope["job_id"], scope["claim_token"], "done")
        self.store.delete_file(received["id"])
        self.assertEqual(len(list(self.store.file_dir.iterdir())), 1)

    def test_existing_blob_database_remains_readable(self):
        # Emulate the pre-disk schema, then reopen with the new implementation.
        row = self.store.create_file(
            "old", "old.txt", "text/plain", 3, hashlib.sha256(b"old").hexdigest()
        )
        self.store.write_file(row["id"], 0, "old", "utf-8")
        self.store.finish_file(row["id"])
        self.store.db.execute("ALTER TABLE files DROP COLUMN disk_name")
        self.store.db.commit()
        self.store.db.close()
        self.store = Store(self.root / "db")
        with self.store.open_file(row["id"]) as stream:
            self.assertEqual(stream.read(), b"old")

    def test_failed_persist_is_not_published_and_export_detects_corruption(self):
        with patch("xangi_dots.native_files.os.fsync", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                self.native.save(b"data", "failed", "failed.txt", "text/plain")
        self.assertEqual(list(self.store.file_dir.iterdir()), [])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM files").fetchone()[0], 0)
        row = self.native.save(b"data", "ok", "ok.txt", "text/plain")
        next(self.store.file_dir.iterdir()).write_bytes(b"oops")
        target = self.root / "corrupt-export.txt"
        with self.assertRaises(ValueError):
            self.native.export_local(row["id"], str(target))
        self.assertFalse(target.exists())
