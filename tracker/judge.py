"""Stage 3 — the judge.

Everything upstream is cheap and deterministic. This is the only stage that
reads a post and forms an opinion, and the only stage that costs money, so it
runs on the narrow end of the funnel: posts that survived triage and dedup.

Two design choices worth stating.

Grounded comparison, not blind novelty. A post in the ambiguous similarity band
arrives with its nearest neighbours attached, so the question becomes "does this
add anything beyond these five things you already surfaced?" rather than "is
this novel?" — the latter gets answered against stale training data with no idea
what the reader has already seen.

Every judgement is logged, including skips. Your definition of "valuable" is
personal and will not survive a generic prompt; the only way to tune it is to
read what got rejected and disagree with it. That requires a record.
"""

from __future__ import annotations

import json
import os

import anthropic

from . import context, db, feedback, novelty

MODEL = "claude-opus-5"
PROMPT_VERSION = "v2"
MAX_TOKENS = 8000

SYSTEM = """You screen posts from AI researchers and practitioners for a daily \
digest read by someone technical who already follows the field closely.

Judge two things independently.

NOVELTY — does this add something beyond the prior items shown to you? A \
rephrasing of a known result, a reaction to news already covered, or a \
restatement of consensus is not novel, however well written. When no prior \
items are shown, judge against what a well-read practitioner would already \
consider common knowledge.

VALUE — would a practitioner change what they read, build, or believe because \
of this? Concrete results, non-obvious technical claims, useful tooling, and \
specific numbers score high. Vague predictions, hype, engagement bait, \
self-promotion, and meta-commentary about the AI discourse score low.

Score each 1-5. Recommend surfacing only when novelty >= 3 AND value >= 4.

Be strict. Missing one good post costs far less than a digest the reader stops \
trusting. But judge the content, not the author's fame: an unknown researcher \
posting a concrete result outranks a famous one posting a platitude.

If a <quoting> block is present the candidate is a quote-post: someone sharing \
another post with their own comment. Judge what the COMMENT contributes on top \
of the quoted content. A bare reaction ("👀", "this", an emoji) contributes \
nothing and scores low on both axes however important the quoted post is — the \
quoted post is collected separately and judged on its own merits. Substantive \
commentary that adds data, disagreement, correction, or context can score high \
even when brief.

Images attached to the post are included in this message when present. Many \
posts are a screenshot of a benchmark table, a chart, or code — read them as \
part of the content, and score numbers or results visible in an image exactly \
as you would if they had been typed out.

A <linked> block is the content behind a URL in the post. A post that is just \
"great paper: <link>" has no argument of its own, but the linked work can still \
be worth surfacing — judge the recommendation plus what was linked.

shared_by_tracked_accounts=N means N experts the reader follows independently \
shared this post. Treat it as evidence the topic matters — it raises the ceiling \
on VALUE, but it is not a substitute for content: a vacuous post shared by five \
people is still vacuous. Corroboration cannot by itself make something novel.

A candidate marked kind="reply" is a response inside someone else's thread. \
Judge it on its own content: a sharp correction or added datapoint is valuable, \
agreement and applause are not.

The rationale must be one sentence explaining the scores — it is read by a \
human tuning this prompt, so say what actually drove the decision."""

SCHEMA = {
    "type": "object",
    "properties": {
        "novelty": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "value": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "category": {
            "type": "string",
            "enum": ["research", "tooling", "product", "opinion", "meta"],
        },
        "rationale": {"type": "string"},
        "verdict": {"type": "string", "enum": ["surface", "skip"]},
    },
    "required": ["novelty", "value", "category", "rationale", "verdict"],
    "additionalProperties": False,
}


def build_prompt(post: dict, neighbours: list[dict]) -> str:
    view = post.get("view") or {}
    size = view.get("thread_size")
    attrs = f' thread_posts="{size}"' if size and size > 1 else ""
    if view.get("source") == "reply":
        attrs += ' kind="reply"'
    parts = [
        f"<candidate author=\"@{post['author_handle']}\" posted=\"{post['created_at']}\"{attrs}>",
        (view.get("text") or post["text"]).strip(),
        "</candidate>",
    ]
    for link in view.get("links") or []:
        blurb = " ".join(filter(None, [link.get("title"), link.get("summary")]))
        if blurb:
            parts += [f'\n<linked site="{link.get("site","")}">',
                      blurb[:700], "</linked>"]
    quoted = view.get("quoted")
    if quoted:
        parts += [
            f"\n<quoting author=\"@{quoted['author_handle']}\">",
            " ".join(quoted["text"].split())[:900],
            "</quoting>",
        ]
    if neighbours:
        parts.append("\n<previously_collected>")
        for i, n in enumerate(neighbours, 1):
            text = " ".join(n["text"].split())[:400]
            parts.append(
                f'[{i}] ({n["created_at"][:10]}, @{n["author_handle"]}, '
                f'similarity {n["similarity"]:.2f}) {text}'
            )
        parts.append("</previously_collected>")
    else:
        parts.append("\n<previously_collected>none</previously_collected>")
    return "\n".join(parts)


