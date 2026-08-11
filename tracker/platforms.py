"""What a post's address and byline look like, per service.

The pipeline was written against X and it showed in exactly one way: six places
downstream built `https://x.com/{handle}/status/{id}` in an f-string. Nothing
else was coupled — dedup, judging, extraction, clustering and the digest all
read `posts.text` and never asked where it came from — so this is the whole of
what "support another platform" costs on the reading side.

Sources store a canonical `url` when they have one, which is always the right
answer. This is the fallback for rows collected before the column existed, and
the place to add a service whose URLs are derivable.
"""

from __future__ import annotations

PLATFORMS = {
    "x":        {"label": "X",           "author": "https://x.com/{handle}",
                 "post": "https://x.com/{handle}/status/{id}", "at": True},
    "bluesky":  {"label": "Bluesky",     "author": "https://bsky.app/profile/{handle}",
                 "post": None, "at": True},
    "hn":       {"label": "Hacker News", "author": "https://news.ycombinator.com/user?id={handle}",
                 "post": "https://news.ycombinator.com/item?id={id}", "at": False},
    "arxiv":    {"label": "arXiv",       "author": None,
                 "post": "https://arxiv.org/abs/{id}", "at": False},
    "rss":      {"label": "Web",         "author": None, "post": None, "at": False},
}


def spec(platform: str | None) -> dict:
    return PLATFORMS.get(platform or "x", PLATFORMS["x"])


def post_url(row) -> str:
    """The canonical link for a post, whatever it came from."""
    stored = _get(row, "url")
    if stored:
        return stored
    platform = _get(row, "platform") or "x"
    template = spec(platform)["post"]
    if not template:
        return ""
    return template.format(handle=_get(row, "author_handle") or "",
                           id=_get(row, "id") or "")


def author_url(row) -> str:
    stored_platform = _get(row, "platform") or "x"
    template = spec(stored_platform)["author"]
    handle = _get(row, "author_handle") or ""
    if not template or not handle:
        return post_url(row)
    return template.format(handle=handle)


def byline(row) -> str:
    """`@karpathy` on X, plain `Anthropic` for a blog. The @ is not universal."""
    handle = _get(row, "author_handle") or ""
    return f"@{handle}" if spec(_get(row, "platform")).get("at") else handle


def label(platform: str | None) -> str:
    return spec(platform)["label"]


def _get(row, key: str):
    """Rows arrive as sqlite3.Row or plain dict depending on the caller."""
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None
