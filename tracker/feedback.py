"""Teach the judge what you actually value.

A generic prompt encodes a generic notion of "valuable". Yours is specific and
will not survive contact with one — the only way to close the gap is to
disagree with the judge in writing and feed those disagreements back.

Ratings become few-shot examples in the system prompt. Because they sit in the
cached prefix they cost almost nothing per call, and because they are your own
posts they teach far more precisely than any amount of prompt rewording.

The examples that teach most are the ones where you and the judge disagreed —
a post it skipped that you rated good is a correction, not a confirmation.
"""

from __future__ import annotations

from . import db

MAX_EXAMPLES = 6


def rate(conn, post_id: str, rating: str, note: str | None = None) -> bool:
    if rating not in ("good", "bad"):
        raise ValueError("rating must be 'good' or 'bad'")
    exists = conn.execute("SELECT 1 FROM posts WHERE id=?", (post_id,)).fetchone()
    if not exists:
        return False
    conn.execute(
        "INSERT INTO feedback (post_id, rating, note, created_at) VALUES (?,?,?,?) "
        "ON CONFLICT(post_id) DO UPDATE SET rating=excluded.rating, "
        "note=excluded.note, created_at=excluded.created_at",
        (post_id, rating, note, db.now()))
    conn.commit()
    return True


def disagreements(conn) -> list[dict]:
    """Where your rating contradicts the judge — the highest-signal examples."""
    rows = conn.execute(
        """
        SELECT f.post_id, f.rating, f.note, p.text, p.author_handle,
               j.verdict, j.novelty, j.value, j.rationale
        FROM feedback f
        JOIN posts p ON p.id = f.post_id
        LEFT JOIN judgements j ON j.post_id = f.post_id
        WHERE (f.rating = 'good' AND (j.verdict = 'skip' OR j.verdict IS NULL))
           OR (f.rating = 'bad'  AND j.verdict = 'surface')
        ORDER BY f.created_at DESC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def examples(conn, limit: int = MAX_EXAMPLES) -> list[dict]:
    """Few-shot examples for the prompt: disagreements first, then agreements."""
    picked = disagreements(conn)[:limit]
    if len(picked) < limit:
        seen = {p["post_id"] for p in picked}
        extra = conn.execute(
            """
            SELECT f.post_id, f.rating, f.note, p.text, p.author_handle
            FROM feedback f JOIN posts p ON p.id = f.post_id
            ORDER BY f.created_at DESC LIMIT ?
            """, (limit * 2,)).fetchall()
        for row in extra:
            if row["post_id"] not in seen and len(picked) < limit:
                picked.append(dict(row))
    return picked


def render_examples(conn, limit: int = MAX_EXAMPLES) -> str:
    """Format ratings as a prompt block. Empty string when there is no feedback."""
    picked = examples(conn, limit)
    if not picked:
        return ""

    good = [p for p in picked if p["rating"] == "good"]
    bad = [p for p in picked if p["rating"] == "bad"]
    lines = ["\n\nThe reader has rated past decisions. These are calibration "
             "examples from this specific reader — weight them heavily, they "
             "override the general guidance above where they conflict."]

    if good:
        lines.append("\nRated WORTH SURFACING:")
        for p in good:
            text = " ".join(p["text"].split())[:220]
            note = f" [reader: {p['note']}]" if p.get("note") else ""
            lines.append(f'- "{text}"{note}')
    if bad:
        lines.append("\nRated NOT worth surfacing:")
        for p in bad:
            text = " ".join(p["text"].split())[:220]
            note = f" [reader: {p['note']}]" if p.get("note") else ""
            lines.append(f'- "{text}"{note}')
    return "\n".join(lines)


def stats(conn) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) total, SUM(rating='good') good, SUM(rating='bad') bad "
        "FROM feedback").fetchone()
    return {"total": row["total"] or 0, "good": row["good"] or 0,
            "bad": row["bad"] or 0, "disagreements": len(disagreements(conn))}
