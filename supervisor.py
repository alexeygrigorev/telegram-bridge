#!/usr/bin/env python3
"""Keep the Telegram relay (and optional extra commands) running.

Foreground supervisor for relay.py. It:

* restarts every child with bounded backoff when it exits;
* sends an external HTTPS heartbeat (dead-man switch) only while every child
  is alive, so a crash loop, a hung supervisor or a dead host all show up as
  missing pings at the heartbeat service, which pages the on-call;
* sends a Telegram alert to an explicit operator chat whenever a child crashes,
  the heartbeat fails, or the supervisor starts/stops;
* writes supervisor.status.json so `--status` can tell whether it is healthy;
* holds a lock so only one supervisor (and so one relay) runs per base dir.

Credentials stay in the environment and never appear in logs or alerts.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shlex
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from alert_config import read_alert_chat

HERE = Path(__file__).resolve().parent
MIN_RESTART_DELAY = 5
MAX_RESTART_DELAY = 300
HEALTHY_RUNTIME_SECONDS = 60
STABLE_SECONDS = 30
ALERT_COOLDOWN = 600
TOKEN_ENV = "DATAOPS_TELEGRAM_BOT_TOKEN"
HEARTBEAT_ENV = "OPERATOR_COMMS_HEARTBEAT_URL"
HEARTBEAT_TOKEN_ENV = "OPERATOR_COMMS_HEARTBEAT_TOKEN"
RELAY_LOCK_HELD_RC = 3


@dataclass
class Child:
    name: str
    command: list[str]
    process: subprocess.Popen[bytes] | None = None
    started_at: float = 0.0
    next_start_at: float = 0.0
    restart_delay: int = MIN_RESTART_DELAY
    restarts: int = 0
    last_exit: str = ""


@dataclass
class Health:
    last_heartbeat_ok: float = 0.0
    last_heartbeat_error: str = ""
    started_at: float = field(default_factory=time.time)


def next_restart_delay(current_delay: int, runtime_seconds: float) -> int:
    if runtime_seconds >= HEALTHY_RUNTIME_SECONDS:
        return MIN_RESTART_DELAY
    return min(max(current_delay, MIN_RESTART_DELAY) * 2, MAX_RESTART_DELAY)


def child_is_stable(child: Child, now: float) -> bool:
    """Alive and up for STABLE_SECONDS, so a crash loop cannot slip a heartbeat through."""
    alive = child.process is not None and child.process.poll() is None
    return alive and now - child.started_at >= STABLE_SECONDS


def heartbeat_failure_message(error: BaseException) -> str:
    """Return a useful heartbeat error without exposing its URL or response."""
    if isinstance(error, urllib.error.HTTPError):
        return f"external heartbeat returned HTTP {error.code}"
    return f"external heartbeat failed ({type(error).__name__})"


def log(message: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"{now} {message}", flush=True)


def parse_child(spec: str) -> Child:
    name, sep, command = spec.partition("=")
    name = name.strip()
    if not sep or not name or not command.strip():
        raise ValueError(f"--child must look like NAME=COMMAND, got {spec!r}")
    return Child(name, shlex.split(command))


def make_children(base_dir: Path, extra: list[str]) -> list[Child]:
    children = [Child("telegram-relay", [sys.executable, "-u", str(HERE / "relay.py")])]
    names = {"telegram-relay"}
    for spec in extra:
        child = parse_child(spec)
        if child.name in names:
            raise ValueError(f"duplicate child name {child.name!r}")
        names.add(child.name)
        children.append(child)
    return children


def read_heartbeat_url() -> str:
    url = os.environ.get(HEARTBEAT_ENV, "").strip()
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        raise ValueError(f"{HEARTBEAT_ENV} must be a valid HTTPS URL") from None
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"{HEARTBEAT_ENV} must be an HTTPS URL")
    return url


def check_config(base_dir: Path, require_heartbeat: bool) -> list[str]:
    """Return a list of problems (empty when valid). Never includes values."""
    problems = []
    if not os.environ.get(TOKEN_ENV, "").strip():
        problems.append(f"{TOKEN_ENV} is required")
    heartbeat_ok = False
    if require_heartbeat:
        try:
            read_heartbeat_url()
            heartbeat_ok = True
        except ValueError as error:
            problems.append(str(error))
    if not (HERE / "relay.py").is_file():
        problems.append("relay.py is missing next to supervisor.py")
    if not base_dir.is_dir():
        problems.append(f"base dir {base_dir} does not exist")
    elif read_alert_chat(base_dir) is None and not heartbeat_ok:
        problems.append(
            "no operator alert chat: set DATAOPS_TELEGRAM_ALERT_CHAT or write one non-personal id to alert_chat.txt "
            "(or configure an HTTPS heartbeat so the dead-man can page instead)"
        )
    return problems


class Alerter:
    """Telegram alerts to the relay's alert chat, collapsed per key."""

    def __init__(self, base_dir: Path, host: str):
        self.base_dir = base_dir
        self.host = host
        self.last: dict[str, float] = {}

    def __call__(self, key: str, text: str, force: bool = False) -> bool:
        now = time.time()
        if not force and now - self.last.get(key, 0) < ALERT_COOLDOWN:
            return False
        chat = read_alert_chat(self.base_dir)
        token = os.environ.get(TOKEN_ENV, "").strip()
        if chat is None or not token:
            log(f"ALERT NOT DELIVERED (no alert chat or token): {text}")
            return False
        body = json.dumps({"chat_id": chat, "text": f"[telegram-bridge supervisor @{self.host}] {text}"}).encode()
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                ok = 200 <= response.status < 300
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as error:
            log(f"ALERT NOT DELIVERED ({type(error).__name__}): {text}")
            return False
        if ok:
            self.last[key] = now
        return ok


