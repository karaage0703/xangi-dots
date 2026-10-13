"""Dots webhook transport; carried forward from the verified MCP Events experiment."""

import base64
import hmac
import http.client
import ipaddress
import json
import socket
import ssl
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def check_text(value, name, maximum=200):
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise ValueError(f"Invalid {name}")
    return value


def signed_headers(secret, event_id, body, subscription_id):
    key = base64.b64decode(secret.removeprefix("whsec_"), validate=True)
    stamp = str(int(time.time()))
    sig = base64.b64encode(
        hmac.digest(key, f"{event_id}.{stamp}.".encode() + body, "sha256")
    ).decode()
    return {
        "Content-Type": "application/json",
        "webhook-id": event_id,
        "webhook-timestamp": stamp,
        "webhook-signature": "v1," + sig,
        "X-MCP-Subscription-Id": subscription_id,
    }


def callback_target(url):
    u = urlsplit(check_text(url, "callback URL", 4096))
    host = u.hostname or ""
    if (
        u.scheme != "https"
        or u.port not in (None, 443)
        or u.username
        or u.password
        or u.fragment
        or not any(host == d or host.endswith("." + d) for d in ("openai.com", "chatgpt.com"))
    ):
        raise ValueError("Callback must be HTTPS on an OpenAI/ChatGPT domain, port 443")
    return u


def deliver(url, body, headers):
    """Resolve once, reject nonpublic addresses, pin connection, retain TLS hostname."""
    u = callback_target(url)
    addresses = socket.getaddrinfo(u.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("Non-public callback address")
    address = addresses[0][4][0]

    class PinnedHTTPS(http.client.HTTPSConnection):
        def connect(self):
            raw = socket.create_connection((address, 443), timeout=self.timeout)
            try:
                self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
            except Exception:
                raw.close()
                raise

    conn = PinnedHTTPS(u.hostname, timeout=10, context=ssl.create_default_context())
    try:
        conn.request(
            "POST", u.path + ("?" + u.query if u.query else ""), body=body, headers=headers
        )
        response = conn.getresponse()
        data = response.read(65537)
        if len(data) > 65536:
            raise ValueError("Callback response too large")
        return response.status, data
    finally:
        conn.close()
