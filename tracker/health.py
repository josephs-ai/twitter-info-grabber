"""Is the thing actually working?

Every failure this system has in practice is silent. The session expires and
collection returns empty pages that look like a quiet day. A scheduled run dies
and nothing says so until you notice the digest stopped changing. The judge
falls behind and the funnel just... narrows.

So the checks live here rather than inside the `doctor` command, and both the
CLI and the app read the same answers. A status the app never shows is a status
nobody sees.

Each check is (level, label, detail, fix): level is ok|warn|fail, and `fix` is
the command that resolves it, or None when there is nothing to do.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Two days of nothing new is not a quiet week — collection is broken.
STALE_HOURS = 48


def _age_hours(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        stamp = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamp).total_seconds() / 3600


def _check(level, label, detail, fix=None) -> dict:
    return {"level": level, "label": label, "detail": detail, "fix": fix}


def session(conn) -> dict:
    from . import collect as collect_mod

    profile = collect_mod.PROFILE_DIR
    if not (profile.exists() and any(profile.iterdir())):
        return _check("fail", "Session", "not signed in", "./run login")

    # A live profile that collects nothing is the shape an expired session
    # takes: the pages load, they just come back empty.
    row = conn.execute(
        "SELECT started_at, posts_seen FROM runs "
        "WHERE status='ok' AND posts_seen > 0 ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return _check("warn", "Session", "signed in, nothing collected yet",
                      "./run collect --all")
    age = _age_hours(row["started_at"])
    if age is not None and age > STALE_HOURS:
        return _check("fail", "Session",
                      f"nothing collected in {age / 24:.0f} days — likely expired",
                      "./run login")
    return _check("ok", "Session", "signed in" + (f", collecting {_ago(age)}" if age else ""))


def _ago(hours: float | None) -> str:
    if hours is None:
        return ""
    if hours < 1:
        return "just now"
    if hours < 24:
        return f"{hours:.0f}h ago"
    return f"{hours / 24:.0f}d ago"


def last_run(conn) -> dict:
    row = conn.execute(
        "SELECT status, started_at, posts_new, error FROM runs "
        "ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return _check("warn", "Last run", "never", "./run daily")
    age = _ago(_age_hours(row["started_at"]))
    if row["status"] != "ok":
        detail = (row["error"] or row["status"])[:90]
        return _check("fail", "Last run", f"{detail} ({age})", "./run doctor")
    return _check("ok", "Last run", f"+{row['posts_new']} new, {age}")


def api_key(conn=None) -> dict:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return _check("ok", "API key", "set")
    env = ROOT / ".env"
    if env.exists() and "ANTHROPIC_API_KEY" in env.read_text():
        return _check("ok", "API key", "in .env")
    return _check("fail", "API key", "missing — judging and extraction are skipped",
                  "add ANTHROPIC_API_KEY to .env")


def queue(conn) -> dict:
    """The backlog only matters if it is growing faster than it drains."""
    from . import judge as judge_mod

    info = judge_mod.backlog(conn)
    waiting = info["waiting"]
    if not waiting:
        return _check("ok", "Judge queue", "empty")
    oldest = _age_hours(info["oldest"])
    detail = f"{waiting:,} waiting"
    if oldest and oldest > 24:
        detail += f", oldest {oldest / 24:.0f}d"
    # One daily run judges `limit` posts; a queue deeper than a few runs will
    # take weeks to drain even with the backlog slice.
    level = "warn" if waiting > 300 else "ok"
    return _check(level, "Judge queue", detail,
                  "./run judge --limit 200" if level == "warn" else None)


def report(conn) -> list[dict]:
    return [session(conn), last_run(conn), api_key(conn), queue(conn)]


def worst(checks: list[dict]) -> str:
    for level in ("fail", "warn"):
        if any(c["level"] == level for c in checks):
            return level
    return "ok"
