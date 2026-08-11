"""Where posts come from.

X needed a browser, an interception layer and a burner account. Most of what is
actually worth reading does not: labs publish to RSS, papers land on arXiv with
an Atom API, and Hacker News hands out its entire dataset over unauthenticated
JSON. Those are easier than X in every way that matters — no login, no terms of
service to violate, no session to expire — and the pipeline behind collection
never cared which service a post came from.

A source is anything with a `fetch(conn) -> list[dict]` that returns rows
shaped like the `posts` table. Everything downstream — dedup, judging,
extraction, clustering, digest, notification — reads `posts.text` and is
already platform-agnostic.

Required keys: id, author_handle, text, created_at, platform, url.
Everything else is optional and defaults sensibly.
"""

from __future__ import annotations

from . import arxiv, hackernews, rss

REGISTRY = {
    "rss": rss,
    "hn": hackernews,
    "arxiv": arxiv,
}


def available() -> list[str]:
    return list(REGISTRY)


def fetch(conn, names: list[str] | None = None, limit: int = 60) -> dict:
    """Run each source and store what it returns. Never let one break the rest."""
    from .. import db

    results = {}
    for name in (names or available()):
        module = REGISTRY.get(name)
        if module is None:
            results[name] = {"error": f"unknown source {name}"}
            continue
        try:
            posts = module.fetch(conn, limit=limit)
        except Exception as exc:  # noqa: BLE001 - one bad feed is not fatal
            results[name] = {"error": str(exc)[:160]}
            continue
        seen, added = store(conn, posts)
        results[name] = {"seen": seen, "new": added}
    return results


def store(conn, posts: list[dict]) -> tuple[int, int]:
    """Insert, idempotently, and queue anything new for triage.

    Deliberately mirrors db.upsert_posts rather than reusing it: that one is
    built around X's payload shape (media keys, retweet targets, raw JSON), and
    bending it to fit three simpler sources would complicate the path that
    carries the most volume.
    """
    from .. import db

    fetched = db.now()
    added = 0
    for post in posts:
        cur = conn.execute(
            """
            INSERT INTO posts (id, author_handle, author_name, text, created_at,
                               fetched_at, is_retweet, capture_source, urls,
                               platform, url)
            VALUES (?,?,?,?,?,?,0,'timeline',?,?,?)
            ON CONFLICT(id) DO NOTHING
            """,
            (post["id"], post["author_handle"], post.get("author_name") or "",
             post["text"], post["created_at"], fetched,
             post.get("urls") or "[]", post["platform"], post.get("url")))
        if cur.rowcount:
            added += 1
            conn.execute(
                "INSERT INTO triage (post_id, stage, updated_at) "
                "VALUES (?, 'collected', ?) ON CONFLICT(post_id) DO NOTHING",
                (post["id"], fetched))
    conn.commit()
    return len(posts), added
