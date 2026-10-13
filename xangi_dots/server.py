"""Separate loopback listeners: parent bearer API and worker capability MCP."""

import hmac
import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .mcp import rpc
from .transport import encoded


class BoundedServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler):
        super().__init__(address, handler)
        self.slots = threading.BoundedSemaphore(16)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def make_server(
    store, events, token, *, worker=False, port=0, connection=None, native=None, requests=None
):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, *args):
            pass  # No request paths, task text, auth headers, callback URLs, or secrets.

        def reply(self, status, value=None):
            body = encoded(value).encode() if value is not None else b""
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def authorized(self):
            if self.headers.get("Origin"):
                self.reply(403)
                return False
            if worker:
                accepted = hmac.compare_digest(self.path.encode(), ("/mcp/" + token).encode())
            else:
                accepted = hmac.compare_digest(
                    self.headers.get("Authorization", "").encode(), ("Bearer " + token).encode()
                )
            if not accepted:
                self.reply(404 if worker else 401)
            return accepted

        def do_GET(self):
            if worker and native is not None and self.path.startswith("/download/"):
                if self.headers.get("Origin"):
                    return self.reply(403)
                try:
                    stream, size = native.download(self.path)
                except (ValueError, FileNotFoundError):
                    return self.reply(404)
                with stream:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Disposition", 'attachment; filename="attachment.bin"')
                    self.send_header("Content-Length", str(size))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    while part := stream.read(1024 * 1024):
                        self.wfile.write(part)
                self.close_connection = True
                return
            if not self.authorized():
                return
            if worker:
                return self.reply(405)
            store.expire()
            route = urlsplit(self.path)
            if route.path.startswith("/files/"):
                try:
                    query = parse_qs(route.query, strict_parsing=True)
                    if set(query) - {"offset"} or len(query.get("offset", [])) > 1:
                        raise ValueError("Invalid query")
                    return self.reply(
                        200, store.read_file(route.path[7:], int(query.get("offset", ["0"])[0]))
                    )
                except ValueError:
                    return self.reply(400, {"error": "Invalid file or offset"})
            if self.path.startswith("/requests/") and requests is not None:
                try:
                    return self.reply(200, requests.get(self.path[10:], admin=True))
                except ValueError:
                    return self.reply(404)
            if self.path == "/health":
                return self.reply(200, store.health())
            if self.path == "/connection":
                return self.reply(200, connection or {})
            if self.path == "/jobs":
                return self.reply(200, {"jobs": store.jobs(True)})
            if self.path.startswith("/jobs/"):
                try:
                    return self.reply(200, store.get(self.path[6:], True))
                except ValueError:
                    pass
            self.reply(404)

        def read_body(self):
            if (
                self.headers.get("Transfer-Encoding")
                or self.headers.get_content_type() != "application/json"
            ):
                raise ValueError("application/json and Content-Length required")
            n = int(self.headers.get("Content-Length", "0"))
            if not 0 < n <= 262144:
                raise ValueError("Body size must be 1..262144 bytes")
            body = json.loads(self.rfile.read(n))
            if not isinstance(body, dict):
                raise ValueError("Object required")
            return body

        def do_POST(self):
            if not self.authorized():
                return
            msg = {}
            try:
                msg = self.read_body()
                if worker:
                    if msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
                        raise ValueError("Invalid JSON-RPC")
                    if "id" not in msg:
                        return self.reply(202)
                    return self.reply(
                        200,
                        {
                            "jsonrpc": "2.0",
                            "id": msg["id"],
                            "result": rpc(store, events, msg, native, requests),
                        },
                    )
                if self.path.startswith("/requests/") and requests is not None:
                    parts = self.path.split("/")
                    if len(parts) == 5 and parts[3:] == ["files", "import-local"]:
                        if set(msg) != {"claim_token", "path", "upload_key", "mime_type"}:
                            raise ValueError("Invalid output fields")
                        return self.reply(201, requests.import_output(parts[2], **msg))
                    if len(parts) == 4:
                        if parts[3] == "claim" and set(msg) == {"claim_key"}:
                            return self.reply(200, requests.claim(parts[2], **msg))
                        if parts[3] == "result" and set(msg) == {
                            "claim_token",
                            "result",
                            "outcome",
                        }:
                            return self.reply(200, requests.complete(parts[2], **msg))
                        if parts[3] == "cancel" and not msg:
                            return self.reply(200, requests.cancel(parts[2]))
                    raise ValueError("Invalid request action")
                if self.path == "/jobs":
                    if set(msg) - {
                        "request_key",
                        "prompt",
                        "origin",
                        "timeout_seconds",
                        "attachments",
                    }:
                        raise ValueError("Unknown request fields")
                    return self.reply(201, store.enqueue(**msg))
                if self.path == "/files/import-local" and native is not None:
                    if set(msg) != {"path", "upload_key", "mime_type"}:
                        raise ValueError("Invalid import fields")
                    return self.reply(201, native.import_local(**msg))
                if self.path == "/files":
                    if set(msg) != {"upload_key", "name", "mime_type", "size", "sha256"}:
                        raise ValueError("Invalid file fields")
                    return self.reply(201, store.create_file(**msg))
                if self.path.startswith("/files/"):
                    parts = self.path.split("/")
                    if len(parts) == 4 and parts[3] == "export-local" and native is not None:
                        if set(msg) != {"path"}:
                            raise ValueError("Invalid export fields")
                        return self.reply(200, native.export_local(parts[2], msg["path"]))
                    if len(parts) == 4 and parts[3] == "chunks":
                        if set(msg) != {"offset", "data", "encoding"}:
                            raise ValueError("Invalid chunk fields")
                        return self.reply(200, store.write_file(parts[2], **msg))
                    if len(parts) == 4 and parts[3] == "finish" and not msg:
                        return self.reply(200, store.finish_file(parts[2]))
                if self.path.startswith("/jobs/") and self.path.endswith("/cancel"):
                    if msg:
                        raise ValueError("Expected empty object")
                    return self.reply(200, store.cancel(self.path[6:-7]))
                return self.reply(404)
            except (ValueError, TypeError, KeyError, AttributeError):
                # Do not reflect user-controlled malformed content into an error/log.
                error = {"code": -32602, "message": "Invalid request; check fields and job state"}
                if worker:
                    return self.reply(200, {"jsonrpc": "2.0", "id": msg.get("id"), "error": error})
                return self.reply(400, error)
            except FileExistsError:
                return self.reply(409, {"error": "Output already exists; choose a new path"})
            except FileNotFoundError:
                return self.reply(400, {"error": "Local file or output directory not found"})
            except (OSError, http.client.HTTPException):
                return self.reply(502, {"error": "File or callback transport failed"})

        def do_DELETE(self):
            if self.authorized():
                if not worker and self.path.startswith("/files/"):
                    try:
                        return self.reply(200, store.delete_file(self.path[7:]))
                    except ValueError:
                        return self.reply(400, {"error": "Unknown file or active job"})
                self.reply(405)

    return BoundedServer(("127.0.0.1", port), Handler)
