import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from xangi_dots.notification_health import NotificationHealth
from xangi_dots.notify import Notifier
from xangi_dots.store import Store


class NotificationHealthTests(unittest.TestCase):
    def test_missing_target_preserves_pending_and_recovers(self):
        env = {}
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, "trigger help"))
        clock = Mock(return_value=1)
        health = NotificationHealth("/test/xangi", runner, env, clock)
        action = Mock()
        health.run(action)
        runner.assert_not_called()
        action.assert_not_called()
        self.assertEqual(health.status["last_error"], "missing_tool_server")
        env["XANGI_TOOL_SERVER"] = "http://127.0.0.1:1234"
        clock.return_value = 32
        health.run(action)
        action.assert_called_once()
        self.assertTrue(health.status["ready"])
        self.assertEqual(runner.call_args.args[0], ["/test/xangi", "tool", "help", "trigger"])

    def test_probe_failure_is_safe_and_health_is_degraded(self):
        for effect, expected in [
            (subprocess.CompletedProcess([], 7, "SECRET", "PRIVATE"), "cli_exit_7"),
            (subprocess.TimeoutExpired("private command", 20), "cli_timeout"),
            (OSError("secret path"), "cli_unavailable"),
        ]:
            with self.subTest(expected=expected):
                runner = Mock()
                if isinstance(effect, Exception):
                    runner.side_effect = effect
                else:
                    runner.return_value = effect
                health = NotificationHealth("/test/xangi", runner, {"XANGI_TOOL_SERVER": "test"})
                self.assertFalse(health.check())
                self.assertEqual(health.status["last_error"], expected)
                store = Mock()
                store.health.return_value = {"ready": True}
                self.assertFalse(health.health(store)["ready"])
                self.assertNotIn("SECRET", str(health.status))

    def test_disabled_notifications_do_not_break_inbox_health(self):
        health = NotificationHealth(None)
        self.assertTrue(health.check())
        self.assertFalse(health.status["configured"])

    def test_cli_exit_code_is_saved_without_cli_output(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "inbox.sqlite")
            try:
                job = store.enqueue(
                    "notify-test", "task", {"platform": "discord", "channel": "test"}, 60
                )
                store.finish(job["id"], "failed")
                runner = Mock(return_value=subprocess.CompletedProcess([], 3, "SECRET", "PRIVATE"))
                notifier = Notifier(store, "/test/xangi", runner)
                notifier.tick()
                notification = store.get(job["id"], True)["notification"]
                self.assertEqual(notification["last_error"], "cli_exit_3")
                self.assertEqual(notification["status"], "needs_review")
                notifier.tick()
                self.assertEqual(runner.call_count, 1)
            finally:
                store.db.close()


if __name__ == "__main__":
    unittest.main()
