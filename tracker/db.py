"""SQLite layer. One file, WAL mode, no server."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "tracker.db"
SCHEMA = ROOT / "schema.sql"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA.read_text())
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Add columns missing from databases created by an older schema.

    CREATE TABLE IF NOT EXISTS silently does nothing when the table already
    exists, so new columns never reach an existing database without this.
    SQLite has no ADD COLUMN IF NOT EXISTS, hence the explicit check.
    """
    wanted = {
        "accounts": {
            "category": "TEXT",
            "note": "TEXT",
            "harvested_at": "TEXT",
        },
        "posts": {
            "retweet_of_id": "TEXT",
            "amplifiers": "INTEGER NOT NULL DEFAULT 0",
            "thread_root_id": "TEXT",
            "thread_size": "INTEGER",
        },
        "candidates": {
            "reply_count": "INTEGER NOT NULL DEFAULT 0",
            "replied_under": "TEXT",
        },
    }
    for table, columns in wanted.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, coltype in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
    conn.commit()


def upsert_posts(conn: sqlite3.Connection, posts: list[dict], raw_by_id: dict | None = None) -> tuple[int, int]:
    """Insert posts we haven't seen. Returns (seen, newly_inserted).

    Idempotent by design: re-running collection over the same timeline inserts
    nothing and is always safe. First-seen values win — we never overwrite an
    existing row, so fetched_at stays honest.
    """
    raw_by_id = raw_by_id or {}
    inserted = 0
    fetched = now()

    for post in posts:
        raw = raw_by_id.get(post["id"])
        cur = conn.execute(
            """
            INSERT INTO posts (
                id, author_handle, author_name, text, created_at, fetched_at,
                conversation_id, reply_to_id, is_retweet, quoted_id, urls,
                capture_source, raw_json, retweet_of_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                post["id"], post["author_handle"], post["author_name"], post["text"],
                post["created_at"], fetched, post["conversation_id"], post["reply_to_id"],
                int(post["is_retweet"]), post["quoted_id"], json.dumps(post["urls"]),
                post["capture_source"], json.dumps(raw) if raw else None,
                post.get("retweet_of_id"),
            ),
        )
        for item in post.get("media") or []:
            conn.execute(
                "INSERT INTO media (id, post_id, url, kind, alt_text) VALUES (?,?,?,?,?) "
                "ON CONFLICT(id) DO NOTHING",
                (item["id"], post["id"], item["url"], item.get("kind"),
                 item.get("alt_text")))

        if cur.rowcount:
            inserted += 1
            conn.execute(
                "INSERT INTO triage (post_id, stage, updated_at) VALUES (?, 'collected', ?) "
                "ON CONFLICT(post_id) DO NOTHING",
                (post["id"], fetched),
            )

    conn.commit()
    return len(posts), inserted


def known_ids(conn: sqlite3.Connection, ids: list[str]) -> set[str]:
    if not ids:
        return set()
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT id FROM posts WHERE id IN ({placeholders})", ids)
    return {row["id"] for row in rows}


def start_run(conn: sqlite3.Connection, url: str) -> int:
    cur = conn.execute("INSERT INTO runs (started_at, url) VALUES (?,?)", (now(), url))
    conn.commit()
    return cur.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, **fields) -> None:
    fields["finished_at"] = now()
    assignments = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE runs SET {assignments} WHERE id=?", (*fields.values(), run_id))
    conn.commit()


def stats(conn: sqlite3.Connection) -> dict:
    def scalar(sql: str, *args):
        row = conn.execute(sql, args).fetchone()
        return row[0] if row else 0

    return {
        "posts_total": scalar("SELECT COUNT(*) FROM posts"),
        "posts_timeline": scalar("SELECT COUNT(*) FROM posts WHERE capture_source='timeline'"),
        "posts_embedded": scalar("SELECT COUNT(*) FROM posts WHERE capture_source='embedded'"),
        "authors": scalar("SELECT COUNT(DISTINCT author_handle) FROM posts"),
        "oldest": scalar("SELECT MIN(created_at) FROM posts"),
        "newest": scalar("SELECT MAX(created_at) FROM posts"),
        "runs": scalar("SELECT COUNT(*) FROM runs"),
        "last_run": scalar("SELECT status FROM runs ORDER BY id DESC LIMIT 1"),
    }
