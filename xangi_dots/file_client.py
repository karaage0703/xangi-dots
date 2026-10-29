"""Local file transfer through the target xangi CLI; credentials stay with xangi."""

import argparse
import base64
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import subprocess
import tempfile
import shutil
import re

from .native_files import MAX_NATIVE_FILE

from .files import CHUNK, MAX_FILE


class Client:
    def __init__(self, cli):
        if not Path(cli).is_absolute():
            raise ValueError("--xangi-cli must be an absolute path")
        self.cli = cli

    def request(self, path, method="GET", body=None):
        args = [
            self.cli,
            "tool",
            "extension_request",
            "--id",
            "xangi-dots",
            "--capability",
            "dots.jobs",
            "--path",
            path,
            "--method",
            method,
        ]
        if body is not None:
            args += ["--body-json", json.dumps(body, ensure_ascii=True)]
        result = subprocess.run(args, capture_output=True, text=True, timeout=45)
        if result.returncode:
            # Never echo CLI output or file contents on failure.
            raise ValueError(
                f"xangi request failed (exit {result.returncode}); inspect job/file state"
            )
        return json.loads(result.stdout)

    def upload(self, path, upload_key, mime_type=None, legacy_chunks=False):
        source = Path(path)
        if not legacy_chunks:
            source = source.resolve(strict=True)
            if not source.is_file() or source.stat().st_size > MAX_NATIVE_FILE:
                raise ValueError("Expected a regular file no larger than 512 MiB")
            with source.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            # TMPDIR may point outside the extension workspace. Keep a private,
            # real copy in the shared /tmp allowlist; preserve the name for retries.
            with tempfile.TemporaryDirectory(prefix="xangi-dots-input-", dir="/tmp") as stage:
                staged = Path(stage) / source.name
                shutil.copyfile(source, staged)
                size = staged.stat().st_size
                with staged.open("rb") as stream:
                    copied = hashlib.file_digest(stream, "sha256").hexdigest()
                with source.open("rb") as stream:
                    current = hashlib.file_digest(stream, "sha256").hexdigest()
                if copied != digest or current != digest or size > MAX_NATIVE_FILE:
                    raise ValueError("Input changed while staging; use a fixed snapshot")
                meta = self.request(
                    "/files/import-local",
                    "POST",
                    {
                        "path": str(staged),
                        "upload_key": upload_key,
                        "mime_type": mime_type
                        or mimetypes.guess_type(source.name)[0]
                        or "application/octet-stream",
                    },
                )
                self.verify_input_metadata(meta, size, digest)
                confirmed = self.request(f"/files/{meta['id']}?offset={size}")
                self.verify_input_metadata(confirmed, size, digest)
                if confirmed["id"] != meta["id"]:
                    raise ValueError("File identity changed after upload")
                return meta
        with source.open("rb") as stream:
            raw = stream.read(MAX_FILE + 1)
        if len(raw) > MAX_FILE:
            raise ValueError("File exceeds 1 MiB")
        meta = self.request(
            "/files",
            "POST",
            {
                "upload_key": upload_key,
                "name": source.name,
                "mime_type": mime_type
                or mimetypes.guess_type(source.name)[0]
                or "application/octet-stream",
                "size": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            },
        )
        if meta["status"] == "ready":
            return meta
        for offset in range(meta["received"], len(raw), CHUNK):
            self.request(
                f"/files/{meta['id']}/chunks",
                "POST",
                {
                    "offset": offset,
                    "encoding": "base64",
                    "data": base64.b64encode(raw[offset : offset + CHUNK]).decode(),
                },
            )
        return self.request(f"/files/{meta['id']}/finish", "POST", {})

    @staticmethod
    def verify_input_metadata(meta, size, digest):
        if not isinstance(meta, dict) or not re.fullmatch(r"file_[0-9a-f]{24}", meta.get("id", "")):
            raise ValueError("Invalid input file ID")
        if any(
            meta.get(k) != v
            for k, v in {
                "direction": "input",
                "status": "ready",
                "size": size,
                "sha256": digest,
            }.items()
        ):
            raise ValueError("Input status, size or SHA-256 mismatch")

    def verify_job(self, file_id, job_id):
        if not re.fullmatch(r"file_[0-9a-f]{24}", file_id) or not re.fullmatch(
            r"job_[0-9a-f]{24}", job_id
        ):
            raise ValueError("Invalid file or job ID")
        job = self.request(f"/jobs/{job_id}")
        rows = [row for row in job.get("files", []) if row.get("id") == file_id]
        if len(rows) != 1 or rows[0].get("job_id") != job_id:
            raise ValueError("Input attachment missing from job; do not create a duplicate job")
        meta = rows[0]
        self.verify_input_metadata(meta, meta["size"], meta["sha256"])
        confirmed = self.request(f"/files/{file_id}?offset={meta['size']}")
        self.verify_input_metadata(confirmed, meta["size"], meta["sha256"])
        if confirmed["id"] != file_id or confirmed.get("job_id") != job_id:
            raise ValueError("Input job binding changed")
        return {"job_id": job_id, "file_id": file_id, "attachment_verified": True}

    def download(self, file_id, output, legacy_chunks=False):
        # O_EXCL rejects existing files and symlinks. The caller chooses the path;
        # a worker-supplied filename never becomes a host path or executable.
        if (
            not isinstance(file_id, str)
            or not file_id.startswith("file_")
            or not all(c in "0123456789abcdef" for c in file_id[5:])
            or len(file_id) != 29
        ):
            raise ValueError("Invalid file ID")
        target = Path(output)
        if not legacy_chunks:
            result = self.request(
                f"/files/{file_id}/export-local", "POST", {"path": str(target.absolute())}
            )
            with target.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if target.stat().st_size != result["size"] or digest != result["sha256"]:
                raise ValueError("Export verification failed")
            return result
        collected = bytearray()
        expected = None
        while True:
            part = self.request(f"/files/{file_id}?offset={len(collected)}")
            identity = (part["id"], part["size"], part["sha256"])
            if expected is None:
                expected = identity
            if identity != expected or part["id"] != file_id or not 0 <= part["size"] <= MAX_FILE:
                raise ValueError("File metadata changed or exceeds limit")
            chunk = base64.b64decode(part["data_base64"], validate=True)
            if part["offset"] != len(collected) or len(chunk) > CHUNK:
                raise ValueError("Invalid chunk")
            collected.extend(chunk)
            if part["next_offset"] != len(collected) or len(collected) > part["size"]:
                raise ValueError("Invalid chunk offset")
            if part["eof"]:
                break
            if not chunk:
                raise ValueError("Empty nonterminal chunk")
        if len(collected) != expected[1] or hashlib.sha256(collected).hexdigest() != expected[2]:
            raise ValueError("File size or SHA-256 mismatch")
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(collected)
        except BaseException:
            target.unlink()
            raise
        return {
            "file_id": file_id,
            "path": str(target.resolve()),
            "size": len(collected),
            "sha256": expected[2],
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xangi-cli", required=True)
    parser.add_argument(
        "--legacy-chunks", action="store_true", help="Use the older 1 MiB base64 route"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    upload = commands.add_parser("upload")
    upload.add_argument("path")
    upload.add_argument("--upload-key", required=True)
    upload.add_argument("--mime-type")
    download = commands.add_parser("download")
    download.add_argument("file_id")
    download.add_argument("--out", required=True)
    verify = commands.add_parser("verify-job", help="Check a registered input attachment")
    verify.add_argument("file_id")
    verify.add_argument("--job-id", required=True)
    args = parser.parse_args()
    try:
        client = Client(args.xangi_cli)
        if args.command == "upload":
            result = client.upload(args.path, args.upload_key, args.mime_type, args.legacy_chunks)
        elif args.command == "download":
            result = client.download(args.file_id, args.out, args.legacy_chunks)
        else:
            result = client.verify_job(args.file_id, args.job_id)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        parser.exit(
            1, f"File transfer failed ({type(exc).__name__}). Check input and saved state.\n"
        )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
