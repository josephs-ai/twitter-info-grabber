"""Mine conversations under tracked accounts' posts.

Two things fall out of one operation, which is why this is worth building:

1. DISCOVERY — the people who consistently write the sharp correction under a
   well-known researcher's post are exactly the frontline practitioners you
   can't name in advance. Follow-graph presence is a one-off endorsement that
   may be years stale; showing up in replies means being in the conversation
   right now.

2. CONTENT — a good reply is frequently more informative than the post that
   provoked it. Those replies land in `posts` like anything else and flow
   through the same triage, novelty, and judging stages.
"""

from __future__ import annotations

import json
import sys
import time

from playwright.sync_api import sync_playwright

from . import accounts as accounts_mod, db, parse
from .collect import PROFILE_DIR, goto_with_retry, log

DETAIL_OP = "TweetDetail"


def pick_posts(conn, limit: int, min_age_hours: int = 2) -> list[dict]:
    """Choose which posts to mine.

    Two constraints matter. Only posts by accounts we actually track — a reply
    thread under an embedded retweet original tells us nothing about our seeds.
    And nothing too fresh: replies need time to accumulate, so a post from five
    minutes ago has an empty thread.
    """
    rows = conn.execute(
        """
        SELECT p.id, p.author_handle, p.text
        FROM posts p
        JOIN accounts a ON a.handle = p.author_handle COLLATE NOCASE
        WHERE p.capture_source = 'timeline'
          AND p.is_retweet = 0
          AND a.active = 1
          AND p.created_at < datetime('now', ?)
          AND p.id NOT IN (SELECT DISTINCT reply_to_id FROM posts
                           WHERE reply_to_id IS NOT NULL)
        ORDER BY p.created_at DESC
        LIMIT ?
        """,
        (f"-{min_age_hours} hours", limit),
    ).fetchall()
    return [dict(r) for r in rows]


def record_repliers(conn, seed: str, handles: list[str]) -> int:
    """Credit each distinct replier once per seed."""
    tracked = {h.lower() for h in accounts_mod.active_handles(conn)}
    new = 0
    for handle in {h for h in handles if h.lower() not in tracked}:
        row = conn.execute(
            "SELECT replied_under, reply_count FROM candidates WHERE handle=?",
            (handle,)).fetchone()
        if row:
            unders = set(json.loads(row["replied_under"] or "[]"))
            unders.add(seed)
            conn.execute(
                "UPDATE candidates SET reply_count=?, replied_under=? WHERE handle=?",
                (len(unders), json.dumps(sorted(unders)), handle))
        else:
            conn.execute(
                "INSERT INTO candidates (handle, seed_count, followed_by, discovered_at, "
                "reply_count, replied_under) VALUES (?,0,'[]',?,1,?)",
                (handle, db.now(), json.dumps([seed])))
            new += 1
    conn.commit()
    return new


def mine(conn, limit: int = 5, scrolls: int = 3, headless: bool = True) -> int:
    targets = pick_posts(conn, limit)
    if not targets:
        log("No posts to mine. Run ./run collect first.")
        return 0

    log(f"Mining {len(targets)} conversation(s)")
    total_replies = total_new = 0

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR), headless=headless,
            viewport={"width": 1280, "height": 900})
        page = context.pages[0] if context.pages else context.new_page()

        bucket: list[dict] = []

        def on_response(response):
            if DETAIL_OP not in response.url or "/i/api/graphql/" not in response.url:
                return
            try:
                body = response.json()
            except Exception:
                return
            bucket.extend(parse.posts_from_response(body))

        page.on("response", on_response)

        for target in targets:
            bucket.clear()
            url = f"https://x.com/{target['author_handle']}/status/{target['id']}"
            snippet = " ".join(target["text"].split())[:60]
            log(f"\n@{target['author_handle']}: {snippet}...")
            try:
                goto_with_retry(page, url, attempts=2)
                page.wait_for_timeout(4000)
            except Exception as exc:
                log(f"  skipped: {str(exc).splitlines()[0][:60]}")
                continue

            for _ in range(scrolls):
                page.mouse.wheel(0, 2400)
                time.sleep(1.6)

            # Everything in the thread that isn't the root post is a reply.
            found = {p["id"]: p for p in bucket if p["id"] != target["id"]}
            for post in found.values():
                post["capture_source"] = "reply"

            seen, inserted = db.upsert_posts(conn, list(found.values()))
            repliers = [p["author_handle"] for p in found.values()
                        if p["author_handle"] != target["author_handle"]]
            new = record_repliers(conn, target["author_handle"], repliers)

            total_replies += inserted
            total_new += new
            log(f"  {seen} in thread, {inserted} new posts, "
                f"{len(set(repliers))} repliers, {new} new candidates")
            time.sleep(2.0)

        context.close()

    log(f"\n{total_replies} reply posts stored, {total_new} new candidates")
    return 0
