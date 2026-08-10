"""Scheduling: when the pipeline runs, and what it does when it runs.

Settings live in the database rather than a config file so the app can change
them without editing anything on disk, and installing the schedule writes to
whatever the platform actually uses — cron on Linux and macOS, Task Scheduler
on Windows.

Collection is the only stage that cannot be caught up later: a post that scrolls
off the timeline before you fetched it is gone. Everything downstream reads from
the database and can be re-run at any time. So frequency is really a question
about collection, and the defaults reflect that.
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
from pathlib import Path

from . import db

ROOT = Path(__file__).resolve().parent.parent
MARKER = "# ai-signal-tracker"
WINDOWS = platform.system() == "Windows"
TASK_NAME = "AI Signal Tracker"

DEFAULTS = {
    "enabled": False,
    "times": ["07:00"],          # 24h local time
    "days": "everyday",          # everyday | weekdays
    "stages": ["collect", "replies", "suggest", "links", "amplify",
               "threads", "dedup", "judge", "extract", "digest"],
    "collect_scrolls": 5,
    "judge_limit": 60,
}

PRESETS = {
    "once":    {"times": ["07:00"], "label": "Once a day, 07:00"},
    "twice":   {"times": ["07:00", "19:00"], "label": "Morning and evening"},
    "thrice":  {"times": ["07:00", "13:00", "19:00"], "label": "Three times a day"},
    "hourly6": {"times": [f"{h:02d}:00" for h in range(7, 23, 3)],
                "label": "Every 3 hours, 07:00–22:00"},
}

# Stages the user can turn off, with why they might want to.
STAGE_INFO = [
    ("collect",  "Fetch new posts", "The only stage that cannot be caught up later."),
    ("replies",  "Mine conversations", "Finds people who reply under tracked posts."),
    ("suggest",  "Harvest follow graph", "Discovers accounts. Heaviest browsing."),
    ("links",    "Resolve links", "Fetches titles and abstracts behind URLs."),
    ("amplify",  "Count amplification", "How many tracked accounts shared each post."),
    ("threads",  "Stitch threads", "Joins self-threads into one item."),
    ("dedup",    "Find duplicates", "Local only. No API cost."),
    ("judge",    "Score posts", "Costs API credit. Roughly $0.0025 per post."),
    ("extract",  "Pull out findings", "Costs API credit. Surfaced posts only."),
    ("digest",   "Write the digest", "Renders the Markdown file."),
]


def _table(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS settings ("
                 "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)")
    conn.commit()


def load(conn) -> dict:
    _table(conn)
    row = conn.execute("SELECT value FROM settings WHERE key='schedule'").fetchone()
    settings = dict(DEFAULTS)
    if row:
        try:
            settings.update(json.loads(row["value"]))
        except json.JSONDecodeError:
            pass
    return settings


def save(conn, settings: dict) -> dict:
    _table(conn)
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in settings.items() if k in DEFAULTS})
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ('schedule',?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
        "updated_at=excluded.updated_at",
        (json.dumps(merged), db.now()))
    conn.commit()
    return merged


def command_for(settings: dict) -> str:
    """The command a scheduler should run."""
    skipped = [s for s, *_ in STAGE_INFO if s not in settings.get("stages", [])]
    parts = [sys.executable, "-m", "tracker", "daily"]
    if skipped:
        parts += ["--skip", *skipped]
    return subprocess.list2cmdline(parts) if WINDOWS else " ".join(parts)


# -- cron (Linux, macOS) ----------------------------------------------------

def _crontab_read() -> str:
    try:
        out = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        return out.stdout if out.returncode == 0 else ""
    except FileNotFoundError:
        return ""


def _crontab_write(text: str) -> None:
    subprocess.run(["crontab", "-"], input=text, text=True, check=True)


def _cron_lines(settings: dict) -> list[str]:
    days = "1-5" if settings.get("days") == "weekdays" else "*"
    command = command_for(settings)
    log = ROOT / "logs" / "daily.log"
    lines = []
    for slot in settings.get("times", []):
        hour, _, minute = slot.partition(":")
        lines.append(f"{int(minute)} {int(hour)} * * {days} "
                     f"cd {ROOT} && {command} >> {log} 2>&1  {MARKER}")
    return lines


def install(conn, settings: dict) -> dict:
    """Write the schedule to the OS. Removing ours never touches other entries."""
    settings = save(conn, settings)
    (ROOT / "logs").mkdir(exist_ok=True)

    if WINDOWS:
        return _install_windows(settings)

    kept = [ln for ln in _crontab_read().splitlines() if MARKER not in ln]
    if settings.get("enabled"):
        kept += _cron_lines(settings)
    body = "\n".join(ln for ln in kept if ln.strip()) + "\n"
    try:
        _crontab_write(body)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}
    return {"ok": True, "entries": len(_cron_lines(settings)) if settings["enabled"] else 0}


def _install_windows(settings: dict) -> dict:
    # One task per time slot; schtasks has no multi-time daily trigger.
    subprocess.run(["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
                   capture_output=True)
    for i, slot in enumerate(settings.get("times", [])):
        subprocess.run(["schtasks", "/delete", "/tn", f"{TASK_NAME} {i + 1}", "/f"],
                       capture_output=True)
    if not settings.get("enabled"):
        return {"ok": True, "entries": 0}

    schedule = "WEEKLY" if settings.get("days") == "weekdays" else "DAILY"
    made = 0
    for i, slot in enumerate(settings.get("times", [])):
        args = ["schtasks", "/create", "/tn", f"{TASK_NAME} {i + 1}",
                "/tr", f'cmd /c cd /d "{ROOT}" && {command_for(settings)}',
                "/sc", schedule, "/st", slot, "/f"]
        if schedule == "WEEKLY":
            args += ["/d", "MON,TUE,WED,THU,FRI"]
        result = subprocess.run(args, capture_output=True, text=True)
        if result.returncode != 0:
            return {"ok": False, "error": (result.stderr or result.stdout)[:200]}
        made += 1
    return {"ok": True, "entries": made}


def status(conn) -> dict:
    settings = load(conn)
    installed = 0
    if WINDOWS:
        out = subprocess.run(["schtasks", "/query", "/fo", "csv"],
                             capture_output=True, text=True)
        installed = out.stdout.count(TASK_NAME) if out.returncode == 0 else 0
    else:
        installed = sum(1 for ln in _crontab_read().splitlines() if MARKER in ln)

    last = conn.execute(
        "SELECT started_at, status FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    return {
        "settings": settings,
        "installed": installed,
        "platform": "windows" if WINDOWS else platform.system().lower(),
        "command": command_for(settings),
        "presets": PRESETS,
        "stage_info": [{"key": k, "label": l, "note": n} for k, l, n in STAGE_INFO],
        "last_run": dict(last) if last else None,
    }
