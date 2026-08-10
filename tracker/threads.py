"""Stitch self-threads into single logical posts.

A researcher explaining something across twelve posts has written ONE idea, not
twelve. Left alone that produces twelve near-identical digest entries, twelve
judge calls, and twelve chances for the dedup index to match a thread against
its own continuation.

A self-thread here means: several posts sharing a conversation_id, all by the
same author, where each non-root post replies within the conversation. Replies
from *other* people are a discussion, not a thread, and are left alone — they
are exactly the reply-graph content worth keeping separate.

The root post carries the stitched text. Continuations are marked dropped with
reason 'thread_part' so they never reach the judge, but they stay in the
database and stay queryable.
"""

from __future__ import annotations

from . import db

MIN_THREAD = 2


def find_threads(conn) -> dict[str, list[dict]]:
    """Group timeline posts into self-threads, keyed by root post id."""
    rows = conn.execute(
        """
        SELECT id, author_handle, conversation_id, reply_to_id, created_at, text
        FROM posts
        WHERE conversation_id IS NOT NULL
          AND capture_source = 'timeline'
          AND is_retweet = 0
        ORDER BY conversation_id, created_at
        """
    ).fetchall()

    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["conversation_id"], []).append(dict(row))

    threads = {}
    for conversation_id, members in grouped.items():
        if len(members) < MIN_THREAD:
            continue
        # Single author only — a mixed conversation is a discussion.
        authors = {m["author_handle"].lower() for m in members}
        if len(authors) != 1:
            continue
        members.sort(key=lambda m: (m["created_at"], m["id"]))
        threads[members[0]["id"]] = members
    return threads


def stitched_text(members: list[dict]) -> str:
    """Join a thread in order, dropping the numbering people add by hand."""
    import re

    parts = []
    for member in members:
        text = member["text"].strip()
        # Strip leading "1/", "2/7", "(3/5)" style counters — they are artifacts
        # of the medium, not content, and they hurt similarity matching.
        text = re.sub(r"^\(?\d{1,2}\s*/\s*\d{0,2}\)?[.:) ]*", "", text).strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def apply(conn, dry_run: bool = False) -> dict:
    threads = find_threads(conn)
    stats = {"threads": 0, "posts_folded": 0, "examples": []}

    for root_id, members in threads.items():
        stats["threads"] += 1
        stats["posts_folded"] += len(members) - 1
        if len(stats["examples"]) < 5:
            stats["examples"].append({
                "root": root_id,
                "author": members[0]["author_handle"],
                "size": len(members),
                "preview": " ".join(stitched_text(members).split())[:110],
            })

        if dry_run:
            continue

        conn.execute("UPDATE posts SET thread_root_id=?, thread_size=? WHERE id=?",
                     (root_id, len(members), root_id))
        for member in members[1:]:
            conn.execute("UPDATE posts SET thread_root_id=? WHERE id=?",
                         (root_id, member["id"]))
            # Fold continuations out of the pipeline, but only if they haven't
            # already been judged — never rewrite a decision already made.
            conn.execute(
                "UPDATE triage SET stage='dropped', drop_reason='thread_part', "
                "updated_at=? WHERE post_id=? AND stage IN ('collected','triaged')",
                (db.now(), member["id"]))

    if not dry_run:
        conn.commit()
    return stats


def text_for(conn, post_id: str, fallback: str) -> str:
    """The text that represents a post downstream.

    For a thread root that is the whole stitched thread; for anything else it is
    the post's own text. Embedding and judging both go through this so a thread
    is compared and scored as the single idea it actually is.
    """
    row = conn.execute(
        "SELECT thread_size, conversation_id FROM posts WHERE id=?", (post_id,)).fetchone()
    if not row or not row["thread_size"]:
        return fallback

    members = conn.execute(
        "SELECT id, text, created_at FROM posts "
        "WHERE thread_root_id=? ORDER BY created_at, id", (post_id,)).fetchall()
    if not members:
        return fallback
    return stitched_text([dict(m) for m in members])
