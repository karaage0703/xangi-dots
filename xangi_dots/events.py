"""Dots-specific webhook subscription adapter (MCP Events)."""

import base64
import hashlib
import http.client
import json
import secrets
import time
from datetime import datetime, timezone
from .transport import callback_target, check_text, deliver, encoded, signed_headers

EVENT = "job.created"
QUEUE = "dots"


class Events:
    def __init__(self, store, sender=deliver):
        self.store = store
        self.sender = sender

    def subscribe(self, p):
        if p.get("name") != EVENT or p.get("arguments") != {"queue_id": QUEUE}:
            raise ValueError("Unknown event or queue")
        d = p.get("delivery", {})
        if not isinstance(d, dict) or d.get("mode") != "webhook":
            raise ValueError("Only webhook delivery is supported")
        url = d.get("url")
        callback_target(url)
        secret = check_text(d.get("secret"), "signing secret", 200)
        if not secret.startswith("whsec_"):
            raise ValueError("Invalid signing secret")
        key = base64.b64decode(secret[6:], validate=True)
        if not 24 <= len(key) <= 64:
            raise ValueError("Invalid signing key length")
        ttl = p.get("ttlMs", 86400000)
        if ttl is None:
            ttl = 86400000
        if type(ttl) is not int or ttl <= 0:
            raise ValueError("Invalid ttlMs")
        expiry = time.time() + min(ttl, 86400000) / 1000
        sub_id = "sub_" + hashlib.sha256((url + EVENT + QUEUE).encode()).hexdigest()[:32]
        challenge = secrets.token_urlsafe(24)
        body = encoded({"type": "verification", "challenge": challenge}).encode()
        event_id = "verify_" + secrets.token_hex(12)
        status, reply = self.sender(url, body, signed_headers(secret, event_id, body, sub_id))
        if not 200 <= status < 300 or json.loads(reply).get("challenge") != challenge:
            raise ValueError("Callback verification failed")
        with self.store.lock, self.store.db:
            self.store.db.execute("DELETE FROM subscriptions WHERE expires<=?", (time.time(),))
            count = self.store.db.execute(
                "SELECT count(*) FROM subscriptions WHERE id!=?", (sub_id,)
            ).fetchone()[0]
            if count >= 3:
                raise ValueError("Subscription limit reached")
            self.store.db.execute(
                "INSERT OR REPLACE INTO subscriptions VALUES(?,?,?,?)",
                (sub_id, url, secret, expiry),
            )
            for row in self.store.db.execute(
                "SELECT id FROM jobs WHERE status='queued'"
            ).fetchall():
                self.store.queue_event(row["id"], sub_id)
        return {
            "id": sub_id,
            "refreshBefore": datetime.fromtimestamp(expiry, timezone.utc).isoformat(),
            "cursor": None,
            "truncated": False,
        }

    def unsubscribe(self, p):
        if p.get("name") != EVENT or p.get("arguments") != {"queue_id": QUEUE}:
            raise ValueError("Unknown event or queue")
        url = p.get("delivery", {}).get("url")
        callback_target(url)
        with self.store.lock, self.store.db:
            ids = [
                r[0]
                for r in self.store.db.execute("SELECT id FROM subscriptions WHERE url=?", (url,))
            ]
            for sub_id in ids:
                self.store.db.execute("DELETE FROM subscriptions WHERE id=?", (sub_id,))
                self.store.db.execute(
                    "UPDATE deliveries SET status='cancelled' WHERE sub_id=? AND status='pending'",
                    (sub_id,),
                )
        return {}

    def dispatch(self):
        with self.store.lock:
            rows = self.store.db.execute(
                """SELECT d.*,s.url,s.secret FROM deliveries d
                JOIN subscriptions s ON s.id=d.sub_id JOIN jobs j ON j.id=d.job_id
                WHERE j.status='queued' AND d.status='pending' AND d.next_try<=? AND s.expires>?""",
                (time.time(), time.time()),
            ).fetchall()
        for row in rows:
            body = row["body"].encode()
            status = 0
            try:
                status, _ = self.sender(
                    row["url"],
                    body,
                    signed_headers(row["secret"], row["event_id"], body, row["sub_id"]),
                )
            except (OSError, ValueError, http.client.HTTPException):
                pass
            attempts = row["attempts"] + 1
            state = (
                "delivered"
                if 200 <= status < 300
                else ("failed" if status in (410, 413) or attempts >= 5 else "pending")
            )
            with self.store.lock, self.store.db:
                self.store.db.execute(
                    "UPDATE deliveries SET status=?,attempts=?,next_try=?,http_status=? "
                    "WHERE event_id=? AND status='pending'",
                    (state, attempts, time.time() + min(300, 2**attempts), status, row["event_id"]),
                )
                if status == 410:
                    self.store.db.execute(
                        "UPDATE subscriptions SET expires=0 WHERE id=?", (row["sub_id"],)
                    )