def pending(conn, limit: int) -> list[dict]:
    """Posts that survived dedup and haven't been judged under this prompt."""
    rows = conn.execute(
        """
        SELECT p.id, p.author_handle, p.text, p.created_at, p.thread_size,
               t.max_similarity
        FROM posts p
        JOIN triage t ON t.post_id = p.id
        WHERE t.stage = 'triaged'
          AND p.id NOT IN (SELECT post_id FROM judgements
                           WHERE model = ? AND prompt_version = ?)
        ORDER BY p.created_at DESC
        LIMIT ?
        """,
        (MODEL, PROMPT_VERSION, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _content(post: dict, neighbours: list[dict]) -> list[dict]:
    """User turn: any attached images, then the text prompt.

    Images go in the same request as the judgement rather than a separate
    description pass — one call, and the model sees the picture in context
    instead of a lossy caption of it. X media URLs are publicly reachable, so
    they can be referenced by URL with nothing to download.
    """
    blocks: list[dict] = []
    for image in (post.get("view") or {}).get("images") or []:
        blocks.append({"type": "image",
                       "source": {"type": "url", "url": image["url"]}})
    blocks.append({"type": "text", "text": build_prompt(post, neighbours)})
    return blocks


def judge_one(client, post: dict, neighbours: list[dict], model: str,
              effort: str = "medium", system_text: str | None = None) -> dict:
    response = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=[{
            "type": "text",
            "text": system_text or SYSTEM,
            # Stable prefix, so every call after the first is a cache read at
            # ~10% of input price. The volatile candidate goes in the user turn.
            "cache_control": {"type": "ephemeral"},
        }],
        output_config={"format": {"type": "json_schema", "schema": SCHEMA},
                       "effort": effort},
        messages=[{"role": "user", "content": _content(post, neighbours)}],
    )

    if response.stop_reason == "refusal":
        return {"error": "refusal", "usage": response.usage}
    if response.stop_reason == "max_tokens":
        return {"error": "max_tokens", "usage": response.usage}

    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        return {"error": "no_text", "usage": response.usage}
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        return {"error": "bad_json", "usage": response.usage}
    result["usage"] = response.usage
    return result


def _context_for(conn, post_id: str, k: int) -> list[dict]:
    """Neighbours worth showing the judge.

    Anything below the novelty floor is noise — a 0.20-similarity post shares no
    claim with the candidate, and padding the prompt with unrelated text costs
    tokens while making the comparison harder, not easier.
    """
    return [n for n in novelty.neighbours(conn, post_id, k=k)
            if n["similarity"] >= novelty.NOVEL_AT]


def replay(conn, limit: int = 20, model: str = MODEL, effort: str = "medium",
           k: int = 5, version: str | None = None) -> int:
    """Re-judge already-judged posts under the current prompt.

    Tuning without this is guesswork: you change the prompt, and the only way to
    know whether it helped is to run the new version over the same posts and
    compare. Old judgements are kept — they stay attributed to the prompt version
    that produced them, so nothing is lost by iterating.
    """
    target = version or PROMPT_VERSION
    posts = conn.execute(
        """
        SELECT DISTINCT p.id, p.author_handle, p.text, p.created_at
        FROM posts p JOIN judgements j ON j.post_id = p.id
        WHERE p.id NOT IN (SELECT post_id FROM judgements WHERE prompt_version = ?)
        ORDER BY p.created_at DESC LIMIT ?
        """, (target, limit)).fetchall()
    if not posts:
        print(f"Nothing to replay: every judged post already has a '{target}' verdict.")
        print("Bump PROMPT_VERSION in tracker/judge.py after editing the prompt.")
        return 0

    client = anthropic.Anthropic()
    changed = 0
    for row in posts:
        post = dict(row)
        neigh = _context_for(conn, post["id"], k)
        result = judge_one(client, post, neigh, model, effort)
        if "error" in result:
            print(f"  FAIL {result['error']} @{post['author_handle']}")
            continue
        prior = conn.execute(
            "SELECT verdict, novelty, value FROM judgements WHERE post_id=? "
            "ORDER BY id DESC LIMIT 1", (post["id"],)).fetchone()
        usage = result.get("usage")
        conn.execute(
            "INSERT INTO judgements (post_id, model, prompt_version, verdict, novelty, "
            "value, category, rationale, neighbours, input_tokens, output_tokens, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (post["id"], model, target, result["verdict"], result["novelty"],
             result["value"], result["category"], result["rationale"],
             json.dumps([n["post_id"] for n in neigh]),
             usage.input_tokens if usage else None,
             usage.output_tokens if usage else None, db.now()))
        conn.commit()
        if prior and (prior["verdict"] != result["verdict"]):
            changed += 1
            print(f"  CHANGED {prior['verdict']}->{result['verdict']} "
                  f"(v{prior['value']}->{result['value']}) @{post['author_handle']}")
    print(f"\n{len(posts)} replayed, {changed} verdicts changed")
    return 0


