"""Group independent reports of the same story.

Six accounts announcing one model release is not six findings. Dedup does not
catch this and should not: those posts are differently worded, written
independently, and the fact that six people bothered is real corroboration —
the system already treats that as signal worth keeping. The mistake was
rendering it as six separate items.

So the grouping happens at read time, on the *extracted* headline rather than
the raw post. That matters: measured on a real day, five independent
announcements of the same release sat at 0.64-0.80 raw similarity — under any
dedup threshold safe enough to use — but 0.79-0.86 once extraction had stripped
each author's framing down to what happened. Extraction normalises voice, which
is exactly what makes the underlying story comparable.

Nothing is dropped or rewritten. A cluster keeps every member, picks the
best-scored one to lead, and the rest become corroboration on that one line.
"""

from __future__ import annotations

import numpy as np

from . import embed

# Tuned against the corpus: 0.62 merges independent reports of one release
# without pulling in unrelated posts that merely share a topic. Two papers both
# about RL post-training land around 0.5 and stay separate.
THRESHOLD = 0.62

# Two accounts saying the same thing a month apart are not one story.
MAX_DAYS_APART = 4


def _text(item: dict) -> str:
    """What the item is *about*, preferring the extraction's neutral phrasing."""
    extraction = item.get("extraction") or {}
    parts = [item.get("headline") or extraction.get("headline") or "",
             item.get("so_what") or extraction.get("so_what") or ""]
    joined = " ".join(p for p in parts if p).strip()
    return joined or " ".join((item.get("text") or "").split())[:400]


def _days(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    return abs((np.datetime64(a[:10]) - np.datetime64(b[:10])) / np.timedelta64(1, "D"))


def group(items: list[dict], threshold: float = THRESHOLD) -> list[dict]:
    """[{lead, members, sources, size}], best cluster first.

    Greedy single-pass agglomeration, matching against every member rather than
    only the lead. Announcements of one event vary in framing — the vendor's
    post and a downstream integrator's post can each sit near a third post while
    scoring below threshold against each other — so lead-only matching leaves
    stragglers outside a cluster they obviously belong to. The date bound is
    what keeps single-linkage from chaining off into a general topic.

    Items arrive already ranked, so the first item to claim a cluster is the
    strongest one in it and becomes the lead.
    """
    if not items:
        return []

    backend = embed.BACKENDS[-1]  # semantic: this is a meaning question
    try:
        vectors = backend.encode_many([_text(i) for i in items])
    except Exception:
        # Without the semantic backend every item is its own story, which is
        # exactly the old behaviour — degraded, not broken.
        return [{"lead": i, "members": [i], "sources": [i.get("author_handle")],
                 "size": 1} for i in items]

    clusters: list[dict] = []
    for index, item in enumerate(items):
        for cluster in clusters:
            near = any(float(vectors[index] @ vectors[i]) >= threshold
                       for i in cluster["_indices"])
            if not near:
                continue
            if _days(item.get("created_at"), cluster["lead"].get("created_at")) > MAX_DAYS_APART:
                continue
            cluster["members"].append(item)
            cluster["_indices"].append(index)
            handle = item.get("author_handle")
            if handle and handle not in cluster["sources"]:
                cluster["sources"].append(handle)
            break
        else:
            clusters.append({"lead": item, "members": [item], "_indices": [index],
                             "sources": [item.get("author_handle")]})

    for cluster in clusters:
        cluster.pop("_indices", None)
        cluster["size"] = len(cluster["members"])
    return clusters