def start_child(child: Child, now: float) -> None:
    log(f"starting child={child.name}")
    try:
        child.process = subprocess.Popen(
            child.command, stdin=subprocess.DEVNULL, start_new_session=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        child.started_at = now
    except OSError as error:
        delay = child.restart_delay
        child.restart_delay = next_restart_delay(delay, 0)
        child.next_start_at = now + delay
        child.process = None
        child.last_exit = f"could not start ({type(error).__name__})"
        log(f"could not start child={child.name} ({type(error).__name__})")


def heartbeat_request(url: str, children: list[Child]) -> urllib.request.Request:
    """POST a small JSON body; add a bearer token when OPERATOR_COMMS_HEARTBEAT_TOKEN is set."""
    headers = {"User-Agent": "telegram-bridge-supervisor", "Content-Type": "application/json"}
    token = os.environ.get(HEARTBEAT_TOKEN_ENV, "").strip()
    if token:
        headers["Authorization"] = token if token.lower().startswith("bearer ") else f"Bearer {token}"
    body = json.dumps({
        "source": "telegram-bridge",
        "host": os.uname().nodename,
        "children": [c.name for c in children],
        "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }).encode()
    return urllib.request.Request(url, data=body, headers=headers, method="POST")


def ping_heartbeat(request: urllib.request.Request) -> str | None:
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if 200 <= response.status < 300:
                return None
            return f"external heartbeat returned HTTP {response.status}"
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as error:
        return heartbeat_failure_message(error)


def stop_children(children: list[Child]) -> None:
    active = [child.process for child in children if child.process and child.process.poll() is None]
    for process in active:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 10
    for process in active:
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


def write_status(path: Path, children: list[Child], health: Health, interval: int) -> None:
    status = {
        "pid": os.getpid(),
        "updated_at": time.time(),
        "started_at": health.started_at,
        "heartbeat_interval": interval,
        "last_heartbeat_ok": health.last_heartbeat_ok,
        "last_heartbeat_error": health.last_heartbeat_error,
        "children": {
            c.name: {
                "pid": c.process.pid if c.process and c.process.poll() is None else None,
                "alive": bool(c.process and c.process.poll() is None),
                "restarts": c.restarts,
                "last_exit": c.last_exit,
            }
            for c in children
        },
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, indent=1))
    os.replace(tmp, path)


def evaluate_status(status: dict, now: float) -> list[str]:
    """Problems found in a status file (empty = healthy)."""
    problems = []
    interval = status.get("heartbeat_interval", 60)
    age = now - status.get("updated_at", 0)
    if age > 30:
        problems.append(f"status file is stale ({int(age)}s old): supervisor not running or hung")
    for name, child in status.get("children", {}).items():
        if not child.get("alive"):
            problems.append(f"child {name} is down (last exit: {child.get('last_exit') or 'n/a'})")
    last_ok = status.get("last_heartbeat_ok", 0)
    if now - status.get("started_at", now) > 3 * interval and now - last_ok > 3 * interval:
        problems.append(f"no successful external heartbeat for {int(now - last_ok)}s "
                        f"({status.get('last_heartbeat_error') or 'not attempted'})")
    return problems


def supervise(children: list[Child], base_dir: Path, heartbeat_url: str | None, interval: int,
              alert: Alerter) -> None:
    stopping = False

    def request_stop(signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    status_path = base_dir / "supervisor.status.json"
    health = Health()
    now = time.monotonic()
    for child in children:
        start_child(child, now)
    next_heartbeat_at = now + STABLE_SECONDS + 1
    next_status_at = now
    log("supervisor started")
    alert("start", "supervisor started; children: " + ", ".join(c.name for c in children), force=True)

    try:
        while not stopping:
            now = time.monotonic()
            for child in children:
                process = child.process
                if process is None:
                    if now >= child.next_start_at:
                        start_child(child, now)
                    continue
                return_code = process.poll()
                if return_code is None:
                    continue
                runtime = now - child.started_at
                delay = child.restart_delay
                child.restart_delay = next_restart_delay(delay, runtime)
                child.next_start_at = now + delay
                child.process = None
                child.restarts += 1
                child.last_exit = f"rc={return_code} after {int(runtime)}s"
                log(f"child exited name={child.name} rc={return_code} after {int(runtime)}s; restarting in {delay}s")
                hint = " (another relay holds the lock: two relays?)" if (
                    child.name == "telegram-relay" and return_code == RELAY_LOCK_HELD_RC) else ""
                if return_code != 0:
                    alert(f"exit:{child.name}", f"CRASH: {child.name} exited rc={return_code} after "
                          f"{int(runtime)}s{hint}; restarting in {delay}s")

            if now >= next_heartbeat_at:
                healthy = all(child_is_stable(c, now) for c in children)
                if not healthy:
                    log("external heartbeat withheld: a child is down or restarted less than "
                        f"{STABLE_SECONDS}s ago")
                    health.last_heartbeat_error = "withheld: child down"
                elif heartbeat_url:
                    error = ping_heartbeat(heartbeat_request(heartbeat_url, children))
                    if error:
                        log(error)
                        health.last_heartbeat_error = error
                        alert("heartbeat", f"HEARTBEAT FAILING: {error}")
                    else:
                        health.last_heartbeat_ok = time.time()
                        health.last_heartbeat_error = ""
                else:
                    health.last_heartbeat_ok = time.time()
                next_heartbeat_at = now + interval

            if now >= next_status_at:
                try:
                    write_status(status_path, children, health, interval)
                except OSError as error:
                    log(f"could not write status ({type(error).__name__})")
                next_status_at = now + 5

            time.sleep(0.5)
    finally:
        log("stopping child processes")
        stop_children(children)
        log("supervisor stopped")
        alert("stop", "supervisor stopped (SIGTERM/SIGINT); relay is DOWN until it is started again", force=True)
        try:
            status_path.unlink()
        except OSError:
            pass


def acquire_lock(base_dir: Path):
    handle = open(base_dir / ".supervisor.lock", "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def lock_is_held(base_dir: Path) -> bool:
    handle = acquire_lock(base_dir)
    if handle is None:
        return True
    handle.close()
    return False


def ensure(base_dir: Path, argv: list[str]) -> int:
    """Start a detached supervisor unless one already runs (for boot/login hooks)."""
    if lock_is_held(base_dir):
        print("supervisor already running")
        return 0
    log_path = base_dir / "supervisor.log"
    if log_path.exists() and log_path.stat().st_size > 5 * 1024 * 1024:
        os.replace(log_path, log_path.with_suffix(".log.1"))
    args = [a for a in argv if a != "--ensure"]
    with open(log_path, "a") as out:
        subprocess.Popen([sys.executable, "-u", str(Path(__file__).resolve()), *args],
                         stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(20):
        time.sleep(0.5)
        if lock_is_held(base_dir):
            print("supervisor started")
            return 0
    print(f"supervisor failed to start; see {log_path}", file=sys.stderr)
    return 1


def show_status(base_dir: Path) -> int:
    path = base_dir / "supervisor.status.json"
    try:
        status = json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        print("DOWN: no status file (supervisor not running)")
        return 1
    problems = evaluate_status(status, time.time())
    if not lock_is_held(base_dir):
        problems.insert(0, "supervisor lock is free: supervisor not running")
    print(json.dumps(status, indent=1))
    print("OK" if not problems else "PROBLEMS:\n- " + "\n- ".join(problems))
    return 0 if not problems else 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-dir", default=os.environ.get("DATAOPS_TELEGRAM_BASE_DIR") or str(HERE),
                        help="relay state/config directory (default: DATAOPS_TELEGRAM_BASE_DIR or this directory)")
    parser.add_argument("--child", action="append", default=[], metavar="NAME=COMMAND",
                        help="extra command to keep running next to the relay (repeatable)")
    parser.add_argument("--heartbeat-interval", type=int, default=60)
    parser.add_argument("--no-heartbeat", action="store_true",
                        help=f"run without {HEARTBEAT_ENV} (development only: nothing pages if the host dies)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="validate config without starting anything")
    mode.add_argument("--status", action="store_true", help="print health; exit 1 when unhealthy")
    mode.add_argument("--ensure", action="store_true", help="start detached unless already running")
    args = parser.parse_args(argv)

    base_dir = Path(args.base_dir).expanduser().resolve()
    os.environ["DATAOPS_TELEGRAM_BASE_DIR"] = str(base_dir)
    if args.status:
        return show_status(base_dir)
    if args.heartbeat_interval < 10:
        print("heartbeat interval must be at least 10s", file=sys.stderr)
        return 2
    try:
        children = make_children(base_dir, args.child)
    except ValueError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2
    problems = check_config(base_dir, require_heartbeat=not args.no_heartbeat)
    if problems:
        print("configuration error:\n- " + "\n- ".join(problems), file=sys.stderr)
        return 2
    if args.check:
        print("configuration is valid; children: " + ", ".join(c.name for c in children))
        return 0
    if args.ensure:
        return ensure(base_dir, argv)

    lock = acquire_lock(base_dir)
    if lock is None:
        print("another supervisor already runs for this base dir", file=sys.stderr)
        return 3
    heartbeat_url = None if args.no_heartbeat else read_heartbeat_url()
    alert = Alerter(base_dir, os.uname().nodename)
    supervise(children, base_dir, heartbeat_url, args.heartbeat_interval, alert)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
