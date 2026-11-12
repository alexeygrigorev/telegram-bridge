#!/usr/bin/env python3
"""Telegram bot -> webhook relay.

Long-polls getUpdates and forwards messages from allowlisted users and from
forward-all group chats to a webhook (or to inbox.jsonl when the webhook isn't
configured / fails). Every dropped message is reported loudly.
Python 3 stdlib only. The bot token and webhook key are read from the
environment at runtime and are never logged or written to disk.
"""
import fcntl
import json
import logging
import logging.handlers
import os
import sys
import time
import urllib.error
import urllib.request

BASE_DIR = os.environ.get("DATAOPS_TELEGRAM_BASE_DIR") or os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")
ALLOW_FILE = os.path.join(BASE_DIR, "allowed_users.txt")
INBOX_FILE = os.path.join(BASE_DIR, "inbox.jsonl")
DENIED_FILE = os.path.join(BASE_DIR, "denied.jsonl")
CHATS_FILE = os.path.join(BASE_DIR, "forward_chats.txt")
ALERT_CHAT_FILE = os.path.join(BASE_DIR, "alert_chat.txt")
LOG_FILE = os.path.join(BASE_DIR, "relay.log")
LOCK_FILE = os.path.join(BASE_DIR, ".relay.lock")

POLL_TIMEOUT = 50
TOKEN_ENV = "DATAOPS_TELEGRAM_BOT_TOKEN"

# No chat or user ids are built in. Forward-all group chats come from
# forward_chats.txt / DATAOPS_TELEGRAM_FORWARD_CHATS, the allowlist from
# allowed_users.txt / DATAOPS_TELEGRAM_ALLOWED_USERS, and the chat that receives
# drop/failure alerts from alert_chat.txt / DATAOPS_TELEGRAM_ALERT_CHAT.
ALERT_COOLDOWN = 600  # seconds between repeated alerts for the same key


class _Redact(logging.Filter):
    """Belt and braces: scrub secrets from any log record."""

    def filter(self, record):
        secrets = [s for s in (os.environ.get(TOKEN_ENV), os.environ.get("DATAOPS_ROUTINE_WEBHOOK_KEY")) if s]
        if secrets:
            msg = record.getMessage()
            for s in secrets:
                msg = msg.replace(s, "***")
            record.msg, record.args = msg, None
            if record.exc_info:
                text = logging.Formatter().formatException(record.exc_info)
                for s in secrets:
                    text = text.replace(s, "***")
                record.exc_text, record.exc_info = text, None
        return True


def setup_logging():
    log = logging.getLogger("relay")
    log.setLevel(logging.INFO)
    h = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    h.addFilter(_Redact())
    log.addHandler(h)
    return log


log = setup_logging()


def token():
    t = os.environ.get(TOKEN_ENV, "").strip()
    if not t:
        log.error("%s is not set in the environment; exiting", TOKEN_ENV)
        sys.exit(2)
    return t


class TelegramError(Exception):
    pass


