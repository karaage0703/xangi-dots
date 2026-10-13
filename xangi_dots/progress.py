"""Worker checkpoints and durable, once-per-checkpoint attention notifications."""

import hmac
import time
from datetime import datetime

from .transport import check_text

STALE_SECONDS = 1800


class ProgressStore:
    def init_progress(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS progress (
              job_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL, state TEXT NOT NULL,
              summary TEXT NOT NULL, next_action TEXT NOT NULL, updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS progress_notifications (
              job_id TEXT PRIMARY KEY, actual_job_id TEXT NOT NULL, reason TEXT NOT NULL,
              status TEXT NOT NULL, trigger_id TEXT, last_error TEXT,
              next_check REAL NOT NULL DEFAULT 0);
        """)
        with self.db:
            self.db.execute(
                "UPDATE progress_notifications SET status='needs_review', "
                "last_error='Process stopped during trigger submission' WHERE status='sending'"
            )

    def progress_view(self, job_id, admin=False):
        row = self.db.execute("SELECT * FROM progress WHERE job_id=?", (job_id,)).fetchone()
        value = dict(row) if row else None
        if value:
            value.pop("job_id")
        if admin:
            notes = self.db.execute(
                "SELECT * FROM progress_notifications WHERE actual_job_id=? "
                "ORDER BY rowid DESC LIMIT 10",
                (job_id,),
            ).fetchall()
            return value, [dict(note) for note in notes]
        return value

    def queue_attention(self, job_id, sequence, reason):
        note_id = f"{job_id}:{sequence}:{reason}"
        self.db.execute(
            "INSERT OR IGNORE INTO progress_notifications"
            "(job_id,actual_job_id,reason,status) VALUES(?,?,?,'pending')",
            (note_id, job_id, reason),
        )

    def report_progress(self, job_id, claim_token, sequence, state, summary, next_action):
        check_text(job_id, "job_id")
        check_text(claim_token, "claim_token")
        check_text(summary, "summary", 4000)
        check_text(next_action, "next_action", 2000)
        if type(sequence) is not int or not 1 <= sequence <= 1000000:
            raise ValueError("sequence must be 1..1000000")
        if state not in {"working", "waiting_for_user", "blocked"}:
            raise ValueError("Invalid progress state")
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
            old = self.db.execute("SELECT * FROM progress WHERE job_id=?", (job_id,)).fetchone()
            if old and sequence <= old["sequence"]:
                if sequence == old["sequence"] and (state, summary, next_action) == (
                    old["state"],
                    old["summary"],
                    old["next_action"],
                ):
                    return self.progress_view(job_id)
                raise ValueError("Progress sequence must advance; retry with identical content")
            self.db.execute(
                "INSERT INTO progress VALUES(?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET "
                "sequence=excluded.sequence,state=excluded.state,summary=excluded.summary,"
                "next_action=excluded.next_action,updated_at=excluded.updated_at",
                (job_id, sequence, state, summary, next_action, time.time()),
            )
            # Keep the same blocker alert pending while its detail is updated.
            # A fresh working checkpoint supersedes old unsent stale/blocker alerts.
            if state == "working" or not old or old["state"] != state:
                self.db.execute(
                    "UPDATE progress_notifications SET status='superseded' "
                    "WHERE actual_job_id=? AND status='pending'",
                    (job_id,),
                )
                if state != "working":
                    self.queue_attention(job_id, sequence, state)
            return self.progress_view(job_id)

    def check_progress(self):
        self.expire()
        with self.lock, self.db:
            self.db.execute(
                "UPDATE progress_notifications SET status='superseded' WHERE status='pending' "
                "AND actual_job_id IN (SELECT id FROM jobs WHERE status NOT IN ('queued','running'))"
            )
            rows = self.db.execute(
                "SELECT j.id,j.status,j.created_at,j.updated_at,p.sequence,p.state,"
                "p.updated_at AS checkpoint FROM jobs j LEFT JOIN progress p ON p.job_id=j.id "
                "WHERE j.status IN ('queued','running')"
            ).fetchall()
            for row in rows:
                if row["state"] in {"waiting_for_user", "blocked"}:
                    continue
                stamp = row["checkpoint"]
                if stamp is None:
                    stamp = datetime.fromisoformat(row["updated_at"]).timestamp()
                if time.time() - stamp >= STALE_SECONDS:
                    reason = "unclaimed" if row["status"] == "queued" else "stale"
                    self.queue_attention(row["id"], row["sequence"] or 0, reason)
