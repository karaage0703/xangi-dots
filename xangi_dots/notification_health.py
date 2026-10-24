"""Read-only CLI preflight shared by all notification paths."""

import os
import subprocess
import threading
import time


class NotificationHealth:
    def __init__(self, cli, runner=subprocess.run, env=None, clock=time.monotonic):
        self.cli, self.runner = cli, runner
        self.env = os.environ if env is None else env
        self.clock = clock
        self.lock = threading.Lock()
        self.next_check = 0
        self.status = {"configured": bool(cli), "ready": not cli, "last_error": None}

    def check(self):
        with self.lock:
            if not self.cli or self.clock() < self.next_check:
                return self.status["ready"]
            self.next_check = self.clock() + 30
            error = None
            if not self.env.get("XANGI_TOOL_SERVER"):
                error = "missing_tool_server"
            else:
                try:
                    result = self.runner(
                        [self.cli, "tool", "help", "trigger"],
                        capture_output=True,
                        text=True,
                        timeout=20,
                        env=self.env,
                    )
                    if result.returncode:
                        error = f"cli_exit_{result.returncode}"
                except subprocess.TimeoutExpired:
                    error = "cli_timeout"
                except OSError:
                    error = "cli_unavailable"
                except subprocess.SubprocessError:
                    error = "cli_error"
            self.status = {"configured": True, "ready": error is None, "last_error": error}
            return error is None

    def run(self, action):
        # Leave pending outbox entries untouched until a read-only probe succeeds.
        if self.check():
            action()

    def health(self, store):
        result = store.health()
        # A slow probe must not block the inbox health endpoint.
        status = self.status
        result["notification_transport"] = dict(status)
        result["ready"] = result["ready"] and status["ready"]
        return result