def tg(method, params=None, timeout=30):
    """Call a Telegram Bot API method. Errors never include the URL/token."""
    url = "https://api.telegram.org/bot%s/%s" % (token(), method)
    data = json.dumps(params or {}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            desc = json.loads(e.read().decode()).get("description", "")
        except Exception:
            desc = ""
        raise TelegramError("%s failed: HTTP %s %s" % (method, e.code, desc)) from None
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise ConnectionError("%s network error: %s" % (method, getattr(e, "reason", e))) from None
    if not body.get("ok"):
        raise TelegramError("%s failed: %s" % (method, body.get("description")))
    return body["result"]


# ---------- state ----------

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {"offset": 0, "denied_notified": []}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def read_ids(env_name, path):
    """Numeric ids from a comma/newline separated env var plus a file (`#` comments OK)."""
    ids = set()
    lines = os.environ.get(env_name, "").replace(",", "\n").splitlines() if env_name else []
    if path:
        try:
            with open(path) as f:
                lines += f.read().splitlines()
        except FileNotFoundError:
            pass
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if line.lstrip("-").isdigit():
            ids.add(int(line))
    return ids


def allowed_users():
    return read_ids("DATAOPS_TELEGRAM_ALLOWED_USERS", ALLOW_FILE)


def forward_chats():
    return read_ids("DATAOPS_TELEGRAM_FORWARD_CHATS", CHATS_FILE)


def alert_chat():
    """Chat id for loud alerts, or None when not configured (env wins over file)."""
    ids = read_ids("DATAOPS_TELEGRAM_ALERT_CHAT", None)
    if not ids:
        ids = read_ids(None, ALERT_CHAT_FILE)
    return next(iter(ids)) if len(ids) == 1 else None


_last_alert = {}


def alert(key, text):
    """Loud report: ERROR log (caller) plus a Telegram DM to the alert chat.

    Repeats of the same key within ALERT_COOLDOWN are collapsed so a stuck webhook
    cannot flood the chat; every occurrence is still logged at ERROR by the caller.
    Returns True when a DM was sent."""
    now = time.time()
    if now - _last_alert.get(key, 0) < ALERT_COOLDOWN:
        return False
    chat = alert_chat()
    if chat is None:
        log.error("ALERT NOT DELIVERED (no alert chat configured) (%s): %s", key, text)
        return False
    sent = safe_tg("sendMessage", {"chat_id": chat, "text": "[telegram-relay] " + text}) is not None
    if sent:
        _last_alert[key] = now
    else:
        log.error("ALERT NOT DELIVERED (%s): %s", key, text)
    return sent


def append_jsonl(path, payload, **extra):
    rec = dict(payload)
    rec.update(extra)
    with open(path, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def append_inbox(payload, reason):
    rec = dict(payload)
    rec["_queued_at"] = int(time.time())
    rec["_reason"] = reason
    with open(INBOX_FILE, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------- message handling ----------

def extract(msg):
    text = msg.get("text") or msg.get("caption") or ""
    atts = []
    if msg.get("photo"):
        p = max(msg["photo"], key=lambda x: x.get("file_size", 0) or x.get("width", 0) * x.get("height", 0))
        atts.append({"type": "photo", "file_id": p["file_id"]})
    for kind in ("voice", "document", "audio", "video", "video_note", "sticker", "animation"):
        if msg.get(kind):
            o = msg[kind]
            a = {"type": kind, "file_id": o.get("file_id")}
            for k in ("file_name", "mime_type", "duration"):
                if k in o:
                    a[k] = o[k]
            atts.append(a)
    for kind in ("location", "contact"):
        if msg.get(kind):
            atts.append({"type": kind, "data": msg[kind]})
    return text, atts


def post_webhook(payload):
    """Returns (ok, reason). Retries up to 3 times with backoff."""
    url = os.environ.get("DATAOPS_ROUTINE_WEBHOOK_URL", "").strip()
    key = os.environ.get("DATAOPS_ROUTINE_WEBHOOK_KEY", "").strip()
    if not url:
        return False, "webhook_not_configured"
    header = os.environ.get("DATAOPS_ROUTINE_WEBHOOK_HEADER", "").strip() or "Authorization"
    headers = {"Content-Type": "application/json"}
    if key:
        if header.lower() == "authorization" and not key.startswith("Bearer "):
            key = "Bearer " + key
        headers[header] = key
    data = json.dumps(payload, ensure_ascii=False).encode()
    last = ""
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=20) as r:
                if 200 <= r.status < 300:
                    return True, "ok"
                last = "HTTP %s" % r.status
        except urllib.error.HTTPError as e:
            last = "HTTP %s" % e.code
        except Exception as e:  # network etc.
            last = type(e).__name__
        log.warning("webhook POST attempt %d failed: %s", attempt, last)
        if attempt < 3:
            time.sleep(2 ** attempt)
    return False, "webhook_failed: " + last


def safe_tg(method, params):
    try:
        return tg(method, params)
    except Exception as e:
        log.warning("%s", e)
        return None


def handle_message(msg, state):
    frm = msg.get("from") or {}
    chat = msg.get("chat") or {}
    uid = frm.get("id")
    chat_id = chat.get("id")
    text, atts = extract(msg)

    if uid not in allowed_users() and chat_id not in forward_chats():
        log.error("DENIED user_id=%s username=%s chat_id=%s message_id=%s text=%r attachments=%s",
                  uid, frm.get("username"), chat_id, msg.get("message_id"), text[:80], [a["type"] for a in atts])
        try:
            append_jsonl(DENIED_FILE, {"user_id": uid, "username": frm.get("username"), "chat_id": chat_id,
                                       "message_id": msg.get("message_id"), "date": msg.get("date"),
                                       "text": text, "attachments": atts}, _denied_at=int(time.time()))
        except OSError as e:
            log.error("could not record denied message: %s", e)
        alert("denied:%s:%s" % (chat_id, uid),
              "DENIED message from user_id=%s (@%s) in chat_id=%s, not forwarded: %r"
              % (uid, frm.get("username"), chat_id, text[:80]))
        notified = state.setdefault("denied_notified", [])
        # The "bot is private" reply is for private chats only; never post it into groups.
        if uid not in notified and chat_id is not None and (chat.get("type") or "private") == "private":
            if safe_tg("sendMessage", {"chat_id": chat_id,
                                       "text": "Sorry, this bot is private. Your Telegram user id is %s." % uid}) is not None:
                notified.append(uid)
        return

    safe_tg("sendChatAction", {"chat_id": chat_id, "action": "typing"})
    payload = {
        "source": "telegram",
        "chat_id": chat_id,
        "user_id": uid,
        "username": frm.get("username"),
        "first_name": frm.get("first_name"),
        "message_id": msg.get("message_id"),
        "date": msg.get("date"),
        "text": text,
        "attachments": atts,
    }
    if msg.get("reply_to_message"):
        payload["reply_to_message_id"] = msg["reply_to_message"].get("message_id")
    ok, reason = post_webhook(payload)
    if ok:
        # Full content in the log so messages can be recovered if the routine loses them.
        log.info("forwarded %s", describe(payload))
        return
    append_inbox(payload, reason)
    log.error("NOT FORWARDED, queued to inbox.jsonl %s (%s)", describe(payload), reason)
    alert("webhook:" + reason, "message_id=%s from user_id=%s in chat_id=%s was NOT delivered to the routine (%s); "
          "queued in inbox.jsonl" % (payload["message_id"], uid, chat_id, reason))
    if reason == "webhook_not_configured":
        safe_tg("sendMessage", {"chat_id": chat_id, "text": "Got it, the assistant isn't connected yet.",
                                "reply_to_message_id": msg.get("message_id")})


def describe(payload):
    """One log line with everything needed to recover a message: ids, date, reply_to, text."""
    date = payload.get("date")
    when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(date)) if isinstance(date, (int, float)) else date
    return ("message_id=%s chat_id=%s user_id=%s username=%s date=%s reply_to=%s attachments=%d text=%r"
            % (payload.get("message_id"), payload.get("chat_id"), payload.get("user_id"), payload.get("username"),
               when, payload.get("reply_to_message_id"), len(payload.get("attachments") or []),
               (payload.get("text") or "")[:4000]))


# ---------- main loop ----------

def main():
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.error("another relay instance holds the lock; exiting")
        print("another relay instance is already running", file=sys.stderr)
        sys.exit(3)

    token()
    me = None
    backoff = 1
    while me is None:
        try:
            me = tg("getMe")
        except Exception as e:
            log.error("getMe: %s; retrying in %ss", e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
    log.info("started: getMe ok=True username=@%s; polling (timeout=%s)", me.get("username"), POLL_TIMEOUT)
    if alert_chat() is None:
        log.error("no alert chat configured (alert_chat.txt or DATAOPS_TELEGRAM_ALERT_CHAT): "
                  "drops and failures will only be logged")

    state = load_state()
    backoff = 1
    while True:
        try:
            updates = tg("getUpdates", {"offset": state.get("offset", 0), "timeout": POLL_TIMEOUT,
                                        "allowed_updates": ["message"]}, timeout=POLL_TIMEOUT + 15)
            backoff = 1
        except Exception as e:
            log.warning("getUpdates: %s; backing off %ss", e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
            continue
        for upd in updates:
            try:
                if upd.get("message"):
                    handle_message(upd["message"], state)
                else:
                    log.error("update_id=%s has no message (keys=%s); not handled",
                              upd.get("update_id"), sorted(upd))
            except Exception as e:
                log.exception("error handling update_id=%s", upd.get("update_id"))
                alert("handler:" + type(e).__name__, "error handling update_id=%s (%s: %s); message dropped"
                      % (upd.get("update_id"), type(e).__name__, e))
            state["offset"] = upd["update_id"] + 1
            save_state(state)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("stopped (SIGINT)")
