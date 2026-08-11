"""Ask the corpus a question.

Thousands of posts accumulate here and, until now, the only way to see any of
them was to wait for the judge to pick them. That makes the corpus exhaust. It
should be the asset: "what has anyone said about speculative decoding" is a
question this database can already answer, because every post is embedded twice
for dedup and those vectors are just sitting there.

Three signals, combined:

  semantic  — the potion-base-8M vectors, so a query finds posts that mean the
              same thing without sharing words
  lexical   — the hashed n-gram vectors, which nail exact names, model numbers
              and quoted phrases that a small semantic model blurs
  substring — a plain match, because when someone searches "Qwen3-235B" they
              want the post containing that exact string ranked first, and no
              embedding guarantees that

Scores are combined with max() for the two vector backends (same reasoning as
dedup: neither dominates) plus a flat bonus for a literal hit. Unjudged posts
are included — most of the corpus has never reached the judge, and excluding it
would hide most of what search is for.
"""

from __future__ import annotations

import numpy as np

from . import embed

# Below this, results are noise. Vector similarity always returns *something*.
MIN_SCORE = 0.25
LITERAL_BONUS = 0.35


def _vector_scores(conn, query: str, pool: int) -> dict[str, float]:
    scores: dict[str, float] = {}
    for backend in embed.BACKENDS:
        rows = conn.execute(
            "SELECT post_id, vector FROM embeddings WHERE model=? AND dim=?",
            (backend.name, backend.dim)).fetchall()
        if not rows:
            continue
        try:
            q = backend.encode(query)
        except Exception:
            # A missing optional backend must not take the whole search down.
            continue
        if not np.any(q):
            continue
        matrix = np.vstack([embed.from_blob(r["vector"], backend.dim) for r in rows])
        sims = matrix @ q
        # Only the head of each backend's ranking is worth merging.
        top = np.argsort(-sims)[:pool]
        for i in top:
            pid = rows[int(i)]["post_id"]
            scores[pid] = max(scores.get(pid, 0.0), float(sims[int(i)]))
    return scores


def run(conn, query: str, limit: int = 40, pool: int = 300) -> list:
    """Ranked posts, as rows shaped like the feed's (judgement columns may be NULL)."""
    query = (query or "").strip()
    if not query:
        return []

    scores = _vector_scores(conn, query, pool)

    literal = {
        r["id"] for r in conn.execute(
            "SELECT id FROM posts WHERE text LIKE ? ESCAPE '\\' LIMIT ?",
            (f"%{_escape(query)}%", pool))
    }
    for pid in literal:
        scores[pid] = scores.get(pid, 0.0) + LITERAL_BONUS

    ranked = [(pid, s) for pid, s in scores.items() if s >= MIN_SCORE]
    ranked.sort(key=lambda x: -x[1])
    ranked = ranked[:limit]
    if not ranked:
        return []

    order = {pid: i for i, (pid, _) in enumerate(ranked)}
    marks = ",".join("?" * len(ranked))
    rows = conn.execute(
        f"""
        SELECT p.id, p.author_handle, p.text, p.created_at, p.amplifiers,
               p.capture_source, p.thread_size,
               j.verdict, j.novelty, j.value, j.category, j.rationale
        FROM posts p
        LEFT JOIN judgements j ON j.post_id = p.id
        WHERE p.id IN ({marks})
        GROUP BY p.id
        """, [pid for pid, _ in ranked]).fetchall()
    return sorted(rows, key=lambda r: order[r["id"]])


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
