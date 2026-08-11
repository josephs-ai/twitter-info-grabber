"""Pull the actual information out of a post.

The judge answers "is this worth reading". This answers "what does it say" —
the specific claims, the numbers, the named models and papers, and why any of
it matters. Without this the digest is a reading list; with it, the digest is
the information, and the link is there only if you want to verify it.

Runs only on posts that already cleared the judge, so it operates on a handful
of items a day rather than everything collected. It reads the same complete
unit the judge does: stitched threads, quoted posts, resolved links, images.

Extraction is strictly grounded. A model asked to summarise will happily invent
a plausible number; the prompt below forbids importing anything not present in
the material, and leaving a field empty is explicitly the correct answer when
the post does not contain it.
"""

from __future__ import annotations

import json

import anthropic

from . import context, db, strictness
from .judge import MODEL, _content, build_prompt, unescape

PROMPT_VERSION = "x1"
MAX_TOKENS = 8000

SYSTEM = """You extract structured information from posts by AI researchers and \
practitioners, for a technical reader who wants the findings without reading \
the original.

Extract ONLY what the material actually states. The material includes the post \
text, any thread continuation, any quoted post, the content behind links, and \
any attached images. Do not add background knowledge, do not infer results that \
were not reported, and never invent a number. If the post does not contain \
something, return an empty value for that field — an empty list is the correct \
answer for a post that makes no quantitative claim, and is far better than a \
plausible guess.

headline — one sentence stating what is new, written so someone who never sees \
the original post understands the finding. Not a description of the post ("a \
thread about X") but the substance itself ("training one transformer layer \
recovers most of full-parameter RL gains").

claims — the specific factual assertions, one per item, each self-contained. \
Include the conditions a claim depends on when stated.

numbers — concrete figures with their units and what they measure. Include \
figures that appear only in an image. Empty if none.

entities — named things: models, papers, datasets, benchmarks, organisations, \
tools. Use kind to say which.

so_what — one sentence on why a practitioner would care, or what it changes. \
Empty if the post genuinely has no practical implication."""

SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "claims": {"type": "array", "items": {"type": "string"}},
        "numbers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "value": {"type": "string"},
                    "measures": {"type": "string"},
                },
                "required": ["value", "measures"],
                "additionalProperties": False,
            },
        },
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": ["model", "paper", "dataset", "benchmark",
                                 "org", "tool", "person"],
                    },
                },
                "required": ["name", "kind"],
                "additionalProperties": False,
            },
        },
        "so_what": {"type": "string"},
    },
    "required": ["headline", "claims", "numbers", "entities", "so_what"],
    "additionalProperties": False,
}


def pending(conn, limit: int, force: bool = False) -> list[dict]:
    """Posts clearing the current bar, without an extraction at this prompt version.

    This follows the strictness setting rather than the stored `verdict`. The
    two came apart when the bar moved to read time: loosening the slider showed
    more posts in the feed, but extraction still keyed off the frozen verdict,
    so the extra posts rendered as raw text — the tool's whole point missing
    exactly when the funnel widened.
    """
    where, bar = strictness.clause(strictness.load(conn))
    clause = "" if force else (
        "AND p.id NOT IN (SELECT post_id FROM extractions WHERE prompt_version = ?)")
    params = [PROMPT_VERSION] if not force else []
    rows = conn.execute(
        f"""
        SELECT DISTINCT p.id, p.author_handle, p.text, p.created_at, p.thread_size
        FROM posts p JOIN judgements j ON j.post_id = p.id
        WHERE {where} {clause}
        ORDER BY j.value DESC, p.created_at DESC LIMIT ?
        """, (*bar, *params, limit)).fetchall()
    return [dict(r) for r in rows]


def extract_one(client, conn, post: dict, model: str = MODEL,
                effort: str = "medium") -> dict:
    post["view"] = context.describe(conn, post["id"], post["text"])
    response = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=[{"type": "text", "text": SYSTEM,
                 "cache_control": {"type": "ephemeral"}}],
        output_config={"format": {"type": "json_schema", "schema": SCHEMA},
                       "effort": effort},
        messages=[{"role": "user", "content": _content(post, [])}],
    )
    if response.stop_reason in ("refusal", "max_tokens"):
        return {"error": response.stop_reason}
    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        return {"error": "no_text"}
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        return {"error": "bad_json"}
    result["headline"] = unescape(result.get("headline"))
    result["so_what"] = unescape(result.get("so_what"))
    result["claims"] = [unescape(c) for c in result.get("claims", [])]
    for n in result.get("numbers", []):
        n["value"] = unescape(n.get("value"))
        n["measures"] = unescape(n.get("measures"))
    result["usage"] = response.usage
    return result


def run(conn, limit: int = 15, model: str = MODEL, effort: str = "medium",
        force: bool = False) -> int:
    posts = pending(conn, limit, force)
    if not posts:
        print("Nothing to extract — every surfaced post already has one.")
        return 0

    client = anthropic.Anthropic()
    done = failed = 0
    for post in posts:
        result = extract_one(client, conn, post, model, effort)
        if "error" in result:
            failed += 1
            print(f"  FAIL {result['error']} @{post['author_handle']}")
            continue
        conn.execute(
            "INSERT INTO extractions (post_id, model, prompt_version, headline, "
            "claims, entities, numbers, so_what, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (post["id"], model, PROMPT_VERSION, result["headline"],
             json.dumps(result["claims"]), json.dumps(result["entities"]),
             json.dumps(result["numbers"]), result["so_what"], db.now()))
        conn.commit()
        done += 1
        print(f"\n  @{post['author_handle']}")
        print(f"  {result['headline']}")
        for claim in result["claims"][:3]:
            print(f"    - {claim[:100]}")
        if result["numbers"]:
            figures = ", ".join(f"{n['value']} ({n['measures']})"
                                for n in result["numbers"][:3])
            print(f"    numbers: {figures[:110]}")
    print(f"\n{done} extracted, {failed} failed")
    return 0


def for_post(conn, post_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM extractions WHERE post_id=? ORDER BY id DESC LIMIT 1",
        (post_id,)).fetchone()
    if not row:
        return None
    out = dict(row)
    for field in ("claims", "entities", "numbers"):
        try:
            out[field] = json.loads(out[field] or "[]")
        except json.JSONDecodeError:
            out[field] = []
    return out
