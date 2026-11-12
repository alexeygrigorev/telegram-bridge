import io
import json
import signal
from contextlib import redirect_stdout
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

    def test_personal_chat_is_rejected_from_env_and_file_without_sending(self):
        for source in ("env", "file"):
            with self.subTest(source=source):
                os.environ.pop("DATAOPS_TELEGRAM_ALERT_CHAT", None)
                (self.base / "alert_chat.txt").write_text("822266080\n")
                if source == "env":
                    # A blocked explicit value must not fall back to a file.
                    (self.base / "alert_chat.txt").write_text("100000001\n")
                    os.environ["DATAOPS_TELEGRAM_ALERT_CHAT"] = "822266080"
                os.environ["DATAOPS_TELEGRAM_BOT_TOKEN"] = "fake-token"
                os.environ["OPERATOR_COMMS_HEARTBEAT_URL"] = "https://hc.example.invalid/check"
                self.assertIsNone(supervisor.read_alert_chat(self.base))
                self.assertEqual(supervisor.check_config(self.base, require_heartbeat=True), [])
                self.assertTrue(supervisor.check_config(self.base, require_heartbeat=False))
                with mock.patch.object(supervisor.urllib.request, "urlopen") as send:
                    self.assertFalse(supervisor.Alerter(self.base, "test")("crash", "CRASH: test"))
                send.assert_not_called()


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
    """Real children complete two observed cycles; network alerts are captured."""

    def test_exit_classification_and_restart(self):
        for rc in (0, 7, -signal.SIGTERM):
            with self.subTest(rc=rc), tempfile.TemporaryDirectory() as folder:
                base = Path(folder)
                command = ("import os, signal; os.kill(os.getpid(), signal.SIGTERM)"
                           if rc < 0 else f"import sys; sys.exit({rc})")
                child = supervisor.Child("sync", [sys.executable, "-c", command])
                alerts = []
                statuses = []
                output = io.StringIO()
                clock = [100.0]
                real_write = supervisor.write_status

                def advance(_seconds):
                    if child.restarts >= 2:
                        raise KeyboardInterrupt
                    if child.process is not None:
                        child.process.wait(timeout=5)
                    clock[0] += 10

                def capture_status(*args):
                    real_write(*args)
                    statuses.append(json.loads((base / "supervisor.status.json").read_text()))

                timer = mock.Mock(wraps=time)
                timer.monotonic.side_effect = lambda: clock[0]
                timer.sleep.side_effect = advance
                with mock.patch.object(supervisor.signal, "signal"), \
                        mock.patch.object(supervisor, "time", timer), \
                        mock.patch.object(supervisor, "write_status", side_effect=capture_status), \
                        redirect_stdout(output):
                    with self.assertRaises(KeyboardInterrupt):
                        supervisor.supervise([child], base, None, 60,
                                             lambda key, text, force=False: alerts.append(text))

                self.assertEqual(child.restarts, 2)
                self.assertEqual(output.getvalue().count(f"child exited name=sync rc={rc}"), 2)
                self.assertIn("restarting in", output.getvalue())
                self.assertEqual(statuses[-1]["children"]["sync"]["last_exit"], f"rc={rc} after 10s")
                crash_alerts = [text for text in alerts if text.startswith("CRASH:")]
                if rc == 0:
                    self.assertEqual(crash_alerts, [])
                    self.assertNotIn("CRASH", output.getvalue())
                else:
                    self.assertEqual(len(crash_alerts), 2)
                    self.assertIn(f"CRASH: sync exited rc={rc}", crash_alerts[0])

    def test_python_children_are_unbuffered_and_keep_environment(self):
        child = supervisor.Child("sync", [sys.executable, "sync.py"])
        with mock.patch.dict(os.environ, {"PYTHONUNBUFFERED": "0", "SYNC_SETTING": "kept"}), \
                mock.patch.object(supervisor.subprocess, "Popen") as popen:
            supervisor.start_child(child, 100)
        self.assertEqual(popen.call_args.kwargs["env"]["PYTHONUNBUFFERED"], "1")
        self.assertEqual(popen.call_args.kwargs["env"]["SYNC_SETTING"], "kept")


if __name__ == "__main__":
    unittest.main()
