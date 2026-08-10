"""Let the tracked-account list improve itself.

Discovery already finds candidates and judging already scores every post, so
the system has the evidence to manage its own roster — it just never used it.
Two halves:

PROMOTE — a candidate followed by several tracked accounts, or repeatedly
replying under their posts, is more likely to be worth reading than a name
picked from memory. Above a threshold, track them.

DEMOTE — an account whose posts have been judged many times and never scored
well is costing collection time and adding noise. Deactivate it.

Demotion is the half that matters and the half people forget. A list that only
grows drifts toward noise: every marginal account dilutes the corpus, and the
dedup index has more near-misses to wade through.

Three rules keep this from running away:

  Evidence minimums — never act on one data point. An account needs a real
  judged history before it can be dropped.
  Caps per run — a bad threshold should cost you three accounts, not fifty.
  Everything reversible — auto changes are labelled and logged, so `curate
  --undo` puts the roster back.
"""

from __future__ import annotations

import json

from . import db

DEFAULTS = {
    "auto_promote": False,
    "promote_min_follows": 4,    # distinct tracked accounts following them
    "promote_min_replies": 3,    # distinct tracked posts they replied under
    "promote_max_per_run": 3,

    "auto_demote": False,
    "demote_min_judged": 15,     # never judge an account on a thin record
    "demote_max_mean_value": 1.6,
    "demote_max_per_run": 2,
    "demote_grace_days": 21,     # a newly added account gets time to prove itself
}


def load(conn) -> dict:
    conn.execute("CREATE TABLE IF NOT EXISTS settings ("
                 "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS curation_log ("
                 "id INTEGER PRIMARY KEY, handle TEXT NOT NULL, action TEXT NOT NULL,"
                 "reason TEXT, evidence TEXT, created_at TEXT NOT NULL, undone INTEGER DEFAULT 0)")
    conn.commit()
    row = conn.execute("SELECT value FROM settings WHERE key='curate'").fetchone()
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
        "INSERT INTO settings (key, value, updated_at) VALUES ('curate',?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (json.dumps(merged), db.now()))
    conn.commit()
    return merged


def _log(conn, handle: str, action: str, reason: str, evidence: dict) -> None:
    conn.execute(
        "INSERT INTO curation_log (handle, action, reason, evidence, created_at) "
        "VALUES (?,?,?,?,?)",
        (handle, action, reason, json.dumps(evidence), db.now()))


def promote_candidates(conn, settings: dict, dry_run: bool = True) -> list[dict]:
    rows = conn.execute(
        "SELECT handle, name, bio, seed_count, reply_count FROM candidates "
        "WHERE status='new' AND (seed_count >= ? OR reply_count >= ?) "
        "ORDER BY (seed_count + reply_count) DESC LIMIT ?",
        (settings["promote_min_follows"], settings["promote_min_replies"],
         settings["promote_max_per_run"])).fetchall()

    actions = []
    for row in rows:
        why = []
        if row["seed_count"] >= settings["promote_min_follows"]:
            why.append(f"followed by {row['seed_count']} tracked accounts")
        if row["reply_count"] >= settings["promote_min_replies"]:
            why.append(f"replied under {row['reply_count']} tracked posts")
        reason = " and ".join(why)
        actions.append({"handle": row["handle"], "name": row["name"],
                        "action": "promote", "reason": reason,
                        "evidence": {"follows": row["seed_count"],
                                     "replies": row["reply_count"]}})
        if dry_run:
            continue
        conn.execute(
            "INSERT INTO accounts (handle, added_at, category, note, active) "
            "VALUES (?,?, 'auto', ?, 1) ON CONFLICT(handle) DO UPDATE SET active=1",
            (row["handle"], db.now(), f"auto: {reason}"))
        conn.execute("UPDATE candidates SET status='approved' WHERE handle=?",
                     (row["handle"],))
        _log(conn, row["handle"], "promote", reason, actions[-1]["evidence"])

    if not dry_run:
        conn.commit()
    return actions


def demote_accounts(conn, settings: dict, dry_run: bool = True) -> list[dict]:
    """Find tracked accounts with a long, consistently poor judged record."""
    rows = conn.execute(
        """
        SELECT a.handle, a.added_at,
               COUNT(j.id) judged,
               AVG(j.value) mean_value,
               SUM(CASE WHEN j.verdict='surface' THEN 1 ELSE 0 END) surfaced
        FROM accounts a
        JOIN posts p ON p.author_handle = a.handle COLLATE NOCASE
        JOIN judgements j ON j.post_id = p.id
        WHERE a.active = 1
          AND a.added_at < datetime('now', ?)
        GROUP BY a.handle
        HAVING judged >= ? AND surfaced = 0 AND mean_value <= ?
        ORDER BY mean_value ASC, judged DESC
        LIMIT ?
        """,
        (f"-{settings['demote_grace_days']} days", settings["demote_min_judged"],
         settings["demote_max_mean_value"], settings["demote_max_per_run"])).fetchall()

    actions = []
    for row in rows:
        reason = (f"{row['judged']} posts judged, none surfaced, "
                  f"mean value {row['mean_value']:.1f}")
        actions.append({"handle": row["handle"], "action": "demote",
                        "reason": reason,
                        "evidence": {"judged": row["judged"],
                                     "mean_value": round(row["mean_value"], 2),
                                     "surfaced": row["surfaced"]}})
        if dry_run:
            continue
        conn.execute("UPDATE accounts SET active=0 WHERE handle=?", (row["handle"],))
        _log(conn, row["handle"], "demote", reason, actions[-1]["evidence"])

    if not dry_run:
        conn.commit()
    return actions


def run(conn, dry_run: bool = True, force: bool = False) -> dict:
    settings = load(conn)
    promoted = demoted = []
    if force or settings["auto_promote"]:
        promoted = promote_candidates(conn, settings, dry_run)
    if force or settings["auto_demote"]:
        demoted = demote_accounts(conn, settings, dry_run)
    return {"promoted": promoted, "demoted": demoted,
            "settings": settings, "dry_run": dry_run}


def undo(conn, limit: int = 20) -> int:
    """Reverse recent automatic changes."""
    load(conn)
    rows = conn.execute(
        "SELECT id, handle, action FROM curation_log WHERE undone=0 "
        "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    for row in rows:
        if row["action"] == "promote":
            conn.execute("UPDATE accounts SET active=0 WHERE handle=?", (row["handle"],))
            conn.execute("UPDATE candidates SET status='new' WHERE handle=?", (row["handle"],))
        else:
            conn.execute("UPDATE accounts SET active=1 WHERE handle=?", (row["handle"],))
        conn.execute("UPDATE curation_log SET undone=1 WHERE id=?", (row["id"],))
    conn.commit()
    return len(rows)


def history(conn, limit: int = 30) -> list[dict]:
    load(conn)
    rows = conn.execute(
        "SELECT handle, action, reason, created_at, undone FROM curation_log "
        "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
