"""Tracked accounts and discovered candidates."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import paths
from .db import now

ROOT = Path(__file__).resolve().parent.parent
SEEDS_FILE = paths.code_dir() / "seeds.txt"


def parse_seeds(path: Path = SEEDS_FILE) -> list[dict]:
    """Read seeds.txt: `handle,category,note` per line, # for comments."""
    entries = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",", 2)]
        entries.append({
            "handle": parts[0].lstrip("@"),
            "category": parts[1] if len(parts) > 1 else None,
            "note": parts[2] if len(parts) > 2 else None,
        })
    return entries


def import_seeds(conn: sqlite3.Connection, path: Path = SEEDS_FILE) -> tuple[int, int]:
    entries = parse_seeds(path)
    added = 0
    for entry in entries:
        cur = conn.execute(
            "INSERT INTO accounts (handle, added_at, category, note, active) "
            "VALUES (?,?,?,?,1) ON CONFLICT(handle) DO NOTHING",
            (entry["handle"], now(), entry["category"], entry["note"]),
        )
        added += cur.rowcount
    conn.commit()
    return len(entries), added


def active_handles(conn: sqlite3.Connection) -> list[str]:
    return [r["handle"] for r in conn.execute(
        "SELECT handle FROM accounts WHERE active=1 ORDER BY handle")]


def unharvested(conn: sqlite3.Connection, limit: int) -> list[str]:
    return [r["handle"] for r in conn.execute(
        "SELECT handle FROM accounts WHERE active=1 AND harvested_at IS NULL "
        "ORDER BY handle LIMIT ?", (limit,))]


def mark_harvested(conn: sqlite3.Connection, handle: str) -> None:
    conn.execute("UPDATE accounts SET harvested_at=? WHERE handle=?", (now(), handle))
    conn.commit()


def record_candidates(conn: sqlite3.Connection, seed: str, users: list[dict]) -> int:
    """Merge one seed's following list into the candidate pool.

    A candidate's score is how many *distinct* seeds follow them, so the same
    seed being harvested twice must not inflate anything.
    """
    tracked = set(active_handles(conn))
    new = 0
    for user in users:
        handle = user["handle"]
        if handle in tracked:
            continue  # already tracking them
        row = conn.execute(
            "SELECT followed_by FROM candidates WHERE handle=?", (handle,)).fetchone()
        if row:
            followers = set(json.loads(row["followed_by"] or "[]"))
            if seed in followers:
                continue
            followers.add(seed)
            conn.execute(
                "UPDATE candidates SET seed_count=?, followed_by=? WHERE handle=?",
                (len(followers), json.dumps(sorted(followers)), handle),
            )
        else:
            conn.execute(
                "INSERT INTO candidates (handle, name, bio, seed_count, followed_by, "
                "discovered_at) VALUES (?,?,?,?,?,?)",
                (handle, user.get("name"), user.get("bio"), 1,
                 json.dumps([seed]), now()),
            )
            new += 1
    conn.commit()
    return new


def top_candidates(conn: sqlite3.Connection, min_seeds: int = 2, limit: int = 60) -> list:
    return conn.execute(
        "SELECT * FROM candidates WHERE seed_count >= ? AND status='new' "
        "ORDER BY seed_count DESC, handle LIMIT ?", (min_seeds, limit)).fetchall()


def approve(conn: sqlite3.Connection, handles: list[str]) -> int:
    added = 0
    for handle in handles:
        row = conn.execute(
            "SELECT name FROM candidates WHERE handle=?", (handle,)).fetchone()
        cur = conn.execute(
            "INSERT INTO accounts (handle, added_at, category, note, active) "
            "VALUES (?,?, 'discovered', ?, 1) ON CONFLICT(handle) DO NOTHING",
            (handle, now(), row["name"] if row else None),
        )
        added += cur.rowcount
        conn.execute("UPDATE candidates SET status='approved' WHERE handle=?", (handle,))
    conn.commit()
    return added
