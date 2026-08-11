"""Hacker News, via the public Firebase API.

No key, no auth, no rate limit worth worrying about. Worth having for a reason
that is not obvious: HN is where a release gets argued with. The announcement is
on X and the blog; the person explaining why the benchmark is misleading is in
the comments here. That is exactly the corroboration-and-correction signal the
reply mining was built for on X, available without a browser.

Only stories are collected, filtered to AI relevance by title. Comments are a
much larger volume of much thinner text, and the funnel already has enough to
get through.
"""

from __future__ import annotations

import json
import re
import urllib.request
from datetime import datetime, timezone

API = "https://hacker-news.firebaseio.com/v0"
TIMEOUT = 15

# Deliberately broad. This is a cheap pre-filter to keep the judge's queue
# bounded, not a relevance judgement — that is the judge's job, with context.
TOPICS = re.compile(
    r"\b(ai|llm|llms|gpt|claude|gemini|llama|mistral|qwen|deepseek|openai|"
    r"anthropic|deepmind|hugging ?face|transformer|diffusion|neural|"
    r"machine learning|deep learning|inference|fine-?tun\w*|embedding|rag|"
    r"agent|agents|agentic|reinforcement learning|rlhf|quantiz\w*|"
    r"model|models|gpu|cuda|vllm|pytorch|tensor)\b", re.I)

MIN_SCORE = 20


def _get(path: str):
    with urllib.request.urlopen(f"{API}/{path}.json", timeout=TIMEOUT) as response:
        return json.loads(response.read().decode())


def relevant(title: str) -> bool:
    return bool(TOPICS.search(title or ""))


def fetch(conn, limit: int = 60, min_score: int = MIN_SCORE) -> list[dict]:
    ids = (_get("topstories") or [])[:200]
    posts = []
    for item_id in ids:
        if len(posts) >= limit:
            break
        try:
            item = _get(f"item/{item_id}")
        except Exception:  # noqa: BLE001 - skip one story, not the run
            continue
        if not item or item.get("type") != "story" or item.get("dead"):
            continue
        title = item.get("title") or ""
        if item.get("score", 0) < min_score or not relevant(title):
            continue

        # The link is the story; the HN page is where the argument is. Both are
        # worth having, so the discussion goes in the text and the article URL
        # becomes the canonical link the resolver will follow.
        parts = [title]
        if item.get("text"):
            parts.append(item["text"])
        parts.append(f"{item.get('score', 0)} points, "
                     f"{item.get('descendants', 0)} comments on Hacker News.")
        posts.append({
            "id": f"hn:{item_id}",
            "author_handle": item.get("by") or "hn",
            "author_name": item.get("by") or "",
            "text": "\n\n".join(parts),
            "created_at": datetime.fromtimestamp(
                item.get("time", 0), tz=timezone.utc).isoformat(),
            "platform": "hn",
            "url": item.get("url") or f"https://news.ycombinator.com/item?id={item_id}",
            "urls": json.dumps([item["url"]] if item.get("url") else []),
        })
    return posts