def run(conn, limit: int = 20, model: str = MODEL, effort: str = "medium",
        k: int = 5, dry_run: bool = False) -> int:
    posts = pending(conn, limit)
    if not posts:
        print("Nothing to judge. Run ./run dedup --apply first.")
        return 0

    if dry_run:
        print(f"{len(posts)} posts would be judged with {model} (effort={effort})")
        sample = posts[0]
        sample["view"] = context.describe(conn, sample["id"], sample["text"])
        neigh = _context_for(conn, sample["id"], k)
        print(f"\n--- example prompt for {sample['id']} ---")
        print(build_prompt(sample, neigh)[:1200])
        print("\n(dry run — no API calls made)")
        return 0

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set.")
        return 1

    client = anthropic.Anthropic()
    # Calibration examples live in the cached prefix, so they are nearly free
    # per call and only invalidate the cache when you rate something new.
    system_text = SYSTEM + feedback.render_examples(conn)
    fb = feedback.stats(conn)
    if fb["total"]:
        print(f"(using {fb['total']} reader ratings as calibration, "
              f"{fb['disagreements']} of them disagreements)")
    surfaced = skipped = failed = 0
    in_tok = out_tok = 0

    for post in posts:
        post["view"] = context.describe(conn, post["id"], post["text"])
        neigh = _context_for(conn, post["id"], k)
        result = judge_one(client, post, neigh, model, effort, system_text)

        usage = result.get("usage")
        if usage:
            in_tok += usage.input_tokens
            out_tok += usage.output_tokens

        if "error" in result:
            failed += 1
            print(f"  FAIL  {result['error']:<12} @{post['author_handle']}")
            continue

        conn.execute(
            """
            INSERT INTO judgements (post_id, model, prompt_version, verdict, novelty,
                                    value, category, rationale, neighbours,
                                    input_tokens, output_tokens, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (post["id"], model, PROMPT_VERSION, result["verdict"], result["novelty"],
             result["value"], result["category"], result["rationale"],
             json.dumps([n["post_id"] for n in neigh]),
             usage.input_tokens if usage else None,
             usage.output_tokens if usage else None, db.now()),
        )
        stage = "judged" if result["verdict"] == "surface" else "dropped"
        reason = None if result["verdict"] == "surface" else "low_value"
        conn.execute(
            "UPDATE triage SET stage=?, drop_reason=?, updated_at=? WHERE post_id=?",
            (stage, reason, db.now(), post["id"]))
        conn.commit()

        mark = "SURFACE" if result["verdict"] == "surface" else "skip   "
        if result["verdict"] == "surface":
            surfaced += 1
        else:
            skipped += 1
        text = " ".join(post["text"].split())[:60]
        print(f"  {mark} n{result['novelty']} v{result['value']} "
              f"{result['category']:<10} @{post['author_handle']:<15} {text}")

    # Opus 5 pricing: $5 / $25 per MTok. Cache reads are far cheaper, so this
    # is an upper bound rather than an exact figure.
    cost = in_tok / 1e6 * 5 + out_tok / 1e6 * 25
    print(f"\n{surfaced} surfaced, {skipped} skipped, {failed} failed")
    print(f"tokens: {in_tok:,} in / {out_tok:,} out  (<= ${cost:.3f})")
    return 0
