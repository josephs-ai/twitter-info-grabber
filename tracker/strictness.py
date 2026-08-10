"""How high the bar sits.

The judge scores novelty and value separately, and those scores are a reading of
the post — they do not change when your taste does. What changes is the bar you
hold them to.

So the bar is applied at read time rather than baked into the stored verdict.
Moving it re-filters every post already judged, instantly and for free: no API
calls, no re-judging, and the history stays intact because nothing is rewritten.
The stored `verdict` column remains as a record of what the judge itself
concluded at the time.

The bar is also written into the prompt, so the model knows what it is aiming at
on future runs — but the two uses are independent, which is why changing it
never invalidates existing work.
"""

from __future__ import annotations

import json

from . import db

# (min_novelty, min_value). Both must be met.
LEVELS = [
    {"key": "everything", "label": "Everything",
     "novelty": 1, "value": 1,
     "note": "No filtering. Useful for seeing what the judge actually saw."},
    {"key": "permissive", "label": "Permissive",
     "novelty": 2, "value": 3,
     "note": "Anything with a real point. Expect a long digest."},
    {"key": "balanced", "label": "Balanced",
     "novelty": 3, "value": 3,
     "note": "New, and worth a practitioner's attention."},
    {"key": "strict", "label": "Strict",
     "novelty": 3, "value": 4,
     "note": "Would change what you read, build, or believe. The default."},
    {"key": "severe", "label": "Severe",
     "novelty": 4, "value": 5,
     "note": "Only genuinely significant findings. Some days will be empty."},
]

DEFAULT_KEY = "strict"


def _table(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS settings ("
                 "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)")
    conn.commit()


def load(conn) -> dict:
    _table(conn)
    row = conn.execute("SELECT value FROM settings WHERE key='strictness'").fetchone()
    if row:
        try:
            saved = json.loads(row["value"])
            level = next((l for l in LEVELS if l["key"] == saved.get("key")), None)
            if level:
                return dict(level)
        except json.JSONDecodeError:
            pass
    return dict(next(l for l in LEVELS if l["key"] == DEFAULT_KEY))


def save(conn, key: str) -> dict:
    _table(conn)
    level = next((l for l in LEVELS if l["key"] == key), None)
    if not level:
        raise ValueError(f"unknown strictness level: {key}")
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ('strictness',?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (json.dumps({"key": key}), db.now()))
    conn.commit()
    return dict(level)


def clause(level: dict | None = None, alias: str = "j") -> tuple[str, list]:
    """SQL fragment selecting posts that clear the bar."""
    level = level or dict(next(l for l in LEVELS if l["key"] == DEFAULT_KEY))
    return (f"{alias}.novelty >= ? AND {alias}.value >= ?",
            [level["novelty"], level["value"]])


def preview(conn) -> list[dict]:
    """How many already-judged posts each level would surface.

    This is the whole point of applying the bar at read time — the answer is a
    query over stored scores, so the slider can show its effect before you
    commit to it.
    """
    out = []
    for level in LEVELS:
        row = conn.execute(
            "SELECT COUNT(DISTINCT post_id) n FROM judgements "
            "WHERE novelty >= ? AND value >= ?",
            (level["novelty"], level["value"])).fetchone()
        entry = dict(level)
        entry["count"] = row["n"] if row else 0
        out.append(entry)
    return out


def describe_for_prompt(level: dict) -> str:
    """The bar, phrased for the judge."""
    return (f"Recommend surfacing only when novelty >= {level['novelty']} "
            f"AND value >= {level['value']}.")
