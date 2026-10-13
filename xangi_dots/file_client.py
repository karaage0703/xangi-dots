"""Local file transfer through the target xangi CLI; credentials stay with xangi."""

import argparse
import base64
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import subprocess

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
            return self.request(
                "/files/import-local",
                "POST",
                {
                    "path": str(source.absolute()),
                    "upload_key": upload_key,
                    "mime_type": mime_type
                    or mimetypes.guess_type(source.name)[0]
                    or "application/octet-stream",
                },
            )
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
    args = parser.parse_args()
    try:
        client = Client(args.xangi_cli)
        result = (
            client.upload(args.path, args.upload_key, args.mime_type, args.legacy_chunks)
            if args.command == "upload"
            else client.download(args.file_id, args.out, args.legacy_chunks)
        )
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        parser.exit(
            1, f"File transfer failed ({type(exc).__name__}). Check input and saved state.\n"
        )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
