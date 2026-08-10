"""Assemble the complete unit of meaning for a post.

A post on a timeline is often a fragment. Judging or embedding the fragment
alone throws away most of the signal:

  - A self-thread is one idea split across N posts.
  - A quote-post is a comment ABOUT something; the comment alone can be a
    single emoji while the thing quoted is a major announcement.
  - A reply is a response to something.

Every stage that reads post text goes through here, so each post is evaluated
as the thing a human would actually see on screen.
"""

from __future__ import annotations

from . import links, threads

# A comment shorter than this adds no argument of its own — it is amplification
# ("👀", "wow", "this"). Still meaningful as a signal that someone notable
# thought it worth sharing, but the content being judged is what they quoted.
BARE_COMMENT_CHARS = 25

# Images are expensive in tokens (up to ~4.8k each). Two is enough to see what a
# post is showing; beyond that is usually a photo dump.
MAX_IMAGES = 2


def quoted_post(conn, post_id: str) -> dict | None:
    row = conn.execute(
        "SELECT o.id, o.author_handle, o.text, o.created_at "
        "FROM posts p JOIN posts o ON o.id = p.quoted_id WHERE p.id = ?",
        (post_id,)).fetchone()
    return dict(row) if row else None


def full_text(conn, post_id: str, own_text: str) -> str:
    """Text used for embedding and dedup.

    Threads become the whole thread; quote-posts become comment + quoted, so a
    bare-emoji quote of a big announcement is compared on the announcement
    rather than on the emoji.
    """
    text = threads.text_for(conn, post_id, own_text)
    quoted = quoted_post(conn, post_id)
    if quoted:
        text = f"{text}\n\n[quoting @{quoted['author_handle']}] {quoted['text']}"
    for link in links.for_post(conn, post_id):
        blurb = link["title"]
        if link["summary"]:
            blurb = f"{blurb}. {link['summary']}" if blurb else link["summary"]
        if blurb:
            text = f"{text}\n\n[link: {blurb[:600]}]"
    return text.strip()


def describe(conn, post_id: str, own_text: str) -> dict:
    """Structured view for the judge prompt."""
    row = conn.execute(
        "SELECT thread_size, capture_source, reply_to_id, amplifiers "
        "FROM posts WHERE id=?", (post_id,)).fetchone()
    thread_size = row["thread_size"] if row else None

    return {
        "amplifiers": row["amplifiers"] if row else 0,
        "text": threads.text_for(conn, post_id, own_text),
        "thread_size": thread_size,
        "quoted": quoted_post(conn, post_id),
        "links": links.for_post(conn, post_id),
        "images": images_for(conn, post_id),
        "is_reply": bool(row["reply_to_id"]) if row else False,
        "source": row["capture_source"] if row else "timeline",
    }


def images_for(conn, post_id: str) -> list[dict]:
    """Photos on this post or on the post it quotes.

    A screenshot of a benchmark table is the entire content of many posts, so a
    text-only pipeline reads them as empty.
    """
    rows = conn.execute(
        "SELECT m.url, m.alt_text FROM media m WHERE m.post_id = ? AND m.kind = 'photo' "
        "UNION ALL "
        "SELECT m.url, m.alt_text FROM media m JOIN posts p ON p.quoted_id = m.post_id "
        "WHERE p.id = ? AND m.kind = 'photo' LIMIT ?",
        (post_id, post_id, MAX_IMAGES)).fetchall()
    return [dict(r) for r in rows]


def is_bare_amplification(conn, post_id: str, own_text: str) -> bool:
    """A quote-post whose own comment carries no argument."""
    quoted = quoted_post(conn, post_id)
    if not quoted:
        return False
    stripped = " ".join(own_text.split())
    return len(stripped) < BARE_COMMENT_CHARS
