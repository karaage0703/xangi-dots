import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from xangi_dots.mcp import rpc
from xangi_dots.requests import Requests
from xangi_dots.server import make_server
from xangi_dots.store import Store


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "db"
        self.store = Store(self.path)
        self.calls = []
        self.requests = Requests(
            self.store, "/test/xangi", {"platform": "discord", "channel": "fixed"}, self.run_cli
        )

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def run_cli(self, args, **kwargs):
        self.calls.append(args)
        if args[2] == "trigger":
            return subprocess.CompletedProcess(
                args, 0, "Triggered (id: trigger-test, source: test)"
            )
        return subprocess.CompletedProcess(
            args, 0, json.dumps({"triggerId": "trigger-test", "status": "delivered"})
        )

    def call(self, name, **arguments):
        return rpc(
            self.store,
            None,
            {"method": "tools/call", "params": {"name": name, "arguments": arguments}},
            requests=self.requests,
        )

    def enqueue(self, key="one", prompt="17 * 23"):
        return self.call("request_xangi", request_key=key, prompt=prompt, timeout_seconds=3600)[
            "structuredContent"
        ]

    def test_roundtrip_and_no_prompt_in_trigger(self):
        row = self.enqueue(prompt="UNTRUSTED_TASK_TEXT")
        self.requests.tick()
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("UNTRUSTED_TASK_TEXT", str(self.calls))
        self.assertEqual(self.calls[0][self.calls[0].index("--channel") + 1], "fixed")
        token = self.requests.claim(row["id"], "parent")
        self.assertEqual(self.requests.claim(row["id"], "parent"), token)
        with self.assertRaises(ValueError):
            self.requests.claim(row["id"], "other")
        self.requests.complete(**token, result="391", outcome="completed")
        reply = self.call("get_xangi_request", request_id=row["id"])["structuredContent"]
        self.assertEqual((reply["status"], reply["result"]), ("completed", "391"))
        for private in ("destination", "claim_token", "claim_key", "trigger_id"):
            self.assertNotIn(private, reply)
        self.requests.complete(**token, result="391", outcome="completed")
        with self.assertRaises(ValueError):
            self.requests.complete(**token, result="different", outcome="completed")

    def test_idempotency_frozen_destination_and_expiry(self):
        row = self.enqueue()
        self.requests.destination = {"platform": "discord", "channel": "changed"}
        self.assertEqual(self.enqueue()["id"], row["id"])
        self.assertTrue(
            self.call("request_xangi", request_key="one", prompt="changed", timeout_seconds=3600)[
                "isError"
            ]
        )
        self.requests.tick()
        self.assertEqual(self.calls[0][self.calls[0].index("--channel") + 1], "fixed")
        token = self.requests.claim(row["id"], "one")
        with patch("xangi_dots.requests.time.time", return_value=10**12):
            self.assertEqual(self.requests.get(row["id"])["status"], "expired")
            with self.assertRaises(ValueError):
                self.requests.complete(**token, result="late", outcome="completed")

    def test_restart_ambiguous_dispatch_and_delivery_is_not_completion(self):
        row = self.enqueue()
        self.requests.dispatch_update(row["id"], "sending")
        self.store.db.close()
        self.store = Store(self.path)
        self.requests = Requests(
            self.store, "/test/xangi", {"platform": "discord", "channel": "fixed"}, self.run_cli
        )
        self.requests.tick()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.requests.get(row["id"])["dispatch"], "needs_review")
        second = self.enqueue("two")
        self.requests.tick()
        self.store.db.execute("UPDATE parent_requests SET next_check=0")
        self.store.db.commit()
        self.requests.tick()
        result = self.requests.get(second["id"])
        self.assertEqual(result["dispatch"], "needs_review")
        self.assertEqual(result["status"], "queued")
        self.assertIsNone(result["result"])

    def test_disabled_validation_failure_and_cancel(self):
        disabled = Requests(self.store)
        tools = rpc(self.store, None, {"method": "tools/list"}, requests=disabled)["tools"]
        self.assertNotIn("request_xangi", [t["name"] for t in tools])
        with self.assertRaises(ValueError):
            disabled.enqueue("one", "no", 3600)
        with self.assertRaises(ValueError):
            Requests(self.store, destination={"platform": "discord", "channel": "fixed"})
        for timeout in (0, True, 86401):
            with self.assertRaises(ValueError):
                self.requests.enqueue("bad", "test", timeout)
        row = self.enqueue()
        self.requests.cancel(row["id"])
        self.requests.tick()
        self.assertEqual(self.calls, [])
        with self.assertRaises(ValueError):
            self.requests.claim(row["id"], "late")
        second = self.enqueue("two")
        self.requests.runner = lambda *a, **k: subprocess.CompletedProcess(a, 1, "SECRET")
        self.requests.tick()
        result = self.requests.get(second["id"])
        self.assertEqual(result["dispatch"], "needs_review")
        self.assertNotIn("SECRET", str(result))

    def test_http_worker_cannot_claim_or_write_parent_result(self):
        admin = make_server(self.store, None, "parent", requests=self.requests)
        worker = make_server(self.store, None, "worker", worker=True, requests=self.requests)
        threads = [threading.Thread(target=s.serve_forever) for s in (admin, worker)]
        for t in threads:
            t.start()

        def post(server, path, body, token="parent"):
            req = Request(
                f"http://127.0.0.1:{server.server_port}{path}",
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
            )
            with urlopen(req) as response:
                return json.load(response)

        try:
            reply = post(
                worker,
                "/mcp/worker",
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "request_xangi",
                        "arguments": {
                            "request_key": "http",
                            "prompt": "17 * 23",
                            "timeout_seconds": 3600,
                        },
                    },
                },
            )
            request_id = reply["result"]["structuredContent"]["id"]
            with self.assertRaises(HTTPError):
                post(worker, f"/requests/{request_id}/claim", {"claim_key": "bad"}, "worker")
            with self.assertRaises(HTTPError):
                post(admin, f"/requests/{request_id}/claim", {"claim_key": "bad"}, "worker")
            token = post(admin, f"/requests/{request_id}/claim", {"claim_key": "parent"})[
                "claim_token"
            ]
            post(
                admin,
                f"/requests/{request_id}/result",
                {"claim_token": token, "result": "391", "outcome": "completed"},
            )
            reply = post(
                worker,
                "/mcp/worker",
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "get_xangi_request",
                        "arguments": {"request_id": request_id},
                    },
                },
            )
            self.assertEqual(reply["result"]["structuredContent"]["result"], "391")
        finally:
            for s in (admin, worker):
                s.shutdown()
                s.server_close()
            for t in threads:
                t.join()

    def test_worker_cannot_override_destination_or_forge_result(self):
        with self.assertRaises(ValueError):
            self.call(
                "request_xangi",
                request_key="bad",
                prompt="test",
                timeout_seconds=3600,
                destination={"platform": "discord", "channel": "other"},
            )
        with self.assertRaises(ValueError):
            self.call(
                "complete", request_id="any", claim_token="fake", result="fake", outcome="completed"
            )
        row = self.enqueue()
        token = self.requests.claim(row["id"], "parent")
        with self.assertRaises(ValueError):
            self.requests.complete(row["id"], "fake", "fake", "completed")
        self.requests.complete(**token, result="Approval needed", outcome="failed")
        self.assertEqual(self.requests.get(row["id"])["status"], "failed")

    def test_concurrent_retries_dispatch_once(self):
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(lambda _: self.enqueue(), range(8)))
        self.assertEqual(len({r["id"] for r in rows}), 1)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: self.requests.tick(), range(4)))
        self.assertEqual(len(self.calls), 1)
