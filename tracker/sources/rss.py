"""RSS and Atom feeds — lab blogs, newsletters, personal sites.

The highest-density source in the whole system and the cheapest to run. A lab's
release post says in four paragraphs what forty tweets gesture at, and it
arrives over plain HTTP with no login and no terms to violate.

Parsed with the standard library's XML parser rather than feedparser: feeds are
simple, the dependency list is deliberately short, and the two formats differ in
about six tag names. Entities are resolved conservatively — a feed is remote
input, so external entity resolution stays off.
"""

from __future__ import annotations

import html
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from .. import db, links

USER_AGENT = "ai-signal-tracker/1.0 (+https://github.com/josephs-ai/twitter-info-grabber)"
TIMEOUT = 20
MAX_CHARS = 4000          # a long essay is not more signal than its first pages
MAX_BYTES = 12_000_000    # a feed, not a download

NS = {"atom": "http://www.w3.org/2005/Atom",
      "dc": "http://purl.org/dc/elements/1.1/"}

# Checked live, not guessed. Anthropic publishes no public feed as of writing,
# which is why the lab you would most expect is missing.
DEFAULT_FEEDS = [
    ("https://openai.com/news/rss.xml", "OpenAI"),
    ("https://deepmind.google/blog/rss.xml", "Google DeepMind"),
    ("https://research.google/blog/rss/", "Google Research"),
    ("https://huggingface.co/blog/feed.xml", "Hugging Face"),
    ("https://blog.google/technology/ai/rss/", "Google AI"),
    ("https://engineering.fb.com/feed/", "Meta Engineering"),
    ("https://www.together.ai/blog/rss.xml", "Together AI"),
    ("https://bair.berkeley.edu/blog/feed.xml", "BAIR"),
    ("https://simonwillison.net/atom/everything/", "Simon Willison"),
    ("https://www.interconnects.ai/feed", "Interconnects"),
    ("https://lilianweng.github.io/index.xml", "Lilian Weng"),
    ("https://magazine.sebastianraschka.com/feed", "Sebastian Raschka"),
    ("https://importai.substack.com/feed", "Import AI"),
]


def table(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS feeds ("
        " url TEXT PRIMARY KEY, title TEXT, active INTEGER NOT NULL DEFAULT 1,"
        " last_fetched TEXT, last_error TEXT, added_at TEXT NOT NULL)")
    conn.commit()


def seed(conn) -> int:
    """Install the starter list. Idempotent, like everything else here."""
    table(conn)
    added = 0
    for url, title in DEFAULT_FEEDS:
        cur = conn.execute(
            "INSERT INTO feeds (url, title, added_at) VALUES (?,?,?) "
            "ON CONFLICT(url) DO NOTHING", (url, title, db.now()))
        added += cur.rowcount
    conn.commit()
    return added


def add(conn, url: str, title: str | None = None) -> bool:
    table(conn)
    if not links.is_safe_url(url):
        raise ValueError("refusing an unsafe or non-public URL")
    cur = conn.execute(
        "INSERT INTO feeds (url, title, added_at) VALUES (?,?,?) "
        "ON CONFLICT(url) DO UPDATE SET active=1", (url, title, db.now()))
    conn.commit()
    return bool(cur.rowcount)


def listing(conn) -> list[dict]:
    table(conn)
    return [dict(r) for r in conn.execute(
        "SELECT url, title, active, last_fetched, last_error FROM feeds "
        "ORDER BY active DESC, title")]


def _text(node) -> str:
    """Strip markup: the judge reads prose, and feeds ship escaped HTML."""
    if node is None:
        return ""
    raw = "".join(node.itertext()) if len(node) else (node.text or "")
    raw = html.unescape(raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    return " ".join(raw.split())


def _when(value: str | None) -> str:
    if not value:
        return db.now()
    value = value.strip()
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) \
            .astimezone(timezone.utc).isoformat()
    except ValueError:
        return db.now()


def parse(xml: str, feed_title: str | None = None) -> list[dict]:
    """Both formats, one pass. Returns rows ready for sources.store()."""
    root = ET.fromstring(xml)
    channel = root.find("channel")
    site = feed_title or _text(
        channel.find("title") if channel is not None else root.find("atom:title", NS))

    entries = (channel.findall("item") if channel is not None
               else root.findall("atom:entry", NS))
    posts = []
    for item in entries:
        atom = channel is None
        if atom:
            link_el = item.find("atom:link[@rel='alternate']", NS) \
                or item.find("atom:link", NS)
            url = (link_el.get("href") if link_el is not None else "") or ""
            title = _text(item.find("atom:title", NS))
            body = _text(item.find("atom:content", NS)) \
                or _text(item.find("atom:summary", NS))
            when = _when(_text(item.find("atom:published", NS))
                         or _text(item.find("atom:updated", NS)))
            ident = _text(item.find("atom:id", NS)) or url
            author = _text(item.find("atom:author/atom:name", NS)) or site
        else:
            url = _text(item.find("link"))
            title = _text(item.find("title"))
            body = _text(item.find("description"))
            when = _when(_text(item.find("pubDate")))
            ident = _text(item.find("guid")) or url
            author = _text(item.find("dc:creator", NS)) or site

        if not (title or body) or not ident:
            continue
        # Title first: it is the claim, and the body is the evidence — the same
        # order the extractor expects and the same order a reader wants.
        text = f"{title}\n\n{body}".strip()[:MAX_CHARS]
        posts.append({
            "id": f"rss:{abs(hash(ident)):x}" if len(ident) > 180 else f"rss:{ident}",
            "author_handle": (site or author or "web").strip()[:80],
            "author_name": author or site,
            "text": text,
            "created_at": when,
            "platform": "rss",
            "url": url,
        })
    return posts


def fetch(conn, limit: int = 60) -> list[dict]:
    table(conn)
    if not conn.execute("SELECT COUNT(*) n FROM feeds").fetchone()["n"]:
        seed(conn)

    out: list[dict] = []
    for row in conn.execute("SELECT url, title FROM feeds WHERE active=1"):
        url = row["url"]
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                # Read one byte past the cap so a truncated document is
                # detectable. Cutting the read short and parsing anyway gave
                # "unclosed CDATA section" on a feed that was perfectly valid.
                raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError(f"feed larger than {MAX_BYTES // 1_000_000}MB")
            body = raw.decode("utf-8", "replace")
            out.extend(parse(body, row["title"])[:limit])
            conn.execute("UPDATE feeds SET last_fetched=?, last_error=NULL WHERE url=?",
                         (db.now(), url))
        except Exception as exc:  # noqa: BLE001 - a dead feed must not stop the rest
            conn.execute("UPDATE feeds SET last_fetched=?, last_error=? WHERE url=?",
                         (db.now(), str(exc)[:160], url))
        conn.commit()
    return out
