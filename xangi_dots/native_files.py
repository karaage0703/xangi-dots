"""Native ChatGPT file parameters and expiring HTTP downloads, without base64."""

import hashlib
import hmac
import http.client
import ipaddress
import os
import io
import tempfile
import secrets
from pathlib import Path
import socket
import ssl
import stat
import threading
import time
from urllib.parse import parse_qs, urlsplit

from .transport import check_text

MAX_NATIVE_FILE = 512 * 1024 * 1024


def download_native(url, destination):
    """Only the tested OpenAI file CDN, with public DNS, pinned TLS and no redirect."""
    u = urlsplit(check_text(url, "download_url", 16384))
    if (
        u.scheme != "https"
        or not (u.hostname or "").endswith(".oaiusercontent.com")
        or u.port not in (None, 443)
        or u.username
        or u.password
        or u.fragment
    ):
        raise ValueError("Expected HTTPS OpenAI file CDN URL")
    addresses = socket.getaddrinfo(u.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("Non-public file address")
    address = addresses[0][4][0]

    class PinnedHTTPS(http.client.HTTPSConnection):
        def connect(self):
            raw = socket.create_connection((address, 443), timeout=self.timeout)
            try:
                self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
            except Exception:
                raw.close()
                raise

    conn = PinnedHTTPS(u.hostname, timeout=30, context=ssl.create_default_context())
    try:
        conn.request(
            "GET",
            u.path + ("?" + u.query if u.query else ""),
            headers={"Accept-Encoding": "identity"},
        )
        response = conn.getresponse()
        if (
            response.status != 200
            or response.getheader("Content-Encoding", "identity") != "identity"
        ):
            raise ValueError("File download failed or unsupported encoding")
        length = response.getheader("Content-Length")
        if length is not None and not 0 <= int(length) <= MAX_NATIVE_FILE:
            raise ValueError("Native file exceeds 512 MiB")
        size = 0
        deadline = time.monotonic() + 600
        while True:
            if time.monotonic() > deadline:
                raise ValueError("File download deadline exceeded")
            part = response.read(min(65536, MAX_NATIVE_FILE + 1 - size))
            if not part:
                break
            destination.write(part)
            size += len(part)
            if size > MAX_NATIVE_FILE:
                raise ValueError("Native file exceeds 512 MiB")
        if length is not None and size != int(length):
            raise ValueError("Incomplete download")
        return size
    finally:
        conn.close()


class NativeFiles:
    def __init__(self, store, key, workspace, public_base_url=None, downloader=download_native):
        self.store, self.key = store, key.encode()
        self.workspace = Path(workspace).resolve()
        self.downloader = downloader
        self.slots = threading.BoundedSemaphore(2)
        if public_base_url is not None:
            u = urlsplit(public_base_url)
            if (
                u.scheme != "https"
                or not u.hostname
                or u.username
                or u.password
                or u.query
                or u.fragment
                or u.port not in (None, 443)
            ):
                raise ValueError("public_base_url must be a clean HTTPS base URL")
        self.public_base_url = public_base_url.rstrip("/") if public_base_url else None

    def save(self, data, upload_key, name, mime_type, *, job_id=None, claim_token=None):
        return self.save_stream(
            io.BytesIO(data), upload_key, name, mime_type, job_id=job_id, claim_token=claim_token
        )

    def save_stream(
        self,
        source,
        upload_key,
        name,
        mime_type,
        *,
        job_id=None,
        claim_token=None,
        _direction=None,
        _request_id=None,
        _guard=None,
    ):
        with tempfile.TemporaryFile(dir=self.store.file_dir) as staging:
            digest = hashlib.sha256()
            size = 0
            while part := source.read(1024 * 1024):
                size += len(part)
                if size > MAX_NATIVE_FILE:
                    raise ValueError("Native file exceeds 512 MiB")
                digest.update(part)
                staging.write(part)
            staging.flush()
            os.fsync(staging.fileno())
            with self.store.lock:
                if _guard is not None:
                    _guard()
                row = self.store.create_file(
                    upload_key,
                    name,
                    mime_type,
                    size,
                    digest.hexdigest(),
                    job_id=job_id,
                    claim_token=claim_token,
                    _size_limit=MAX_NATIVE_FILE,
                    _direction=_direction,
                    _request_id=_request_id,
                )
                if row["status"] == "ready":
                    return row
                disk_name = row["id"] + "-" + secrets.token_hex(8)
                destination = self.store.file_dir / disk_name
                try:
                    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "wb") as target:
                        staging.seek(0)
                        while part := staging.read(1024 * 1024):
                            target.write(part)
                        target.flush()
                        os.fsync(target.fileno())
                    directory = os.open(self.store.file_dir, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                    with self.store.db:
                        self.store.db.execute(
                            "UPDATE files SET data=?,disk_name=?,status='ready' WHERE id=?",
                            (b"", disk_name, row["id"]),
                        )
                except BaseException:
                    destination.unlink(missing_ok=True)
                    raise
                return self.store.file_row(row["id"])

    def local_path(self, path):
        p = Path(check_text(path, "path", 4096))
        if not p.is_absolute():
            raise ValueError("Expected absolute path")
        resolved = p.resolve()
        if not (resolved.is_relative_to(self.workspace) or resolved.is_relative_to(Path("/tmp"))):
            raise ValueError("File path must be in the workspace or /tmp")
        return p

    def import_local(self, path, upload_key, mime_type, **private):
        p = self.local_path(path)
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Expected a regular file")
            if os.fstat(stream.fileno()).st_size > MAX_NATIVE_FILE:
                raise ValueError("Native file exceeds 512 MiB")
            return self.save_stream(stream, upload_key, p.name, mime_type, **private)

    def export_local(self, file_id, path):
        target = self.local_path(path)
        with self.store.lock:
            row = self.store.file_row(file_id)
            if row["status"] != "ready":
                raise ValueError("File not ready")
            source = self.store.open_file(file_id)
        with source:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                digest = hashlib.sha256()
                size = 0
                with os.fdopen(fd, "wb") as stream:
                    while part := source.read(1024 * 1024):
                        size += len(part)
                        digest.update(part)
                        stream.write(part)
                if size != row["size"] or digest.hexdigest() != row["sha256"]:
                    raise ValueError("File verification failed")
            except BaseException:
                target.unlink()
                raise
        return {"file_id": file_id, "path": str(target), "size": size, "sha256": row["sha256"]}

    def receive_file(self, job_id, claim_token, upload_key, name, mime_type, file):
        self.store.expire()
        with self.store.lock:
            self.store.file_claim(job_id, claim_token, writing=True)
        if not self.slots.acquire(blocking=False):
            raise ValueError("Native transfers busy; retry later")
        try:
            with tempfile.TemporaryFile(dir=self.store.file_dir) as stream:
                self.downloader(file["download_url"], stream)
                stream.seek(0)
                # Recheck the claim and deadline after network I/O.
                return self.save_stream(
                    stream, upload_key, name, mime_type, job_id=job_id, claim_token=claim_token
                )
        finally:
            self.slots.release()

    def get_file_link(self, job_id, claim_token, file_id):
        if not self.public_base_url:
            raise ValueError("Parent must configure public_base_url for native downloads")
        self.store.expire()
        with self.store.lock:
            row = self.store.authorize_file(file_id, job_id, claim_token)
            if row["status"] != "ready":
                raise ValueError("File not ready")
        return self.sign_link(row)

    def sign_link(self, row):
        file_id = row["id"]
        expires = int(time.time()) + 600
        signature = hmac.new(self.key, f"{file_id}:{expires}".encode(), "sha256").hexdigest()
        return {
            **row,
            "download_url": f"{self.public_base_url}/download/{file_id}?expires={expires}&signature={signature}",
            "expires": expires,
        }

    def download(self, path):
        u = urlsplit(path)
        file_id = u.path.removeprefix("/download/")
        params = parse_qs(u.query, strict_parsing=True)
        if set(params) != {"expires", "signature"} or any(len(v) != 1 for v in params.values()):
            raise ValueError("Invalid ticket")
        expires = int(params["expires"][0])
        expected = hmac.new(self.key, f"{file_id}:{expires}".encode(), "sha256").hexdigest()
        if not 0 < expires - time.time() <= 600 or not hmac.compare_digest(
            expected, params["signature"][0]
        ):
            raise ValueError("Expired or invalid ticket")
        self.store.expire()
        with self.store.lock:
            row = self.store.file_row(file_id)
            allowed = False
            if row["job_id"]:
                allowed = self.store.get(row["job_id"])["status"] not in {"cancelled", "expired"}
            elif row["request_id"] and row["direction"] == "reverse_output":
                request = self.store.db.execute(
                    "SELECT status FROM parent_requests WHERE id=?", (row["request_id"],)
                ).fetchone()
                allowed = request is not None and request[0] in {"completed", "failed"}
            if row["status"] != "ready" or not allowed:
                raise ValueError("File not available")
            return self.store.open_file(file_id), row["size"]
