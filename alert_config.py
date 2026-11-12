"""Explicit operator alert routing shared by the relay and supervisor."""
import os
from pathlib import Path


# This personal chat must never receive automated system alerts, even when an
# old deployment still has it in the environment or alert_chat.txt.
BLOCKED_ALERT_CHATS = {822266080}


def read_alert_chat(base_dir: Path) -> int | None:
    raw = os.environ.get("DATAOPS_TELEGRAM_ALERT_CHAT", "").strip()
    if not raw:
        try:
            lines = (base_dir / "alert_chat.txt").read_text().splitlines()
        except FileNotFoundError:
            lines = []
        values = [line.split("#", 1)[0].strip() for line in lines]
        values = [value for value in values if value]
        raw = values[0] if len(values) == 1 else ""
    if not raw.lstrip("-").isdigit():
        return None
    chat = int(raw)
    return chat if chat and chat not in BLOCKED_ALERT_CHATS else None
