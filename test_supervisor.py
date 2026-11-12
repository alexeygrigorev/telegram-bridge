import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import supervisor  # noqa: E402


class PolicyTests(unittest.TestCase):
    def test_short_crashes_back_off_to_a_cap(self):
        self.assertEqual(supervisor.next_restart_delay(5, 3), 10)
        self.assertEqual(supervisor.next_restart_delay(150, 3), 300)
        self.assertEqual(supervisor.next_restart_delay(300, 3), 300)

    def test_fresh_or_dead_child_is_not_stable(self):
        child = supervisor.Child("x", ["true"])
        self.assertFalse(supervisor.child_is_stable(child, 100.0))
        child.process = mock.Mock(**{"poll.return_value": None})
        child.started_at = 90.0
        self.assertFalse(supervisor.child_is_stable(child, 100.0))
        self.assertTrue(supervisor.child_is_stable(child, 130.0))
        child.process.poll.return_value = 1
        self.assertFalse(supervisor.child_is_stable(child, 130.0))

    def test_long_running_child_resets_backoff(self):
        self.assertEqual(supervisor.next_restart_delay(120, 60), 5)

    def test_heartbeat_error_never_logs_url_or_response_text(self):
        error = urllib.error.URLError("https://example.invalid/private-check-id")
        message = supervisor.heartbeat_failure_message(error)
        self.assertEqual(message, "external heartbeat failed (URLError)")
        self.assertNotIn("private-check-id", message)


class HeartbeatRequestTests(unittest.TestCase):
    def test_post_json_with_optional_bearer(self):
        children = supervisor.make_children(Path("/tmp"), [])
        with mock.patch.dict(os.environ, {"OPERATOR_COMMS_HEARTBEAT_TOKEN": "fake-hb-token"}):
            req = supervisor.heartbeat_request("https://hc.example.invalid/x", children)
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Authorization"), "Bearer fake-hb-token")
        self.assertEqual(json.loads(req.data)["source"], "telegram-bridge")
        with mock.patch.dict(os.environ, {"OPERATOR_COMMS_HEARTBEAT_TOKEN": ""}):
            req = supervisor.heartbeat_request("https://hc.example.invalid/x", children)
        self.assertIsNone(req.get_header("Authorization"))


class ChildrenTests(unittest.TestCase):
    def test_relay_is_always_first_child(self):
        children = supervisor.make_children(Path("/tmp"), [])
        self.assertEqual([c.name for c in children], ["telegram-relay"])
        self.assertEqual(children[0].command[-1], str(supervisor.HERE / "relay.py"))

    def test_extra_children_are_parsed_with_shell_quoting(self):
        children = supervisor.make_children(Path("/tmp"), ["sync=python3 /x/sync.py --interval 300 'a b'"])
        self.assertEqual(children[1].name, "sync")
        self.assertEqual(children[1].command, ["python3", "/x/sync.py", "--interval", "300", "a b"])

    def test_bad_child_specs_are_rejected(self):
        for spec in ("nocommand", "=cmd", "x="):
            with self.assertRaises(ValueError):
                supervisor.parse_child(spec)
        with self.assertRaises(ValueError):
            supervisor.make_children(Path("/tmp"), ["telegram-relay=echo"])


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for k in ("DATAOPS_TELEGRAM_BOT_TOKEN", "OPERATOR_COMMS_HEARTBEAT_URL", "DATAOPS_TELEGRAM_ALERT_CHAT"):
            os.environ.pop(k, None)

    def test_missing_everything_is_reported_without_values(self):
        problems = supervisor.check_config(self.base, require_heartbeat=True)
        text = "\n".join(problems)
        self.assertIn("DATAOPS_TELEGRAM_BOT_TOKEN", text)
        self.assertIn("OPERATOR_COMMS_HEARTBEAT_URL", text)
        self.assertIn("alert chat", text)

    def test_valid_config(self):
        os.environ["DATAOPS_TELEGRAM_BOT_TOKEN"] = "fake-token-value"
        os.environ["OPERATOR_COMMS_HEARTBEAT_URL"] = "https://hc.example.invalid/ping/abc"
        (self.base / "alert_chat.txt").write_text("100000001  # owner\n")
        self.assertEqual(supervisor.check_config(self.base, require_heartbeat=True), [])
        self.assertEqual(supervisor.read_alert_chat(self.base), 100000001)

    def test_http_heartbeat_is_rejected(self):
        os.environ["OPERATOR_COMMS_HEARTBEAT_URL"] = "http://hc.example.invalid/ping"
        with self.assertRaises(ValueError):
            supervisor.read_heartbeat_url()

    def test_heartbeat_without_alert_chat_is_valid(self):
        os.environ["DATAOPS_TELEGRAM_BOT_TOKEN"] = "fake-token-value"
        os.environ["OPERATOR_COMMS_HEARTBEAT_URL"] = "https://hc.example.invalid/ping/abc"
        self.assertEqual(supervisor.check_config(self.base, require_heartbeat=True), [])
        self.assertIsNone(supervisor.read_alert_chat(self.base))


