"""Stage 2 — novelty against our own corpus.

The central idea from the spec: novelty is relative to a corpus, not to a
model's training data. Asking a model "is this new?" cold gets a judgment
against a months-stale snapshot of the internet with no knowledge of what you
have already been shown. Comparing against everything we've collected in a
rolling window answers the question that actually matters.

Three bands, from SPEC.md §6:

    sim >= duplicate_at   near-duplicate  -> drop, no model call
    sim <  novel_at       nothing similar -> judge with no context
    in between            ambiguous       -> judge, with the neighbours attached

The middle band is the point. It converts a vague question ("is this novel?")
into a grounded comparison ("does this add anything beyond these five things?"),
which models are markedly better at answering.
"""

from __future__ import annotations

import numpy as np

from . import context, db, embed

DUPLICATE_AT = 0.92
NOVEL_AT = 0.60
WINDOW_DAYS = 45


def embed_pending(conn, batch: int = 500, window_days: int = WINDOW_DAYS,
                  keep_margin: int = 30) -> int:
    """Embed posts missing a vector, for every backend. Resumable.

    Only posts inside the comparison window (plus the same margin prune keeps)
    are embedded. Without that bound this fights prune(): it would re-create
    every vector prune had just deleted, so the two would churn against each
    other on every run and the database would never actually shrink.
    """
    total = 0
    cutoff = window_days + keep_margin
    for embedder in embed.BACKENDS:
        rows = conn.execute(
            "SELECT p.id, p.text FROM posts p "
            "LEFT JOIN embeddings e ON e.post_id = p.id AND e.model = ? "
            "WHERE e.post_id IS NULL "
            "  AND p.created_at > datetime('now', ?) LIMIT ?",
            (embedder.name, f"-{cutoff} days", batch),
        ).fetchall()
        if not rows:
            continue
        texts = [context.full_text(conn, r["id"], r["text"]) for r in rows]
        vectors = embedder.encode_many(texts)
        for row, vec in zip(rows, vectors):
            conn.execute(
                "INSERT INTO embeddings (post_id, model, dim, vector) VALUES (?,?,?,?) "
                "ON CONFLICT(post_id, model) DO UPDATE SET "
                "dim=excluded.dim, vector=excluded.vector",
                (row["id"], embedder.name, embedder.dim, embed.to_blob(vec)),
            )
        conn.commit()
        total += len(rows)
    return total


def _load_window(conn, window_days: int, embedder, exclude_id: str | None = None):
    """Load vectors for posts inside the comparison window.

    Brute-force cosine over a few tens of thousands of vectors is milliseconds
    in numpy, so there is no index to maintain and no approximation error.
    """
    rows = conn.execute(
        "SELECT e.post_id, e.vector, p.created_at, p.author_handle, p.text "
        "FROM embeddings e JOIN posts p ON p.id = e.post_id "
        "WHERE e.model = ? AND p.created_at > datetime('now', ?) "
        "AND (? IS NULL OR e.post_id != ?)",
        (embedder.name, f"-{window_days} days", exclude_id, exclude_id),
    ).fetchall()
    if not rows:
        return [], np.zeros((0, embedder.dim), dtype=np.float32)
    matrix = np.vstack([embed.from_blob(r["vector"], embedder.dim) for r in rows])
    return rows, matrix


def prune(conn, window_days: int = WINDOW_DAYS, keep_margin: int = 30) -> dict:
    """Drop embeddings for posts too old to be compared against.

    Vectors are the bulk of the database — roughly 9KB per post across both
    backends — and dedup only ever looks inside a rolling window. Keeping
    vectors for posts far outside it costs gigabytes a year and buys nothing.
    The posts themselves are never touched; re-embedding is one cheap local
    pass if the window is ever widened.
    """
    cutoff = window_days + keep_margin
    before = conn.execute("SELECT COUNT(*) n FROM embeddings").fetchone()["n"]
    conn.execute(
        "DELETE FROM embeddings WHERE post_id IN ("
        "  SELECT id FROM posts WHERE created_at < datetime('now', ?))",
        (f"-{cutoff} days",))
    conn.commit()
    after = conn.execute("SELECT COUNT(*) n FROM embeddings").fetchone()["n"]
    return {"removed": before - after, "remaining": after, "cutoff_days": cutoff}


def _window(conn, window_days: int):
    """Load the comparison window once, aligned across every backend.

    Returns (meta, matrices) where meta is one row per post in post-id order and
    matrices maps model name -> matrix whose row i corresponds to meta[i]. The
    shared ordering is what lets us take an element-wise max across backends.
    """
    meta = conn.execute(
        "SELECT id, created_at, author_handle, text FROM posts "
        "WHERE created_at > datetime('now', ?) ORDER BY id",
        (f"-{window_days} days",),
    ).fetchall()
    if not meta:
        return [], {}

    meta = [row for row in meta if embed.has_signal(row["text"])]
    if not meta:
        return [], {}
    order = {row["id"]: i for i, row in enumerate(meta)}
    matrices = {}
    for embedder in embed.BACKENDS:
        matrix = np.zeros((len(meta), embedder.dim), dtype=np.float32)
        rows = conn.execute(
            "SELECT e.post_id, e.vector FROM embeddings e "
            "JOIN posts p ON p.id = e.post_id "
            "WHERE e.model = ? AND p.created_at > datetime('now', ?)",
            (embedder.name, f"-{window_days} days"),
        ).fetchall()
        for row in rows:
            i = order.get(row["post_id"])
            if i is not None:
                matrix[i] = embed.from_blob(row["vector"], embedder.dim)
        matrices[embedder.name] = matrix
    return meta, matrices


