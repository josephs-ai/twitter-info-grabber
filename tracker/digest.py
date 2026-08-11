"""Stage 4 — render surfaced posts into something readable."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import cluster, db, extract, paths, strictness

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = paths.data_dir() / "digests"

CATEGORY_ORDER = ["research", "tooling", "product", "opinion", "meta"]
CATEGORY_TITLE = {
    "research": "Research",
    "tooling": "Tooling",
    "product": "Products & releases",
    "opinion": "Opinion",
    "meta": "Meta",
}


def gather(conn, since_hours: int, limit: int, min_value: int | None = None) -> list[dict]:
    """Surfaced posts, best first.

    `min_value` overrides the judge's own verdict — useful when a quiet day
    would otherwise produce an empty digest and you would rather see the
    near-misses than nothing.
    """
    if min_value is None:
        # The bar is a preference applied now, not a verdict frozen at judge time.
        where, params = strictness.clause(strictness.load(conn))
    else:
        where, params = "j.value >= ?", [min_value]

    rows = conn.execute(
        f"""
        SELECT p.id, p.author_handle, p.author_name, p.text, p.created_at,
               p.capture_source, p.amplifiers,
               j.novelty, j.value, j.category, j.rationale
        FROM judgements j
        JOIN posts p ON p.id = j.post_id
        WHERE {where}
          AND p.created_at > datetime('now', ?)
        ORDER BY j.value DESC, p.amplifiers DESC, j.novelty DESC, p.created_at DESC
        LIMIT ?
        """,
        (*params, f"-{since_hours} hours", limit),
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["extraction"] = extract.for_post(conn, item["id"])
        items.append(item)

    # One story per entry, same as the app and the notifier. This was the last
    # output still listing six announcements of one release as six findings.
    out = []
    for group in cluster.group(items):
        lead = dict(group["lead"])
        lead["also"] = [
            {"handle": m["author_handle"], "id": m["id"]} for m in group["members"][1:]
        ]
        out.append(lead)
    return out


def counts(conn, since_hours: int) -> dict:
    def scalar(sql, *args):
        row = conn.execute(sql, args).fetchone()
        return row[0] if row else 0

    window = f"-{since_hours} hours"
    return {
        "collected": scalar(
            "SELECT COUNT(*) FROM posts WHERE fetched_at > datetime('now', ?)", window),
        "judged": scalar(
            "SELECT COUNT(*) FROM judgements WHERE created_at > datetime('now', ?)", window),
        "dropped_dup": scalar(
            "SELECT COUNT(*) FROM triage WHERE drop_reason='duplicate' "
            "AND updated_at > datetime('now', ?)", window),
    }


def render(items: list[dict], stats: dict, title_date: str, relaxed: bool) -> str:
    lines = [f"# AI Signal — {title_date}", ""]
    lines.append(
        f"_{stats['collected']} posts collected · {stats['judged']} judged · "
        f"{stats['dropped_dup']} deduped · {len(items)} surfaced_"
    )
    if relaxed:
        lines.append("")
        lines.append("> Relaxed threshold — nothing cleared the normal bar today.")
    lines.append("")

    if not items:
        lines += ["Nothing met the bar today.", "",
                  "That is a real result, not a failure: on a quiet day the honest "
                  "output is an empty digest. Use `--min-value 3` to see near-misses."]
        return "\n".join(lines)

    by_category: dict[str, list[dict]] = {}
    for item in items:
        by_category.setdefault(item["category"], []).append(item)

    ordered = [c for c in CATEGORY_ORDER if c in by_category]
    ordered += [c for c in by_category if c not in CATEGORY_ORDER]

    for category in ordered:
        lines.append(f"## {CATEGORY_TITLE.get(category, category.title())}")
        lines.append("")
        for item in by_category[category]:
            handle = item["author_handle"]
            when = item["created_at"][:16].replace("T", " ")
            tag = " · reply" if item["capture_source"] == "reply" else ""
            if item.get("amplifiers"):
                n = item["amplifiers"]
                tag += f" · shared by {n} tracked account{'s' if n != 1 else ''}"
            also = item.get("also") or []
            if also:
                tag += f" · reported by {len(also) + 1} accounts"
            lines.append(
                f"**[@{handle}](https://x.com/{handle})** · {when} · "
                f"novelty {item['novelty']} · value {item['value']}{tag}"
            )
            lines.append("")

            ex = item.get("extraction")
            if ex and ex.get("headline"):
                # Lead with what happened; the raw post is evidence, not the story.
                lines.append(f"**{ex['headline']}**")
                lines.append("")
                for claim in ex.get("claims", [])[:4]:
                    lines.append(f"- {claim}")
                if ex.get("numbers"):
                    figures = ", ".join(f"**{n['value']}** {n['measures']}"
                                        for n in ex["numbers"][:4])
                    lines.append(f"- Figures: {figures}")
                if ex.get("so_what"):
                    lines.append("")
                    lines.append(f"Why it matters: {ex['so_what']}")
                if ex.get("entities"):
                    names = ", ".join(e["name"] for e in ex["entities"][:8])
                    lines.append("")
                    lines.append(f"`{names}`")
                lines.append("")
                lines.append("<details><summary>original post</summary>")
                lines.append("")
                for para in item["text"].strip().split("\n"):
                    if para.strip():
                        lines.append(f"> {para.strip()}")
                lines.append("")
                lines.append("</details>")
            else:
                for para in item["text"].strip().split("\n"):
                    if para.strip():
                        lines.append(f"> {para.strip()}")
                lines.append("")
                lines.append(f"*{item['rationale']}*")
            lines.append("")
            if also:
                # Independent reports, so each one is worth linking: the
                # corroboration is the point, and one of them may say it better.
                others = " · ".join(
                    f"[@{a['handle']}](https://x.com/{a['handle']}/status/{a['id']})"
                    for a in also[:6])
                lines.append(f"Also reported by {others}")
                lines.append("")
            lines.append(f"[open](https://x.com/{handle}/status/{item['id']})")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build(conn, since_hours: int = 24, limit: int = 15,
          min_value: int | None = None, write: bool = True) -> str:
    items = gather(conn, since_hours, limit, min_value)
    relaxed = min_value is not None

    # A quiet day should not silently produce nothing at all — fall back to
    # near-misses so the reader can see what the judge nearly liked.
    if not items and min_value is None:
        items = gather(conn, since_hours, limit, min_value=3)
        relaxed = bool(items)

    stats = counts(conn, since_hours)
    today = datetime.now(timezone.utc)
    # %-d (no zero padding) is a glibc extension: it raises on Windows.
    # Build the day number ourselves so the format is portable.
    title_date = f"{today:%A} {today.day} {today:%B %Y}"
    markdown = render(items, stats, title_date, relaxed)

    if write:
        OUT_DIR.mkdir(exist_ok=True)
        path = OUT_DIR / f"{today:%Y-%m-%d}.md"
        path.write_text(markdown)
        conn.execute(
            "INSERT INTO digests (generated_at, post_ids, markdown) VALUES (?,?,?)",
            (db.now(), json.dumps([i["id"] for i in items]), markdown))
        conn.commit()
        for item in items:
            conn.execute(
                "UPDATE triage SET stage='published', updated_at=? WHERE post_id=?",
                (db.now(), item["id"]))
        conn.commit()
        print(f"wrote {path}")
    return markdown