class StatusTests(unittest.TestCase):
    def status(self, **over):
        now = time.time()
        s = {"updated_at": now, "started_at": now - 1000, "heartbeat_interval": 60,
             "last_heartbeat_ok": now - 30, "last_heartbeat_error": "",
             "children": {"telegram-relay": {"alive": True, "last_exit": ""}}}
        s.update(over)
        return s

    def test_healthy(self):
        self.assertEqual(supervisor.evaluate_status(self.status(), time.time()), [])

    def test_stale_status_down_child_and_missing_heartbeat_are_problems(self):
        now = time.time()
        s = self.status(updated_at=now - 120, last_heartbeat_ok=now - 600, last_heartbeat_error="HTTP 500",
                        children={"telegram-relay": {"alive": False, "last_exit": "rc=1 after 3s"}})
        problems = "\n".join(supervisor.evaluate_status(s, now))
        self.assertIn("stale", problems)
        self.assertIn("telegram-relay is down", problems)
        self.assertIn("HTTP 500", problems)


class SuperviseTests(unittest.TestCase):
    """End to end: a crashing child is restarted, alerted and reported in the status file."""

    def test_crash_is_restarted_and_alerted(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            marker = base / "runs"
            script = base / "child.py"
            script.write_text("import sys\nopen(sys.argv[1], 'a').write('x')\nsys.exit(7)\n")
            children = [supervisor.Child("flaky", [sys.executable, str(script), str(marker)])]
            alerts = []
            alert = lambda key, text, force=False: alerts.append(text) or True  # noqa: E731

            with mock.patch.object(supervisor, "MIN_RESTART_DELAY", 0), \
                    mock.patch.object(supervisor.time, "sleep", side_effect=lambda s: time_sleep(0.05)):
                children[0].restart_delay = 0
                deadline = time.time() + 5

                def stop_when_restarted(*_a):
                    if (marker.exists() and len(marker.read_text()) >= 2) or time.time() > deadline:
                        raise KeyboardInterrupt
                with mock.patch.object(supervisor, "write_status",
                                       side_effect=lambda *a: (real_write(*a), stop_when_restarted())):
                    try:
                        supervisor.supervise(children, base, None, 60, alert)
                    except KeyboardInterrupt:
                        pass

            self.assertGreaterEqual(len(marker.read_text()), 2)
            self.assertTrue(any("CRASH: flaky exited rc=7" in a for a in alerts))
            self.assertTrue(any("supervisor started" in a for a in alerts))
            self.assertTrue(any("supervisor stopped" in a for a in alerts))


time_sleep = time.sleep
real_write = supervisor.write_status


if __name__ == "__main__":
    unittest.main()
