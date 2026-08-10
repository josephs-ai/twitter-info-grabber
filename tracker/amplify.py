"""Corroboration: how many tracked accounts independently shared the same post.

When three people you track all amplify the same paper on the same day, that is
one of the strongest signals the system can produce — and it is invisible to a
per-post pipeline. Worse, the naive treatment is actively harmful: five accounts
retweeting one announcement look like five duplicates, so dedup discards four of
them and the corroboration disappears entirely.

So amplification is counted on the ORIGINAL post rather than being thrown away
with the copies. The retweets stay dropped (nobody wants five identical rows);
what survives is a number on the thing they pointed at.

Independence matters: an author amplifying their own post counts for nothing,
and one account sharing something twice counts once.
"""

from __future__ import annotations

import re

from . import db

RT_PREFIX = re.compile(r"^RT @([A-Za-z0-9_]+):\s*(.*)", re.S)


def backfill_retweet_targets(conn) -> int:
    """Link old retweets to their originals.

    Posts collected before the parser recorded retweet_of_id still carry the
    "RT @author: text" convention, which is enough to find the original we
    already stored as an embedded post.
    """
    rows = conn.execute(
        "SELECT id, text FROM posts WHERE is_retweet=1 AND retweet_of_id IS NULL"
    ).fetchall()
    linked = 0
    for row in rows:
        match = RT_PREFIX.match(row["text"].strip())
        if not match:
            continue
        author, body = match.group(1), match.group(2).strip()
        # X truncates the quoted body with an ellipsis, so match on a prefix.
        prefix = body.rstrip("…").rstrip(". ")[:60]
        if len(prefix) < 15:
            continue
        original = conn.execute(
            "SELECT id FROM posts WHERE author_handle = ? COLLATE NOCASE "
            "AND text LIKE ? ORDER BY created_at LIMIT 1",
            (author, prefix.replace("%", "") + "%"),
        ).fetchone()
        if original:
            conn.execute("UPDATE posts SET retweet_of_id=? WHERE id=?",
                         (original["id"], row["id"]))
            linked += 1
    conn.commit()
    return linked


def recompute(conn) -> dict:
    """Count distinct tracked amplifiers for every post."""
    conn.execute("UPDATE posts SET amplifiers = 0")

    rows = conn.execute(
        """
        SELECT target, COUNT(DISTINCT amplifier) n FROM (
            SELECT retweet_of_id AS target, author_handle AS amplifier
            FROM posts
            WHERE retweet_of_id IS NOT NULL AND capture_source = 'timeline'
            UNION
            SELECT quoted_id AS target, author_handle AS amplifier
            FROM posts
            WHERE quoted_id IS NOT NULL AND capture_source = 'timeline'
        )
        WHERE target IS NOT NULL
        GROUP BY target
        """
    ).fetchall()

    updated = 0
    for row in rows:
        # Self-amplification is not corroboration.
        original = conn.execute(
            "SELECT author_handle FROM posts WHERE id=?", (row["target"],)).fetchone()
        if not original:
            continue
        count = conn.execute(
            """
            SELECT COUNT(DISTINCT author_handle) n FROM (
                SELECT author_handle FROM posts
                WHERE retweet_of_id = ? AND capture_source='timeline'
                UNION
                SELECT author_handle FROM posts
                WHERE quoted_id = ? AND capture_source='timeline'
            ) WHERE author_handle != ? COLLATE NOCASE
            """,
            (row["target"], row["target"], original["author_handle"]),
        ).fetchone()["n"]
        if count:
            conn.execute("UPDATE posts SET amplifiers=? WHERE id=?", (count, row["target"]))
            updated += 1
    conn.commit()

    dist = conn.execute(
        "SELECT amplifiers, COUNT(*) n FROM posts WHERE amplifiers > 0 "
        "GROUP BY amplifiers ORDER BY amplifiers DESC").fetchall()
    return {"posts_with_amplifiers": updated,
            "distribution": {r["amplifiers"]: r["n"] for r in dist}}


def promote(conn) -> int:
    """Give corroborated originals a route back into the pipeline.

    An original that several tracked accounts shared is usually an embedded post
    — nobody we track wrote it, so it would never be judged. Amplification is
    precisely the evidence that it deserves to be.
    """
    rows = conn.execute(
        """
        SELECT p.id FROM posts p
        LEFT JOIN triage t ON t.post_id = p.id
        WHERE p.amplifiers >= 2
          AND (t.post_id IS NULL OR t.stage = 'dropped')
        """
    ).fetchall()
    for row in rows:
        conn.execute(
            "INSERT INTO triage (post_id, stage, updated_at) VALUES (?, 'collected', ?) "
            "ON CONFLICT(post_id) DO UPDATE SET stage='collected', drop_reason=NULL, "
            "updated_at=excluded.updated_at",
            (row["id"], db.now()))
    conn.commit()
    return len(rows)


def top(conn, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        "SELECT id, author_handle, text, amplifiers FROM posts "
        "WHERE amplifiers > 0 ORDER BY amplifiers DESC, created_at DESC LIMIT ?",
        (limit,)).fetchall()
    return [dict(r) for r in rows]
