"""Job-scoped immutable attachments, transferred in bounded, retryable chunks."""

import base64
import hashlib
import hmac
from pathlib import Path
import re
import secrets
import time
import unicodedata

from .transport import check_text, now_iso

MAX_FILE = 1024 * 1024
CHUNK = 48 * 1024
MAX_FILES = 8
MAX_STORAGE = 20 * 1024 * 1024 * 1024
MAX_UPLOADS = 128
FILE_COLUMNS = "id,job_id,request_id,direction,name,mime_type,size,sha256,status,CASE WHEN disk_name IS NOT NULL THEN size ELSE length(data) END AS received"


class FileStore:
    def init_files(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS files (
              id TEXT PRIMARY KEY, upload_key TEXT NOT NULL, job_id TEXT,
              direction TEXT NOT NULL, name TEXT NOT NULL, mime_type TEXT NOT NULL,
              size INTEGER NOT NULL, sha256 TEXT NOT NULL, status TEXT NOT NULL,
              data BLOB NOT NULL, created_at TEXT NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS files_input_key
              ON files(upload_key) WHERE direction='input';
            CREATE UNIQUE INDEX IF NOT EXISTS files_output_key
              ON files(job_id, upload_key) WHERE direction='output';
        """)

    def init_disk_files(self, database):
        self.file_dir = Path(str(database) + ".files")
        self.file_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(files)")}
        if "disk_name" not in columns:
            self.db.execute("ALTER TABLE files ADD COLUMN disk_name TEXT")
            self.db.commit()

        if "request_id" not in columns:
            self.db.execute("ALTER TABLE files ADD COLUMN request_id TEXT")
            self.db.commit()
        self.db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS files_unbound_key ON files(direction,upload_key) WHERE job_id IS NULL"
        )
        self.db.commit()

    def open_file(self, file_id):
        row = self.db.execute("SELECT disk_name,data FROM files WHERE id=?", (file_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown file")
        if row[0]:
            return (self.file_dir / row[0]).open("rb")
        import io

        return io.BytesIO(row[1])

    def file_claim(self, job_id, claim_token, writing=False):
        check_text(job_id, "job_id")
        check_text(claim_token, "claim_token")
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if (
            not row
            or not row["claim_token"]
            or not hmac.compare_digest(row["claim_token"], claim_token)
            or row["status"] not in ({"running"} if writing else {"running", "completed", "failed"})
        ):
            raise ValueError("Invalid claim or job state")

    def file_row(self, file_id):
        check_text(file_id, "file_id")
        row = self.db.execute(f"SELECT {FILE_COLUMNS} FROM files WHERE id=?", (file_id,)).fetchone()
        if not row:
            raise ValueError("Unknown file")
        return dict(row)

    def file_list(self, job_id):
        return [
            dict(row)
            for row in self.db.execute(
                f"SELECT {FILE_COLUMNS} FROM files WHERE job_id=? ORDER BY rowid", (job_id,)
            )
        ]

    def create_file(
        self,
        upload_key,
        name,
        mime_type,
        size,
        sha256,
        *,
        job_id=None,
        claim_token=None,
        _size_limit=MAX_FILE,
        _direction=None,
        _request_id=None,
    ):
        check_text(upload_key, "upload_key")
        check_text(name, "name", 180)
        if (
            name in {".", ".."}
            or any(c in name for c in "/\\:")
            or any(unicodedata.category(c).startswith("C") for c in name)
            or name != name.strip()
        ):
            raise ValueError("name must be a plain filename, not a path")
        if not isinstance(mime_type, str) or not re.fullmatch(
            r"[a-zA-Z0-9!#$&^_.+-]{1,80}/[a-zA-Z0-9!#$&^_.+-]{1,80}", mime_type
        ):
            raise ValueError("Invalid MIME type")
        if type(size) is not int or not 0 <= size <= _size_limit:
            raise ValueError("File size exceeds the transfer limit")
        if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("Expected lowercase SHA-256")
        self.expire()
        direction = _direction or ("output" if job_id is not None else "input")
        with self.lock, self.db:
            if job_id is not None:
                self.file_claim(job_id, claim_token, writing=True)
            old = self.db.execute(
                "SELECT * FROM files WHERE direction=? AND upload_key=? "
                "AND (direction!='output' OR job_id=?)",
                (direction, upload_key, job_id),
            ).fetchone()
            if old:
                if [old[k] for k in ("name", "mime_type", "size", "sha256")] != [
                    name,
                    mime_type,
                    size,
                    sha256,
                ]:
                    raise ValueError("upload_key conflicts with an existing file")
                return self.file_row(old["id"])
            if job_id is not None and len(self.file_list(job_id)) >= MAX_FILES:
                raise ValueError("Job attachment limit reached (8, including uploads)")
            if (
                _request_id is not None
                and self.db.execute(
                    "SELECT count(*) FROM files WHERE request_id=?", (_request_id,)
                ).fetchone()[0]
                >= MAX_FILES
            ):
                raise ValueError("Request attachment limit reached (8)")
            count, reserved = self.db.execute(
                "SELECT count(*),COALESCE(sum(size),0) FROM files"
            ).fetchone()
            if reserved + size > self.max_storage_bytes or count >= 4096:
                raise ValueError("Attachment storage quota reached; remove old files")
            if (
                self.db.execute(
                    "SELECT count(*) FROM files WHERE (job_id IS NULL AND request_id IS NULL) OR status='uploading'"
                ).fetchone()[0]
                >= MAX_UPLOADS
            ):
                raise ValueError("Pending upload limit reached (128)")
            file_id = "file_" + secrets.token_hex(12)
            self.db.execute(
                "INSERT INTO files (id,upload_key,job_id,direction,name,mime_type,size,sha256,status,data,created_at,request_id) VALUES(?,?,?,?,?,?,?,?,'uploading',?,?,?)",
                (
                    file_id,
                    upload_key,
                    job_id,
                    direction,
                    name,
                    mime_type,
                    size,
                    sha256,
                    b"",
                    now_iso(),
                    _request_id,
                ),
            )
            return self.file_row(file_id)

    def authorize_file(self, file_id, job_id, claim_token, writing=False):
        row = self.file_row(file_id)
        if job_id is not None:
            self.file_claim(job_id, claim_token, writing=writing)
            if row["job_id"] != job_id or (writing and row["direction"] != "output"):
                raise ValueError("File does not belong to this job or direction")
        elif writing and (row["direction"] != "input" or row["job_id"] is not None):
            raise ValueError("Only unbound input uploads can be changed by the parent")
        return row

    def write_file(self, file_id, offset, data, encoding, *, job_id=None, claim_token=None):
        if type(offset) is not int or offset < 0:
            raise ValueError("Invalid offset")
        if not isinstance(data, str) or len(data) > CHUNK * 4 // 3:
            raise ValueError("Chunk is too large")
        if encoding == "base64":
            try:
                raw = base64.b64decode(data, validate=True)
            except (ValueError, UnicodeError):
                raise ValueError("Invalid base64") from None
        elif encoding == "utf-8":
            raw = data.encode("utf-8")
        else:
            raise ValueError("encoding must be base64 or utf-8")
        if not 1 <= len(raw) <= CHUNK:
            raise ValueError("Chunk must be 1..49152 bytes")
        self.expire()
        with self.lock, self.db:
            row = self.authorize_file(file_id, job_id, claim_token, writing=True)
            if self.db.execute("SELECT disk_name FROM files WHERE id=?", (file_id,)).fetchone()[0]:
                raise ValueError("Native files are immutable")
            existing = self.db.execute("SELECT data FROM files WHERE id=?", (file_id,)).fetchone()[
                0
            ]
            if offset < len(existing):
                if existing[offset : offset + len(raw)] == raw:
                    return row
                raise ValueError("Chunk conflicts with previously stored bytes")
            if (
                row["status"] != "uploading"
                or offset != len(existing)
                or offset + len(raw) > row["size"]
            ):
                raise ValueError("Invalid offset, file size, or sealed file")
            self.db.execute("UPDATE files SET data=? WHERE id=?", (existing + raw, file_id))
            return self.file_row(file_id)

    def finish_file(self, file_id, *, job_id=None, claim_token=None):
        self.expire()
        with self.lock, self.db:
            row = self.authorize_file(file_id, job_id, claim_token, writing=True)
            if row["status"] == "ready":
                return row
            data = self.db.execute("SELECT data FROM files WHERE id=?", (file_id,)).fetchone()[0]
            if len(data) != row["size"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise ValueError("Incomplete file or SHA-256 mismatch")
            self.db.execute("UPDATE files SET status='ready' WHERE id=?", (file_id,))
            return self.file_row(file_id)

    def read_file(self, file_id, offset, *, job_id=None, claim_token=None):
        if type(offset) is not int or offset < 0:
            raise ValueError("Invalid offset")
        self.expire()
        with self.lock:
            row = self.authorize_file(file_id, job_id, claim_token)
            if row["status"] != "ready" or offset > row["size"]:
                raise ValueError("File not ready or invalid offset")
            with self.open_file(file_id) as stream:
                stream.seek(offset)
                raw = stream.read(CHUNK)
            return {
                **row,
                "offset": offset,
                "next_offset": offset + len(raw),
                "eof": offset + len(raw) == row["size"],
                "data_base64": base64.b64encode(raw).decode(),
            }

    def delete_file(self, file_id):
        self.expire()
        with self.lock, self.db:
            row = self.file_row(file_id)
            if row["job_id"] and self.get(row["job_id"])["status"] in {"queued", "running"}:
                raise ValueError("Cannot delete files of an active job")
            if row["request_id"]:
                self.db.execute(
                    "UPDATE parent_requests SET status='expired' WHERE status IN ('queued','running') AND deadline<=?",
                    (time.time(),),
                )
                state = self.db.execute(
                    "SELECT status FROM parent_requests WHERE id=?", (row["request_id"],)
                ).fetchone()
                if state and state[0] in {"queued", "running"}:
                    raise ValueError("Cannot delete files of an active request")
            disk = self.db.execute("SELECT disk_name FROM files WHERE id=?", (file_id,)).fetchone()[
                0
            ]
            self.db.execute("DELETE FROM files WHERE id=?", (file_id,))
            self.db.commit()
            if disk:
                (self.file_dir / disk).unlink(missing_ok=True)
            return {"file_id": file_id, "deleted": True}

    def save_text_file(self, job_id, claim_token, upload_key, name, mime_type, text):
        check_text(text, "text", 50000)
        raw = text.encode("utf-8")
        row = self.create_file(
            upload_key,
            name,
            mime_type,
            len(raw),
            hashlib.sha256(raw).hexdigest(),
            job_id=job_id,
            claim_token=claim_token,
        )
        for offset in range(0, len(raw), CHUNK):
            self.write_file(
                row["id"],
                offset,
                base64.b64encode(raw[offset : offset + CHUNK]).decode(),
                "base64",
                job_id=job_id,
                claim_token=claim_token,
            )
        return self.finish_file(row["id"], job_id=job_id, claim_token=claim_token)

    def bind_files(self, job_id, attachments):
        for file_id in attachments:
            row = self.file_row(file_id)
            if row["direction"] != "input" or row["status"] != "ready" or row["job_id"] is not None:
                raise ValueError("Input file is not ready or already bound to a job")
            self.db.execute("UPDATE files SET job_id=? WHERE id=?", (job_id, file_id))
