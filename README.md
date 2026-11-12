# telegram-bridge

A small Telegram bot → webhook relay with a supervisor, written in Python 3
with only the standard library.

- `relay.py` long-polls the Bot API (`getUpdates`, no Telegram webhook). It
  forwards messages from allowlisted users, and every message in "forward-all"
  group chats, to an HTTPS webhook such as an agent routine. When the webhook
  is missing or failing, messages are queued in `inbox.jsonl`. A message is
  never dropped silently: each drop gets an `ERROR` log line, a record in
  `denied.jsonl` and a Telegram alert.
- `supervisor.py` keeps the relay (and any extra commands) running. It
  restarts crashed children with backoff and sends a Telegram alert on every
  crash. It also pings an external heartbeat (dead-man switch) while all
  children are alive.
- `tg-reply` is a CLI for sending replies, long texts and files as the bot.

No tokens, chat ids or user ids are built in. Secrets come from the
environment. Ids come from the environment or from small files in the base
directory, and those files are git-ignored.

## Install

```bash
git clone https://github.com/alexeygrigorev/telegram-bridge.git
cd telegram-bridge
python3 -m unittest discover -p 'test_*.py'      # optional
sudo install -m 755 tg-reply /usr/local/bin/tg-reply   # optional
```

By default the base directory, which holds state, config and logs, is the
checkout itself. To use another one, pass `--base-dir` or set
`DATAOPS_TELEGRAM_BASE_DIR`.

## Configuration

### Environment (secrets: keep them in your secret store, never in git or logs)

| Variable | Required | Purpose |
|---|---|---|
| `DATAOPS_TELEGRAM_BOT_TOKEN` | yes | Bot token from BotFather. |
| `OPERATOR_COMMS_HEARTBEAT_URL` | supervisor | HTTPS ping URL of an external dead-man check (healthchecks.io, Better Stack, Cronitor, ...). If pings stop, that service should page the on-call. |
| `OPERATOR_COMMS_HEARTBEAT_TOKEN` | no | Bearer token sent with each heartbeat, for endpoints that need auth. Heartbeats are a JSON `POST` (`{"source":"telegram-bridge","host":...}`). |
| `DATAOPS_ROUTINE_WEBHOOK_URL` | no | Where messages are POSTed. If it is unset, messages are queued in `inbox.jsonl`. |
| `DATAOPS_ROUTINE_WEBHOOK_KEY` | no | Value of the webhook auth header. For `Authorization`, `Bearer ` is added if missing. |
| `DATAOPS_ROUTINE_WEBHOOK_HEADER` | no | Name of the auth header (default `Authorization`). |
| `DATAOPS_TELEGRAM_ALLOWED_USERS` | no | Extra allowed user ids, comma separated. |
| `DATAOPS_TELEGRAM_FORWARD_CHATS` | no | Extra forward-all chat ids, comma separated. |
| `DATAOPS_TELEGRAM_ALERT_CHAT` | no | Chat id for alerts (overrides `alert_chat.txt`). |
| `DATAOPS_TELEGRAM_BASE_DIR` | no | Directory for state, config and logs. |

The variables keep their `DATAOPS_` prefix because the first deployment
already used these names.

### Files in the base directory (git-ignored)

Each file holds one id per line. `#` comments are allowed. The relay re-reads
the files on every message, so changes need no restart.

| File | Purpose |
|---|---|
| `allowed_users.txt` | User ids whose messages are forwarded from any chat. |
| `forward_chats.txt` | Group chat ids whose messages are all forwarded, from any member. |
| `alert_chat.txt` | One chat id that receives alerts about drops, webhook failures, crashes and heartbeat failures. |

If no alert chat is configured, alerts only go to the log. `--check`
still passes when an external heartbeat is configured, so the dead-man can
page the on-call. Without both an alert chat and a heartbeat, `--check`
fails.

### Webhook payload

```json
{"source":"telegram","chat_id":123,"user_id":123,"username":"...","first_name":"...",
 "message_id":42,"date":1760000000,"text":"...","attachments":[{"type":"photo","file_id":"..."}],
 "reply_to_message_id":41}
```

The relay tries 3 times with backoff. After that, the payload goes to
`inbox.jsonl` with `_queued_at` and `_reason`.

## Run in production

```bash
python3 supervisor.py --check          # validates env and files, prints no values
python3 supervisor.py                  # foreground: use as a service / container command
python3 supervisor.py --ensure         # starts detached unless already running (boot or login hooks)
python3 supervisor.py --status         # prints health; exits 1 when something is wrong
```

Pass `--child NAME=COMMAND` (repeatable) to supervise more processes next to
the relay, for example a sync loop:

```bash
python3 supervisor.py --child "library-sync=python3 /srv/repo/scripts/sync.py --daemon loop"
```

Only one supervisor runs per base directory (lock file `.supervisor.lock`),
and only one relay (lock file `.relay.lock`). A second relay exits with code
3, and the supervisor reports it as a likely double start.

### Surviving reboots

Run the supervisor in the foreground from whatever your host starts at boot.
Options:

- a systemd unit (`ExecStart=/usr/bin/python3 /opt/telegram-bridge/supervisor.py`, `Restart=always`, secrets via `EnvironmentFile=` with mode 600);
- a container command with a restart policy;
- on hosts without an init system, a hook that runs at boot or first login: `python3 /path/supervisor.py --ensure >/dev/null 2>&1`. The hook must run in an environment that has the secrets.

### Alerts

| Event | Alert |
|---|---|
| A child exits or crashes | Logged always. If an alert chat is set, also a Telegram message ("CRASH: ..."), at most one per child every 10 minutes. Heartbeats are withheld while a child is down or not yet stable, so a crash-loop pages the on-call through the dead-man. |
| The supervisor starts or stops | Logged always. If an alert chat is set, also a Telegram message (a start after a reboot shows that it came back). A stop ends heartbeats, so the dead-man pages the on-call. |
| A heartbeat ping fails | Logged always. If an alert chat is set, also a Telegram message. The URL is never logged. |
| The host is down, the supervisor hangs, or a child crash-loops | The heartbeat service stops getting pings and pages the on-call. |

### Health and logs

```bash
python3 supervisor.py --status   # children alive, restarts, last exit, heartbeat age
tail -f relay.log                # relay (rotates at 5 MB)
tail supervisor.log              # supervisor (when started with --ensure)
tail inbox.jsonl denied.jsonl    # queued / dropped messages
```

Every forwarded message is logged in `relay.log` with its ids, date (UTC), the
message it replies to, and its text, so messages can be recovered from the log
if the routine loses them. The bot token and webhook key are scrubbed from all
log lines.

## tg-reply

```bash
tg-reply <chat_id> "text"
echo "long text" | tg-reply <chat_id> --reply-to <message_id>
tg-reply <chat_id> --file report.pdf "optional caption"
tg-reply <chat_id> --plain "no Markdown parsing"
```

Texts over 4000 characters are split into several messages. The tool tries
Markdown first and resends as plain text if Telegram rejects it. It reads the
token from `DATAOPS_TELEGRAM_BOT_TOKEN`.

## License

MIT
