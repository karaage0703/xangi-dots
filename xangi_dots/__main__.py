"""Managed lifecycle. Configuration is private and scoped by host instance."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import signal
import sys
import threading

from .events import Events
from .notify import Notifier
from .requests import Requests
from .native_files import NativeFiles
from .server import make_server
from .store import Store
from .transport import encoded


def serve(workspace):
    token = os.environ.get("XANGI_EXTENSION_AUTH_TOKEN")
    if not token:
        raise ValueError("Must be launched by xangi with XANGI_EXTENSION_AUTH_TOKEN")
    instance = os.environ.get("XANGI_EXTENSION_INSTANCE_ID", "default")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", instance) or instance in {".", ".."}:
        raise ValueError("Invalid instance ID")
    workspace = workspace.expanduser().resolve()
    os.umask(0o077)
    state = workspace / ".xangi-dots" / instance
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    lockfile = (state / "runtime.lock").open("a")
    fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    config_path = state / "config.json"
    if not config_path.exists():
        with config_path.open("x") as stream:
            json.dump(
                {
                    "worker_token": secrets.token_urlsafe(32),
                    "mcp_port": 0,
                    "xangi_cli": None,
                    "public_base_url": None,
                    "request_destination": None,
                },
                stream,
            )
    config = json.loads(config_path.read_text())
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", config.get("worker_token", "")):
        raise ValueError("worker_token must be 32..128 URL-safe characters")
    cli = config.get("xangi_cli")
    if cli is not None and (
        not isinstance(cli, str) or not Path(cli).is_absolute() or not os.access(cli, os.X_OK)
    ):
        raise ValueError("xangi_cli must be an executable absolute path")
    port = config.get("mcp_port", 0)
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("Invalid mcp_port")
    store = Store(state / "inbox.sqlite")
    events = Events(store)
    notifier = Notifier(store, cli)
    progress_notifier = Notifier(store, cli, progress=True)
    native = NativeFiles(store, config["worker_token"], workspace, config.get("public_base_url"))
    requests = Requests(store, cli, config.get("request_destination"), native=native)
    worker = make_server(
        store,
        events,
        config["worker_token"],
        worker=True,
        port=port,
        native=native,
        requests=requests,
    )
    connection = {
        "mcp_port": worker.server_port,
        "mcp_path": "/mcp/" + config["worker_token"],
        "notifications_enabled": bool(cli),
        "parent_requests_enabled": requests.enabled,
        "state_directory": str(state),
        "native_downloads_enabled": bool(native.public_base_url),
    }
    admin = make_server(
        store, events, token, connection=connection, native=native, requests=requests
    )
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    def parent_watch():
        sys.stdin.buffer.read()
        stop.set()

    def maintenance(action):
        while not stop.is_set():
            try:
                action()
            except Exception as exc:
                # No secret-bearing exception messages in host logs.
                print(f"maintenance failure: {type(exc).__name__}", file=sys.stderr, flush=True)
            stop.wait(1)

    threads = [threading.Thread(target=parent_watch, daemon=True)]
    threads += [threading.Thread(target=s.serve_forever, daemon=True) for s in (admin, worker)]
    # Slow webhook requests cannot delay job expiry or trigger receipt checks.
    threads += [
        threading.Thread(target=maintenance, args=(action,), daemon=True)
        for action in (
            events.dispatch,
            notifier.tick,
            progress_notifier.tick,
            store.expire,
            requests.tick,
        )
    ]
    for thread in threads:
        thread.start()
    print(
        encoded(
            {
                "schemaVersion": 2,
                "event": "ready",
                "id": "xangi-dots",
                "baseUrl": f"http://127.0.0.1:{admin.server_port}",
                "workspace": str(workspace),
            }
        ),
        flush=True,
    )
    try:
        stop.wait()
    finally:
        for server in (admin, worker):
            server.shutdown()
            server.server_close()
        # Daemon workers terminate with the process; SQLite WAL preserves committed state.
        # Do not close the shared DB while a bounded network call may still return.
        lockfile.close()


def main():
    parser = argparse.ArgumentParser(prog="xangi-dots")
    parser.add_argument("command", choices=["serve"])
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    serve(args.workspace)


if __name__ == "__main__":
    main()
