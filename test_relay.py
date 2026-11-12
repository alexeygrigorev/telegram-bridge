import importlib
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
# Fake ids for tests only.
GROUP = -1000000000001
OWNER = 100000001
MEMBER = 100000002


def load_relay(tmp):
    os.environ["DATAOPS_TELEGRAM_BASE_DIR"] = tmp
    for k in ("DATAOPS_TELEGRAM_ALLOWED_USERS", "DATAOPS_TELEGRAM_FORWARD_CHATS", "DATAOPS_TELEGRAM_ALERT_CHAT"):
        os.environ.pop(k, None)
    sys.path.insert(0, HERE)
    sys.modules.pop("relay", None)
    return importlib.import_module("relay")


def group_msg(uid, text="Mailchimp test is ready", chat_id=GROUP, chat_type="supergroup"):
    return {"message_id": 7, "date": 1, "text": text, "from": {"id": uid, "username": "u%s" % uid},
            "chat": {"id": chat_id, "type": chat_type}}


class RelayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.relay = load_relay(self.tmp.name)
        with open(os.path.join(self.tmp.name, "allowed_users.txt"), "w") as f:
            f.write("%d # owner\n" % OWNER)
        with open(os.path.join(self.tmp.name, "forward_chats.txt"), "w") as f:
            f.write("%d # team group\n" % GROUP)
        with open(os.path.join(self.tmp.name, "alert_chat.txt"), "w") as f:
            f.write("%d\n" % OWNER)
        self.sent = []
        self.relay.safe_tg = lambda method, params: self.sent.append((method, params)) or {"ok": True}
        self.relay._last_alert.clear()

    def messages(self):
        return [p for m, p in self.sent if m == "sendMessage"]

    def test_plain_group_message_from_non_allowlisted_member_is_forwarded(self):
        with mock.patch.object(self.relay, "post_webhook", return_value=(True, "ok")) as post:
            self.relay.handle_message(group_msg(MEMBER), {})
        post.assert_called_once()
        payload = post.call_args[0][0]
        self.assertEqual(payload["user_id"], MEMBER)
        self.assertEqual(payload["text"], "Mailchimp test is ready")
        self.assertEqual(self.messages(), [])
        self.assertFalse(os.path.exists(self.relay.DENIED_FILE))

    def test_forwarded_message_is_logged_with_text_date_and_reply_to(self):
        msg = group_msg(MEMBER, text="Draft is in the doc, @orin please check")
        msg["date"] = 1794409200  # 2026-11-11T15:00:00Z
        msg["reply_to_message"] = {"message_id": 5}
        os.environ[self.relay.TOKEN_ENV] = "123456:SECRET-TOKEN"
        self.addCleanup(os.environ.pop, self.relay.TOKEN_ENV, None)
        msg["text"] += " 123456:SECRET-TOKEN"
        with mock.patch.object(self.relay, "post_webhook", return_value=(True, "ok")):
            self.relay.handle_message(msg, {})
        for h in self.relay.log.handlers:
            h.flush()
        with open(self.relay.LOG_FILE) as f:
            line = [l for l in f if " forwarded " in l][0]
        for part in ("message_id=7", "user_id=%d" % MEMBER, "date=2026-11-11T15:00:00Z", "reply_to=5",
                     "Draft is in the doc, @orin please check"):
            self.assertIn(part, line)
        self.assertNotIn("SECRET-TOKEN", line)

    def test_forward_chats_extendable_from_file_and_env(self):
        with open(self.relay.CHATS_FILE, "a") as f:
            f.write("-100555  # other group\n")
        os.environ["DATAOPS_TELEGRAM_FORWARD_CHATS"] = "-100666"
        self.addCleanup(os.environ.pop, "DATAOPS_TELEGRAM_FORWARD_CHATS")
        self.assertEqual(self.relay.forward_chats(), {GROUP, -100555, -100666})

    def test_denied_elsewhere_is_logged_error_recorded_and_alerted(self):
        with mock.patch.object(self.relay, "post_webhook") as post, \
                self.assertLogs("relay", level="ERROR") as logs:
            self.relay.handle_message(group_msg(MEMBER, chat_id=-100999), {})
        post.assert_not_called()
        self.assertTrue(any("DENIED" in line for line in logs.output))
        self.assertTrue(os.path.exists(self.relay.DENIED_FILE))
        alerts = [m for m in self.messages() if m["chat_id"] == OWNER]
        self.assertEqual(len(alerts), 1)
        self.assertIn("DENIED", alerts[0]["text"])
        # No "bot is private" reply into a group.
        self.assertEqual([m for m in self.messages() if m["chat_id"] == -100999], [])

    def test_denied_private_chat_still_gets_private_reply_and_alert(self):
        msg = group_msg(MEMBER, chat_id=MEMBER, chat_type="private")
        with self.assertLogs("relay", level="ERROR"):
            self.relay.handle_message(msg, {})
        chats = [m["chat_id"] for m in self.messages()]
        self.assertIn(OWNER, chats)
        self.assertIn(MEMBER, chats)

    def test_repeated_denied_alert_is_collapsed_but_every_drop_logged(self):
        with self.assertLogs("relay", level="ERROR") as logs:
            self.relay.handle_message(group_msg(MEMBER, chat_id=-100999), {})
            self.relay.handle_message(group_msg(MEMBER, chat_id=-100999), {})
        self.assertEqual(sum("DENIED" in line for line in logs.output), 2)
        self.assertEqual(len([m for m in self.messages() if m["chat_id"] == OWNER]), 1)

    def test_webhook_failure_is_error_and_alerts(self):
        with mock.patch.object(self.relay, "post_webhook", return_value=(False, "webhook_failed: HTTP 500")), \
                self.assertLogs("relay", level="ERROR") as logs:
            self.relay.handle_message(group_msg(MEMBER), {})
        self.assertTrue(any("NOT FORWARDED" in line for line in logs.output))
        self.assertTrue(os.path.exists(self.relay.INBOX_FILE))
        alerts = [m for m in self.messages() if m["chat_id"] == OWNER]
        self.assertEqual(len(alerts), 1)
        self.assertIn("NOT delivered", alerts[0]["text"])


    def test_no_ids_are_built_in(self):
        for name in ("allowed_users.txt", "forward_chats.txt", "alert_chat.txt"):
            os.remove(os.path.join(self.tmp.name, name))
        self.assertEqual(self.relay.allowed_users(), set())
        self.assertEqual(self.relay.forward_chats(), set())
        self.assertIsNone(self.relay.alert_chat())

    def test_alert_chat_env_overrides_file(self):
        os.environ["DATAOPS_TELEGRAM_ALERT_CHAT"] = "-100777"
        self.addCleanup(os.environ.pop, "DATAOPS_TELEGRAM_ALERT_CHAT")
        self.assertEqual(self.relay.alert_chat(), -100777)

    def test_personal_chat_never_receives_system_alerts(self):
        for source in ("env", "file"):
            with self.subTest(source=source):
                os.environ.pop("DATAOPS_TELEGRAM_ALERT_CHAT", None)
                with open(self.relay.ALERT_CHAT_FILE, "w") as f:
                    f.write("822266080\n" if source == "file" else str(OWNER))
                if source == "env":
                    os.environ["DATAOPS_TELEGRAM_ALERT_CHAT"] = "822266080"
                self.assertIsNone(self.relay.alert_chat())
                self.assertFalse(self.relay.alert("failure", "webhook failed"))
        self.assertEqual(self.messages(), [])

    def test_missing_alert_chat_is_logged_not_sent(self):
        os.remove(os.path.join(self.tmp.name, "alert_chat.txt"))
        with self.assertLogs("relay", level="ERROR") as logs:
            self.relay.handle_message(group_msg(MEMBER, chat_id=-100999), {})
        self.assertTrue(any("ALERT NOT DELIVERED" in line for line in logs.output))
        self.assertEqual(self.messages(), [])


if __name__ == "__main__":
    unittest.main()
