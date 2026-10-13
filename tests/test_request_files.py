import hashlib
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlsplit

from xangi_dots.mcp import REQUEST_TOOLS, rpc
from xangi_dots.native_files import NativeFiles
from xangi_dots.requests import Requests
from xangi_dots.store import Store


class ReverseFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "db")
        self.native = NativeFiles(self.store, "worker", self.root, "https://example.test")
        self.requests = Requests(
            self.store,
            "/test/xangi",
            {"platform": "discord", "channel": "fixed"},
            native=self.native,
        )

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def call(self, tool_name, **args):
        return rpc(
            self.store,
            None,
            {"method": "tools/call", "params": {"name": tool_name, "arguments": args}},
            self.native,
            self.requests,
        )

    def upload(self, key="input"):
        self.native.downloader = lambda _, stream: stream.write(bytes(range(256)) * 4096)
        return self.call(
            "upload_xangi_file",
            upload_key=key,
            name="in.bin",
            mime_type="application/octet-stream",
            file={
                "file_id": "real-file-id",
                "download_url": "https://test.oaiusercontent.com/file",
            },
        )["structuredContent"]

    def enqueue(self, ids, key="request"):
        return self.call(
            "request_xangi",
            request_key=key,
            prompt="process files",
            timeout_seconds=3600,
            attachments=ids,
        )["structuredContent"]

    def test_binary_roundtrip_mcp_restart_and_delete(self):
        incoming = self.upload()
        self.assertEqual(self.upload()["id"], incoming["id"])
        request = self.enqueue([incoming["id"]])
        self.assertEqual(self.enqueue([incoming["id"]])["id"], request["id"])
        self.assertEqual(self.upload()["id"], incoming["id"])
        token = self.requests.claim(request["id"], "parent")
        source = self.root / "input.bin"
        self.native.export_local(incoming["id"], str(source))
        self.assertEqual(source.read_bytes(), bytes(range(256)) * 4096)
        source.write_bytes(source.read_bytes() + b"processed")
        output = self.requests.import_output(
            **token, path=str(source), upload_key="output", mime_type="application/octet-stream"
        )
        self.assertEqual(
            self.requests.import_output(
                **token, path=str(source), upload_key="output", mime_type="application/octet-stream"
            )["id"],
            output["id"],
        )
        self.assertTrue(
            self.call("get_xangi_file_link", request_id=request["id"], file_id=output["id"])[
                "isError"
            ]
        )
        self.requests.complete(**token, result="processed", outcome="completed")
        self.store.db.close()
        self.store = Store(self.root / "db")
        self.native = NativeFiles(self.store, "worker", self.root, "https://example.test")
        self.requests = Requests(
            self.store,
            "/test/xangi",
            {"platform": "discord", "channel": "fixed"},
            native=self.native,
        )
        result = self.call("get_xangi_request", request_id=request["id"])["structuredContent"]
        self.assertEqual(
            [f["direction"] for f in result["files"]], ["reverse_input", "reverse_output"]
        )
        link = self.call("get_xangi_file_link", request_id=request["id"], file_id=output["id"])[
            "structuredContent"
        ]
        url = urlsplit(link["download_url"])
        stream, size = self.native.download(url.path + "?" + url.query)
        with stream:
            raw = stream.read()
        self.assertEqual(raw, source.read_bytes())
        self.assertEqual(hashlib.sha256(raw).hexdigest(), output["sha256"])
        self.assertEqual(size, len(raw))
        self.store.delete_file(output["id"])
        with self.assertRaises(ValueError):
            self.native.download(url.path + "?" + url.query)

    def test_scoping_atomic_bind_and_cancelled_output(self):
        incoming = self.upload()
        with self.assertRaises(ValueError):
            self.requests.enqueue("bad", "bad", 3600, [incoming["id"], "file_missing"])
        self.assertIsNone(self.store.file_row(incoming["id"])["request_id"])
        with self.assertRaises(ValueError):
            self.store.enqueue(
                "ordinary",
                "test",
                {"platform": "discord", "channel": "fixed"},
                attachments=[incoming["id"]],
            )
        request = self.enqueue([incoming["id"]])
        with self.assertRaises(ValueError):
            self.requests.enqueue("other", "test", 3600, [incoming["id"]])
        with self.assertRaises(ValueError):
            self.store.delete_file(incoming["id"])
        token = self.requests.claim(request["id"], "parent")
        source = self.root / "out.bin"
        source.write_bytes(b"answer")
        with self.assertRaises(ValueError):
            self.requests.import_output(
                request["id"], "wrong", str(source), "out", "application/octet-stream"
            )
        output = self.requests.import_output(
            **token, path=str(source), upload_key="out", mime_type="application/octet-stream"
        )
        other = self.enqueue([], "other")
        with self.assertRaises(ValueError):
            self.requests.file_link(other["id"], output["id"])
        self.requests.cancel(request["id"])
        with self.assertRaises(ValueError):
            self.requests.import_output(
                **token, path=str(source), upload_key="late", mime_type="application/octet-stream"
            )
        self.assertTrue(
            self.call("get_xangi_file_link", request_id=request["id"], file_id=output["id"])[
                "isError"
            ]
        )
        self.store.delete_file(output["id"])

    def test_schema_attachment_count_and_wrong_direction(self):
        tool = next(t for t in REQUEST_TOOLS if t["name"] == "upload_xangi_file")
        self.assertEqual(tool["_meta"], {"openai/fileParams": ["file"]})
        ids = [self.upload(str(i))["id"] for i in range(8)]
        with self.assertRaises(ValueError):
            self.requests.enqueue("duplicate", "test", 3600, [ids[0], ids[0]])
        request = self.enqueue(ids)
        token = self.requests.claim(request["id"], "parent")
        source = self.root / "out.bin"
        source.write_bytes(b"out")
        with self.assertRaises(ValueError):
            self.requests.import_output(
                **token, path=str(source), upload_key="extra", mime_type="application/octet-stream"
            )
        ordinary = self.native.save(b"ordinary", "ordinary", "in.txt", "text/plain")
        with self.assertRaises(ValueError):
            self.requests.enqueue("ordinary", "test", 3600, [ordinary["id"]])
