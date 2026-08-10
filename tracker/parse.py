"""Turn X GraphQL responses into post dicts.

Promoted from spike/dump_timeline.py after milestone 1 proved the approach.

The parser deliberately does not hardcode the instructions[] path: it walks the
whole response looking for tweet-shaped nodes. That path differs between list,
profile, and home timelines, and X reshuffles it periodically — walking survives
those changes, which is the whole reason this works unattended.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

GRAPHQL_RE = re.compile(r"/i/api/graphql/[^/]+/(?P<op>[A-Za-z0-9_]+)")

# Operations that carry timeline entries. Anything else is captured but not parsed.
TIMELINE_OPS = {
    "ListLatestTweetsTimeline",
    "UserTweets",
    "UserTweetsAndReplies",
    "HomeLatestTimeline",
    "HomeTimeline",
}

# X's created_at format: "Sun Aug 02 03:00:09 +0000 2026"
_X_TIME_FMT = "%a %b %d %H:%M:%S %z %Y"


def operation_name(url: str) -> str | None:
    match = GRAPHQL_RE.search(url)
    return match.group("op") if match else None


def to_iso(x_timestamp: str | None) -> str | None:
    """Normalise X's timestamp to ISO 8601 UTC so dates sort lexicographically."""
    if not x_timestamp:
        return None
    try:
        dt = datetime.strptime(x_timestamp, _X_TIME_FMT)
        return dt.astimezone(timezone.utc).isoformat()
    except ValueError:
        return None


def _is_tweet(node: dict) -> bool:
    return node.get("__typename") == "Tweet" and "legacy" in node


def collect_tweets(node, embedded: bool = False, out: list | None = None) -> list:
    """Walk a response, returning [(tweet_node, embedded), ...].

    `embedded` marks tweets found *inside* another tweet — retweet originals and
    quoted posts. They are real posts worth keeping for context, but they are not
    posts the tracked account chose to write, so downstream stages treat them
    differently.
    """
    if out is None:
        out = []

    if isinstance(node, dict):
        target = None
        if _is_tweet(node):
            target = node
        elif node.get("__typename") == "TweetWithVisibilityResults" and isinstance(node.get("tweet"), dict):
            target = node["tweet"]

        if target is not None:
            out.append((target, embedded))
            # Anything nested below a tweet is, by definition, embedded.
            for value in target.values():
                collect_tweets(value, True, out)
            return out

        for value in node.values():
            collect_tweets(value, embedded, out)

    elif isinstance(node, list):
        for item in node:
            collect_tweets(item, embedded, out)

    return out


def _author(tweet: dict) -> tuple[str, str]:
    """X moved screen_name from user.legacy to user.core; support both."""
    user = tweet.get("core", {}).get("user_results", {}).get("result", {})
    core = user.get("core", {})
    legacy = user.get("legacy", {})
    return (
        core.get("screen_name") or legacy.get("screen_name") or "?",
        core.get("name") or legacy.get("name") or "",
    )


def _text(tweet: dict) -> str:
    """note_tweet holds untruncated text for long posts; prefer it."""
    note = (
        tweet.get("note_tweet", {})
        .get("note_tweet_results", {})
        .get("result", {})
        .get("text")
    )
    return note or tweet.get("legacy", {}).get("full_text", "")


def _media(tweet: dict) -> list[dict]:
    """Attached images. extended_entities is the complete list; entities truncates."""
    legacy = tweet.get("legacy") or {}
    items = (legacy.get("extended_entities") or {}).get("media") \
        or (legacy.get("entities") or {}).get("media") or []
    out = []
    for item in items:
        url = item.get("media_url_https")
        if not url:
            continue
        out.append({
            "id": str(item.get("id_str") or item.get("media_key") or url),
            "url": url,
            "kind": item.get("type"),
            "alt_text": item.get("ext_alt_text"),
        })
    return out


def parse_tweet(tweet: dict, embedded: bool = False) -> dict | None:
    legacy = tweet.get("legacy") or {}
    post_id = tweet.get("rest_id") or legacy.get("id_str")
    if not post_id:
        return None

    created = to_iso(legacy.get("created_at"))
    if not created:
        return None  # undated posts are useless to a time-windowed pipeline

    handle, name = _author(tweet)
    return {
        "id": str(post_id),
        "author_handle": handle,
        "author_name": name,
        "text": _text(tweet),
        "created_at": created,
        "conversation_id": legacy.get("conversation_id_str"),
        "reply_to_id": legacy.get("in_reply_to_status_id_str"),
        "is_retweet": bool(legacy.get("retweeted_status_result")),
        # Which post this amplifies. Without it, "three accounts you track all
        # shared this" is invisible and gets discarded as duplicate text.
        "retweet_of_id": (
            legacy.get("retweeted_status_result", {})
            .get("result", {})
            .get("rest_id")
        ),
        "quoted_id": legacy.get("quoted_status_id_str"),
        "urls": [u.get("expanded_url") for u in legacy.get("entities", {}).get("urls", []) if u.get("expanded_url")],
        "capture_source": "embedded" if embedded else "timeline",
        "media": _media(tweet),
    }


def posts_from_response(body) -> list[dict]:
    """Extract every parseable post from one GraphQL response body."""
    posts: dict[str, dict] = {}
    for node, embedded in collect_tweets(body):
        parsed = parse_tweet(node, embedded)
        if not parsed:
            continue
        # A post seen both as a timeline entry and as an embedded copy is a
        # timeline post — don't let the embedded copy downgrade it.
        existing = posts.get(parsed["id"])
        if existing and existing["capture_source"] == "timeline":
            continue
        posts[parsed["id"]] = parsed
    return list(posts.values())