def _sims_for(conn, post_id: str, meta, matrices) -> np.ndarray | None:
    """Similarity of `post_id` against the whole window: max over backends.

    A pair only has to look alike to ONE backend to score high, which is what we
    want — lexical catches copy-paste and typos, semantic catches paraphrase,
    and a duplicate is a duplicate either way.
    """
    if not meta:
        return None
    combined = None
    for embedder in embed.BACKENDS:
        row = conn.execute(
            "SELECT vector FROM embeddings WHERE post_id=? AND model=?",
            (post_id, embedder.name)).fetchone()
        if not row:
            continue
        query = embed.from_blob(row["vector"], embedder.dim)
        sims = matrices[embedder.name] @ query
        combined = sims if combined is None else np.maximum(combined, sims)
    return combined


def neighbours(conn, post_id: str, k: int = 8, window_days: int = WINDOW_DAYS) -> list[dict]:
    """Most similar prior posts, nearest first — the context the judge receives."""
    meta, matrices = _window(conn, window_days)
    sims = _sims_for(conn, post_id, meta, matrices)
    if sims is None:
        return []
    for i, row in enumerate(meta):
        if row["id"] == post_id:
            sims[i] = -1.0
    return [
        {
            "post_id": meta[i]["id"],
            "similarity": float(sims[i]),
            "author_handle": meta[i]["author_handle"],
            "text": meta[i]["text"],
            "created_at": meta[i]["created_at"],
        }
        for i in np.argsort(-sims)[:k] if sims[i] > 0
    ]


def classify(similarity: float | None,
             duplicate_at: float = DUPLICATE_AT,
             novel_at: float = NOVEL_AT) -> str:
    if similarity is None:
        return "novel"
    if similarity >= duplicate_at:
        return "duplicate"
    if similarity < novel_at:
        return "novel"
    return "ambiguous"


def scan(conn, window_days: int = WINDOW_DAYS, k: int = 8,
         duplicate_at: float = DUPLICATE_AT, novel_at: float = NOVEL_AT,
         apply: bool = False) -> dict:
    """Score every un-triaged post against the window.

    Nothing is deleted, ever. Duplicates are marked in `triage` with a reason and
    the id of what they matched, so 'what did dedup throw away, and was any of it
    good?' stays a query rather than a guess.
    """
    pending = conn.execute(
        "SELECT p.id, p.author_handle, p.text FROM posts p "
        "JOIN triage t ON t.post_id = p.id WHERE t.stage = 'collected' "
        "ORDER BY p.created_at").fetchall()

    meta, matrices = _window(conn, window_days)
    counts = {"duplicate": 0, "ambiguous": 0, "novel": 0, "no_text": 0}
    examples: list[dict] = []

    for post in pending:
        if not embed.has_signal(post["text"]):
            counts["no_text"] += 1
            if apply:
                conn.execute(
                    "UPDATE triage SET stage='dropped', drop_reason='no_text', "
                    "updated_at=? WHERE post_id=?", (db.now(), post["id"]))
            continue

        sims = _sims_for(conn, post["id"], meta, matrices)
        if sims is None or len(meta) < 2:
            band, best, nearest = "novel", None, None
        else:
            for i, row in enumerate(meta):
                if row["id"] == post["id"]:
                    sims[i] = -1.0        # never match a post against itself
            best_i = int(np.argmax(sims))
            best = float(sims[best_i])
            nearest = meta[best_i]["id"]
            band = classify(best, duplicate_at, novel_at)

        counts[band] += 1
        if band == "duplicate" and len(examples) < 8:
            nearest_row = next((m for m in meta if m["id"] == nearest), None)
            examples.append({
                "post_id": post["id"], "author": post["author_handle"],
                "text": " ".join(post["text"].split())[:80],
                "similarity": best, "nearest": nearest,
                "nearest_author": nearest_row["author_handle"] if nearest_row else "?",
                "nearest_text": " ".join(nearest_row["text"].split())[:80] if nearest_row else "",
            })

        if apply:
            if band == "duplicate":
                conn.execute(
                    "UPDATE triage SET stage='dropped', drop_reason='duplicate', "
                    "max_similarity=?, nearest_post_id=?, updated_at=? WHERE post_id=?",
                    (best, nearest, db.now(), post["id"]))
            else:
                conn.execute(
                    "UPDATE triage SET stage='triaged', max_similarity=?, "
                    "nearest_post_id=?, updated_at=? WHERE post_id=?",
                    (best, nearest, db.now(), post["id"]))

    if apply:
        conn.commit()
    return {"scanned": len(pending), "corpus": len(meta), **counts, "examples": examples}
