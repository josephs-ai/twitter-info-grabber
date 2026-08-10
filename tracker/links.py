"""Resolve links so that "great paper: <url>" carries some signal.

A post whose entire content is a link is invisible to a text pipeline: it has no
claim to judge, no words to embed, and it gets dropped as no_text. Yet those are
often the highest-value posts on the timeline, because a researcher sharing a
paper is exactly the recommendation you want.

Deliberately minimal: stdlib only, short timeouts, a size cap, and title plus
description rather than full page text. arXiv gets a special path because its
abstract is the single most useful field and the API is stable. Anything that
fails is recorded as an error and never retried in a tight loop.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from html import unescape

from . import db

TIMEOUT = 8
MAX_BYTES = 400_000
UA = "Mozilla/5.0 (compatible; ai-signal-tracker/1.0)"

ARXIV_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", re.I)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
DESC_RE = re.compile(
    r'<meta[^>]+(?:name|property)=["\'](?:description|og:description)["\']'
    r'[^>]+content=["\'](.*?)["\']', re.I | re.S)

# Link shorteners and social URLs carry no content of their own.
SKIP_HOSTS = ("t.co", "x.com", "twitter.com", "bit.ly")


def is_safe_url(url: str) -> bool:
    """Refuse anything that points inside our own network.

    These URLs come from posts, and reply mining means an attacker only has to
    reply to a tracked account to choose one. Without this, a crafted link makes
    the tool fetch loopback services, private-range hosts, or a cloud metadata
    endpoint, and store whatever came back in the database.
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    host = parts.hostname
    if not host:
        return False
    if host.lower() in ("localhost", "localhost.localdomain"):
        return False
    try:
        # Check every address the name resolves to: a hostname can point at
        # 127.0.0.1 just as easily as a literal can.
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return False
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            return False
    return True


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return " ".join(unescape(text).split())


def _fetch(url: str) -> tuple[str, str, str]:
    """Return (status, title, summary)."""
    if any(h in url.lower() for h in SKIP_HOSTS):
        return "skipped", "", ""
    if not is_safe_url(url):
        return "blocked", "", ""

    arxiv = ARXIV_RE.search(url)
    if arxiv:
        api = f"http://export.arxiv.org/api/query?id_list={arxiv.group(1)}"
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(api, headers={"User-Agent": UA}),
                    timeout=TIMEOUT) as response:
                body = response.read(MAX_BYTES).decode("utf-8", "replace")
            title = _clean(re.search(r"<title>(.*?)</title>", body, re.S | re.I)
                           .group(1)) if "<title>" in body else ""
            summary_match = re.search(r"<summary>(.*?)</summary>", body, re.S | re.I)
            return "ok", title, _clean(summary_match.group(1))[:1200] if summary_match else ""
        except Exception:
            return "error", "", ""

    try:
        request = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            ctype = response.headers.get("Content-Type", "")
            if "html" not in ctype and "text" not in ctype:
                return "skipped", "", ""
            body = response.read(MAX_BYTES).decode("utf-8", "replace")
    except (urllib.error.URLError, socket.timeout, ValueError, OSError):
        return "error", "", ""
    except Exception:
        return "error", "", ""

    title_match = TITLE_RE.search(body)
    desc_match = DESC_RE.search(body)
    return ("ok",
            _clean(title_match.group(1))[:300] if title_match else "",
            _clean(desc_match.group(1))[:800] if desc_match else "")


def pending_urls(conn, limit: int) -> list[str]:
    """URLs from recent posts that we have not resolved yet."""
    rows = conn.execute(
        "SELECT urls FROM posts WHERE urls IS NOT NULL AND urls != '[]' "
        "AND created_at > datetime('now','-30 days') ORDER BY created_at DESC"
    ).fetchall()
    seen, out = set(), []
    known = {r["url"] for r in conn.execute("SELECT url FROM links")}
    for row in rows:
        try:
            for url in json.loads(row["urls"] or "[]"):
                if url and url not in seen and url not in known:
                    seen.add(url)
                    out.append(url)
                    if len(out) >= limit:
                        return out
        except json.JSONDecodeError:
            continue
    return out


def resolve(conn, limit: int = 40) -> dict:
    urls = pending_urls(conn, limit)
    counts = {"ok": 0, "error": 0, "skipped": 0}
    for url in urls:
        status, title, summary = _fetch(url)
        counts[status] = counts.get(status, 0) + 1
        site = re.sub(r"^www\.", "", (re.search(r"https?://([^/]+)", url)
                                      or re.match("(.*)", url)).group(1))[:80]
        conn.execute(
            "INSERT INTO links (url, title, summary, site, status, fetched_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET title=excluded.title, "
            "summary=excluded.summary, status=excluded.status, fetched_at=excluded.fetched_at",
            (url, title, summary, site, status, db.now()))
    conn.commit()
    return {"attempted": len(urls), **counts}


def for_post(conn, post_id: str) -> list[dict]:
    row = conn.execute("SELECT urls FROM posts WHERE id=?", (post_id,)).fetchone()
    if not row or not row["urls"]:
        return []
    try:
        urls = json.loads(row["urls"])
    except json.JSONDecodeError:
        return []
    out = []
    for url in urls:
        link = conn.execute(
            "SELECT url, title, summary, site FROM links WHERE url=? AND status='ok'",
            (url,)).fetchone()
        if link and (link["title"] or link["summary"]):
            out.append(dict(link))
    return out
