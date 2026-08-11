"""arXiv, via its Atom API.

The primary source. Everything else in this tool is people reacting to papers,
usually a day or two late and with the numbers rounded off; this is the paper.
`links.py` already resolves arXiv abstracts when someone posts one, but that
only ever finds what somebody chose to tweet — this reads the listing directly,
so a paper nobody amplified still gets judged on its merits.

Query is set by category and sorted by submission date. The API asks for one
request every three seconds, which one call per run comfortably respects.
"""

from __future__ import annotations

import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import timezone

from .rss import NS, _text, _when

API = "http://export.arxiv.org/api/query"
TIMEOUT = 25

# cs.LG machine learning · cs.CL language · cs.AI · cs.NE neural · stat.ML
CATEGORIES = ["cs.LG", "cs.CL", "cs.AI", "cs.NE", "stat.ML"]


def fetch(conn, limit: int = 60, categories: list[str] | None = None) -> list[dict]:
    query = " OR ".join(f"cat:{c}" for c in (categories or CATEGORIES))
    url = f"{API}?" + urllib.parse.urlencode({
        "search_query": query,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": min(limit, 100),
    })
    with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
        body = response.read().decode("utf-8", "replace")

    root = ET.fromstring(body)
    posts = []
    for entry in root.findall("atom:entry", NS):
        ident = _text(entry.find("atom:id", NS))
        if not ident:
            continue
        # http://arxiv.org/abs/2508.01234v1 -> 2508.01234
        paper_id = ident.rsplit("/", 1)[-1].split("v")[0]
        title = " ".join(_text(entry.find("atom:title", NS)).split())
        summary = " ".join(_text(entry.find("atom:summary", NS)).split())
        authors = [_text(a) for a in entry.findall("atom:author/atom:name", NS)]

        # The abstract is the claim, stated by the people who did the work. No
        # need to paraphrase it — the extractor reads this as-is.
        who = ", ".join(authors[:4]) + (" et al." if len(authors) > 4 else "")
        posts.append({
            "id": f"arxiv:{paper_id}",
            "author_handle": (authors[0] if authors else "arXiv")[:80],
            "author_name": who,
            "text": f"{title}\n\n{summary}",
            "created_at": _when(_text(entry.find("atom:published", NS))),
            "platform": "arxiv",
            "url": f"https://arxiv.org/abs/{paper_id}",
        })
    return posts
