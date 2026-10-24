"""Persistent job state. No transport or host execution lives here."""

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time

from .transport import check_text, encoded, now_iso
from .files import FileStore, MAX_FILES
from .progress import ProgressStore

TERMINAL = {"completed", "failed", "cancelled", "expired"}


class Store(FileStore, ProgressStore):
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL,
              fingerprint TEXT NOT NULL, prompt TEXT NOT NULL, status TEXT NOT NULL,
              claim_key TEXT, claim_token TEXT, result TEXT, deadline REAL NOT NULL,
              origin TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS notifications (
              job_id TEXT PRIMARY KEY, status TEXT NOT NULL, trigger_id TEXT,
              last_error TEXT, next_check REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS subscriptions (
              id TEXT PRIMARY KEY, url TEXT, secret TEXT, expires REAL);
            CREATE TABLE IF NOT EXISTS deliveries (
              event_id TEXT PRIMARY KEY, job_id TEXT, sub_id TEXT, body TEXT,
              status TEXT, attempts INTEGER, next_try REAL, http_status INTEGER);
        """)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS followups (
              id TEXT PRIMARY KEY, job_id TEXT NOT NULL, request_key TEXT NOT NULL,
              message TEXT NOT NULL, created_at TEXT NOT NULL, acknowledged_at TEXT,
              UNIQUE(job_id,request_key));
        """)
        self.init_progress()
        self.init_files()
        self.init_disk_files(path)
        # An interrupted send might already have reached xangi. Never blindly repeat it.
        with self.db:
            self.db.execute(
                "UPDATE notifications SET status='needs_review', "
                "last_error='Process stopped during trigger submission' "
                "WHERE status='sending'"
            )

    @staticmethod
    def public(row, admin=False):
        value = dict(row)
        for key in ("claim_key", "claim_token", "fingerprint"):
            value.pop(key, None)
        if admin:
            value["origin"] = json.loads(value["origin"])
        else:
            value.pop("origin", None)
            value.pop("request_key", None)
        return value

    def enqueue(self, request_key, prompt, origin, timeout_seconds=3600, attachments=None):
        check_text(request_key, "request_key")
        check_text(prompt, "prompt", 20000)
        if (
            not isinstance(origin, dict)
            or set(origin) != {"platform", "channel"}
            or origin["platform"] not in {"discord", "slack", "telegram", "web", "line"}
        ):
            raise ValueError("origin requires platform and channel")
        check_text(origin["channel"], "channel", 200)
        if type(timeout_seconds) is not int or not 60 <= timeout_seconds <= 604800:
            raise ValueError("timeout_seconds must be 60..604800")
        attachments = [] if attachments is None else attachments
        if (
            not isinstance(attachments, list)
            or len(attachments) > MAX_FILES
            or any(not isinstance(item, str) for item in attachments)
            or len(set(attachments)) != len(attachments)
        ):
            raise ValueError("attachments must contain up to 8 distinct file IDs")
        identity = [prompt, origin["platform"], origin["channel"], timeout_seconds]
        if attachments:
            identity.append(attachments)
        fingerprint = hashlib.sha256(encoded(identity).encode()).hexdigest()
        with self.lock, self.db:
            old = self.db.execute(
                "SELECT * FROM jobs WHERE request_key=?", (request_key,)
            ).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise ValueError("request_key already belongs to a different request")
                return self.get(old["id"], True)
            if (
                self.db.execute(
                    "SELECT count(*) FROM jobs WHERE status IN ('queued','running')"
                ).fetchone()[0]
                >= 100
            ):
                raise ValueError("Active job limit reached (100)")
            job_id = "job_" + secrets.token_hex(12)
            stamp = now_iso()
            self.db.execute(
                "INSERT INTO jobs VALUES(?,?,?,?,'queued',NULL,NULL,NULL,?,?,?,?)",
                (
                    job_id,
                    request_key,
                    fingerprint,
                    prompt,
                    time.time() + timeout_seconds,
                    encoded(origin),
                    stamp,
                    stamp,
                ),
            )
            self.bind_files(job_id, attachments)
            for sub in self.db.execute(
                "SELECT id FROM subscriptions WHERE expires>?", (time.time(),)
            ):
                self.queue_event(job_id, sub["id"])
            return self.get(job_id, True)

    def queue_event(self, job_id, sub_id):
        event_id = "evt_" + hashlib.sha256((job_id + sub_id).encode()).hexdigest()[:32]
        body = encoded(
            {
                "eventId": event_id,
                "name": "job.created",
                "timestamp": now_iso(),
                "data": {"job_id": job_id, "queue_id": "dots"},
                "cursor": None,
            }
        )
        self.db.execute(
            "INSERT INTO deliveries VALUES(?,?,?,?,'pending',0,0,NULL) "
            "ON CONFLICT(event_id) DO UPDATE SET status='pending',attempts=0,next_try=0 "
            "WHERE deliveries.status='cancelled'",
            (event_id, job_id, sub_id, body),
        )

    def followup(self, job_id, request_key, message):
        check_text(job_id, "job_id")
        check_text(request_key, "request_key")
        check_text(message, "message", 4000)
        self.expire()
        with self.lock, self.db:
            old = self.db.execute(
                "SELECT * FROM followups WHERE job_id=? AND request_key=?",
                (job_id, request_key),
            ).fetchone()
            if old:
                if old["message"] != message:
                    raise ValueError("Follow-up key reused with different content")
                return dict(old)
            job = self.db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job or job["status"] not in {"queued", "running"}:
                raise ValueError("Active job required")
            if (
                self.db.execute(
                    "SELECT count(*) FROM followups WHERE job_id=?", (job_id,)
                ).fetchone()[0]
                >= 50
            ):
                raise ValueError("Follow-up limit reached")
            message_id = "msg_" + secrets.token_hex(12)
            self.db.execute(
                "INSERT INTO followups VALUES(?,?,?,?,?,NULL)",
                (message_id, job_id, request_key, message, now_iso()),
            )
            for sub in self.db.execute(
                "SELECT id FROM subscriptions WHERE expires>?", (time.time(),)
            ):
                self.queue_followup_event(job_id, message_id, sub["id"])
            return dict(
                self.db.execute("SELECT * FROM followups WHERE id=?", (message_id,)).fetchone()
            )

    def queue_followup_event(self, job_id, message_id, sub_id):
        # Reuse the existing inbox subscription; get_job exposes the new message.
        event_id = "evt_" + hashlib.sha256((message_id + sub_id).encode()).hexdigest()[:32]
        body = encoded(
            {
                "eventId": event_id,
                "name": "job.created",
                "timestamp": now_iso(),
                "data": {"job_id": job_id, "queue_id": "dots"},
                "cursor": None,
            }
        )
        self.db.execute(
            "INSERT INTO deliveries VALUES(?,?,?,?,'pending',0,0,NULL) "
            "ON CONFLICT(event_id) DO UPDATE SET status='pending',attempts=0,next_try=0 "
            "WHERE deliveries.status='cancelled'",
            (event_id, job_id, sub_id, body),
        )

    def acknowledge_followup(self, job_id, claim_token, message_id):
        check_text(claim_token, "claim_token")
        self.expire()
        with self.lock, self.db:
            job = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if (
                not job
                or job["status"] != "running"
                or not job["claim_token"]
                or not hmac.compare_digest(job["claim_token"], claim_token)
            ):
                raise ValueError("Running job and valid claim required")
            row = self.db.execute(
                "SELECT * FROM followups WHERE id=? AND job_id=?", (message_id, job_id)
            ).fetchone()
            if not row:
                raise ValueError("Unknown follow-up")
            self.db.execute(
                "UPDATE followups SET acknowledged_at=COALESCE(acknowledged_at,?) WHERE id=?",
                (now_iso(), message_id),
            )
            return dict(
                self.db.execute("SELECT * FROM followups WHERE id=?", (message_id,)).fetchone()
            )

    def get(self, job_id, admin=False):
        check_text(job_id, "job_id")
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise ValueError("Unknown job")
            result = self.public(row, admin)
            result["progress"] = self.progress_view(job_id)
            result["files"] = self.file_list(job_id)
            result["followups"] = [
                dict(r)
                for r in self.db.execute(
                    "SELECT id,job_id,message,created_at,acknowledged_at FROM followups WHERE job_id=? ORDER BY rowid",
                    (job_id,),
                )
            ]
            if admin:
                note = self.db.execute(
                    "SELECT * FROM notifications WHERE job_id=?", (job_id,)
                ).fetchone()
                result["notification"] = dict(note) if note else None
                result["attention_notifications"] = self.progress_view(job_id, True)[1]
            return result

    def jobs(self, admin=False):
        with self.lock:
            # Worker discovery is bounded; completed jobs are retrieved by ID.
            rows = self.db.execute(
                "SELECT * FROM jobs "
                + (
                    "ORDER BY created_at DESC LIMIT 100"
                    if admin
                    else "WHERE status IN ('queued','running') ORDER BY created_at LIMIT 100"
                )
            ).fetchall()
            return [
                dict(self.public(row, admin), progress=self.progress_view(row["id"]))
                for row in rows
            ]

    def finish(self, job_id, status, result=None):
        self.db.execute(
            "UPDATE jobs SET status=?,result=?,updated_at=? WHERE id=?",
            (status, result, now_iso(), job_id),
        )
        self.db.execute(
            "INSERT OR IGNORE INTO notifications(job_id,status) VALUES(?,'pending')", (job_id,)
        )
        self.db.execute(
            "UPDATE deliveries SET status='cancelled' WHERE job_id=? AND status='pending'",
            (job_id,),
        )

    def expire(self):
        with self.lock, self.db:
            for row in self.db.execute(
                "SELECT id FROM jobs WHERE status IN ('queued','running') AND deadline<=?",
                (time.time(),),
            ).fetchall():
                self.finish(row["id"], "expired")

    def claim(self, job_id, claim_key):
        check_text(job_id, "job_id")
        check_text(claim_key, "claim_key")
        self.expire()
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["status"] in TERMINAL:
                raise ValueError("Job absent or terminal; do not execute")
            if row["status"] == "running":
                if row["claim_key"] != claim_key:
                    raise ValueError("Job already claimed; do not execute again")
                return {"job_id": job_id, "claim_token": row["claim_token"], "status": "running"}
            token = secrets.token_urlsafe(32)
            self.db.execute(
                "UPDATE jobs SET status='running',claim_key=?,claim_token=?,updated_at=? WHERE id=?",
                (claim_key, token, now_iso(), job_id),
            )
            return {"job_id": job_id, "claim_token": token, "status": "running"}

    def complete(self, job_id, claim_token, result, outcome="completed"):
        check_text(job_id, "job_id")
        check_text(claim_token, "claim_token")
        check_text(result, "result", 50000)
        if outcome not in {"completed", "failed"}:
            raise ValueError("outcome must be completed or failed")
        self.expire()
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if (
                not row
                or not row["claim_token"]
                or not hmac.compare_digest(row["claim_token"], claim_token)
            ):
                raise ValueError("Invalid claim")
            if row["status"] in {"completed", "failed"}:
                if row["status"] != outcome or row["result"] != result:
                    raise ValueError("Terminal result cannot be overwritten")
            elif row["status"] == "running":
                if outcome == "completed" and any(
                    f["status"] != "ready" for f in self.file_list(job_id)
                ):
                    raise ValueError("Finish all file uploads before completing the job")
                self.finish(job_id, outcome, result)
            else:
                raise ValueError("Job no longer running; late results rejected")
            return {"job_id": job_id, "status": outcome}

    def cancel(self, job_id):
        with self.lock, self.db:
            row = self.get(job_id)
            if row["status"] not in TERMINAL:
                self.finish(job_id, "cancelled")
            return self.get(job_id, True)

    def health(self):
        with self.lock:
            return {
                "ready": True,
                "jobs": dict(self.db.execute("SELECT status,count(*) FROM jobs GROUP BY status")),
                "subscriptions": self.db.execute(
                    "SELECT count(*) FROM subscriptions WHERE expires>?", (time.time(),)
                ).fetchone()[0],
                "deliveries": dict(
                    self.db.execute("SELECT status,count(*) FROM deliveries GROUP BY status")
                ),
                "attention_notifications": dict(
                    self.db.execute(
                        "SELECT status,count(*) FROM progress_notifications GROUP BY status"
                    )
                ),
                "notifications": dict(
                    self.db.execute("SELECT status,count(*) FROM notifications GROUP BY status")
                ),
            }
