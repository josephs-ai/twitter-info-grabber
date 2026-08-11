"""Reach the user, instead of waiting to be opened.

The tool's whole output was a markdown file on disk and a window you had to
remember to check. Tools like that get used for a week. The point of a filter
that runs on its own schedule is that it can *tell you* when something cleared
the bar — otherwise the scheduling is pointless and you are back to checking a
feed, which is the habit this was supposed to replace.

Two channels, both optional:

  desktop  — a native notification from the scheduled run, using whatever the
             platform already has. No dependency, no daemon.
  webhook  — one HTTP POST with the findings. The body is Slack- and
             Discord-compatible (both accept a bare {"content"/"text"} field),
             which covers most of what people actually want without writing a
             separate integration for each.

A watermark makes delivery exactly-once per post: without it, every run would
re-announce the same three posts, and a notifier that repeats itself gets muted
within a day.
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import urllib.error
import urllib.request

from . import cluster, db, strictness

DEFAULTS = {
    "desktop": True,
    "webhook_url": "",
    "min_items": 1,       # stay silent unless at least this many cleared the bar
    "max_items": 5,       # how many to name in the message
}
KEY = "notify"
WATERMARK = "notify_watermark"
TIMEOUT = 10


def load(conn) -> dict:
    conn.execute("CREATE TABLE IF NOT EXISTS settings ("
                 "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)")
    conn.commit()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (KEY,)).fetchone()
    settings = dict(DEFAULTS)
    if row:
        try:
            settings.update(json.loads(row["value"]))
        except json.JSONDecodeError:
            pass
    return settings


def save(conn, settings: dict) -> dict:
    load(conn)
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in settings.items() if k in DEFAULTS})
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (KEY, json.dumps(merged), db.now()))
    conn.commit()
    return merged


def _watermark(conn) -> int:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (WATERMARK,)).fetchone()
    return int(row["value"]) if row else 0


def _set_watermark(conn, value: int) -> None:
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (WATERMARK, str(value), db.now()))
    conn.commit()


def undelivered(conn, limit: int = 20) -> list[dict]:
    """Posts that cleared the current bar and have not been announced.

    Keyed on the judgement row id, not on time: a post judged during a run that
    failed halfway still gets announced on the next one.

    Deliberately one-directional — loosening the strictness bar does not
    re-announce everything it newly admits. Moving a slider should not fire
    three hundred notifications; those posts are waiting in the app.
    """
    load(conn)
    where, bar = strictness.clause(strictness.load(conn))
    rows = conn.execute(
        f"""
        SELECT j.id jid, p.id, p.author_handle, p.text, p.created_at,
               j.value, j.novelty, e.headline, e.so_what
        FROM judgements j
        JOIN posts p ON p.id = j.post_id
        LEFT JOIN extractions e ON e.post_id = p.id
        WHERE {where} AND j.id > ?
        GROUP BY p.id
        ORDER BY j.value DESC, j.id DESC
        LIMIT ?
        """, (*bar, _watermark(conn), limit)).fetchall()
    return [dict(r) for r in rows]


def _headline(item: dict, width: int = 140) -> str:
    text = item.get("headline") or " ".join((item["text"] or "").split())
    if len(text) <= width:
        return text
    return text[:width].rsplit(" ", 1)[0] + "…"


def compose(items: list[dict], max_items: int) -> tuple[str, str]:
    """(title, body) — the same words for every channel.

    Clustered first. A notification that says "8 new findings" and then lists
    the same model release six times is worse than no notification: it reads as
    broken, and it is the fastest way to get muted.
    """
    groups = cluster.group(items)
    n = len(groups)
    title = f"{n} new finding{'s' if n != 1 else ''}"
    lines = []
    for group in groups[:max_items]:
        lead = group["lead"]
        line = (f"• {_headline(lead)}\n  @{lead['author_handle']} "
                f"· x.com/{lead['author_handle']}/status/{lead['id']}")
        if group["size"] > 1:
            others = ", ".join("@" + s for s in group["sources"][1:4])
            more = group["size"] - 1
            line += f"\n  also from {others}" + (" and others" if more > 3 else "")
        lines.append(line)
    if n > max_items:
        lines.append(f"…and {n - max_items} more")
    return title, "\n".join(lines)


def desktop(title: str, body: str) -> bool:
    """Native notification, best-effort. Never raise into a scheduled run."""
    system = platform.system()
    try:
        if system == "Linux" and shutil.which("notify-send"):
            subprocess.run(["notify-send", "-a", "AI Signal", title, body],
                           check=True, timeout=TIMEOUT)
            return True
        if system == "Darwin" and shutil.which("osascript"):
            script = (f'display notification {json.dumps(body[:400])} '
                      f'with title "AI Signal" subtitle {json.dumps(title)}')
            subprocess.run(["osascript", "-e", script], check=True, timeout=TIMEOUT)
            return True
        if system == "Windows":
            ps = (
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications,"
                " ContentType=WindowsRuntime] > $null;"
                "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(2);"
                f"$t.GetElementsByTagName('text')[0].AppendChild($t.CreateTextNode({_ps(title)}))>$null;"
                f"$t.GetElementsByTagName('text')[1].AppendChild($t.CreateTextNode({_ps(body[:300])}))>$null;"
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
                "'AI Signal').Show([Windows.UI.Notifications.ToastNotification]::new($t))"
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           check=True, timeout=TIMEOUT)
            return True
    except (subprocess.SubprocessError, OSError):
        return False
    return False


def _ps(text: str) -> str:
    """Single-quoted PowerShell literal."""
    return "'" + text.replace("'", "''") + "'"


def webhook(url: str, title: str, body: str) -> bool:
    if not url.startswith(("http://", "https://")):
        return False
    text = f"**{title}**\n{body}"
    # Slack reads `text`, Discord reads `content`. Sending both means one
    # payload works for either without asking which service this is.
    payload = json.dumps({"text": text, "content": text}).encode()
    request = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError):
        return False


def deliver(conn, dry_run: bool = False) -> dict:
    settings = load(conn)
    items = undelivered(conn, limit=50)
    result = {"found": len(items), "sent": [], "skipped": None}

    if len(items) < settings["min_items"]:
        result["skipped"] = "below min_items"
        return result

    title, body = compose(items, settings["max_items"])
    result["title"], result["body"] = title, body
    if dry_run:
        result["skipped"] = "dry run"
        return result

    if settings["desktop"] and desktop(title, body):
        result["sent"].append("desktop")
    if settings["webhook_url"] and webhook(settings["webhook_url"], title, body):
        result["sent"].append("webhook")

    # Advance regardless of channel success: a failing webhook should not turn
    # into an ever-growing announcement that replays weeks of findings when it
    # comes back.
    _set_watermark(conn, max(i["jid"] for i in items))
    return result
