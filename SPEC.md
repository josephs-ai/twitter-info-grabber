# AI Signal Tracker — Technical Spec

**Status:** draft v1
**Last updated:** 2026-08-05

---

## 1. What this is

A tool that follows a curated list of AI researchers and practitioners on X, collects
everything they post, and surfaces only the posts that are **(a) about AI and (b) actually
new** — meaning the idea isn't something the tool has already shown you in recent weeks.

The output is a short daily digest. The hard part isn't fetching posts; it's the
"is this new" judgment, which is a memory problem before it's an AI problem.

### Goals

- Track 20–50 hand-picked accounts without babysitting the scraper.
- Cut a few hundred posts/day down to ~5–15 items worth reading.
- Never show the same idea twice inside a rolling window.
- Run for under ~$10/month in model costs at that volume.

### Non-goals (v1)

- Not a general X firehose or trend detector. Curated accounts only.
- Not real-time. A few-hours delay is fine.
- No web UI. Digest is a Markdown file, optionally emailed or posted to Discord.
- No engagement metrics, follower graphs, or sentiment analysis.

---

## 2. Architecture

A funnel: free filters first, embeddings in the middle, LLM only at the narrow end.

```
┌────────────────────────────────────────────────────────────────┐
│ Stage 0 — COLLECT                        (Playwright, no AI)   │
│ Poll one X List timeline, intercept GraphQL JSON, upsert to DB │
│                                            ~250 posts/day      │
└────────────────────────────────────────────────────────────────┘
                              ↓
┌────────────────────────────────────────────────────────────────┐
│ Stage 1 — TRIAGE                       (heuristics, free)      │
│ Drop retweets w/o comment, sub-threshold length, non-AI posts  │
│ Keyword allowlist + embedding similarity to an "AI centroid"   │
│                                            ~100 posts/day      │
└────────────────────────────────────────────────────────────────┘
                              ↓
┌────────────────────────────────────────────────────────────────┐
│ Stage 2 — NOVELTY RETRIEVAL          (embeddings, free/local)  │
│ Embed candidate, kNN against everything seen in last N weeks   │
│  sim ≥ 0.92  → duplicate, drop deterministically               │
│  sim ≤ 0.60  → clearly novel, skip to Stage 3 with no context  │
│  in between  → send candidate + neighbors to Stage 3           │
│                                             ~70 posts/day      │
└────────────────────────────────────────────────────────────────┘
                              ↓
┌────────────────────────────────────────────────────────────────┐
│ Stage 3 — JUDGE                              (Claude API)      │
│ Grounded call: "given these similar prior items, does this add │
│ anything, and is it worth a practitioner's attention?"         │
│ Returns: verdict, novelty score, value score, one-line why     │
│                                            ~10 items/day       │
└────────────────────────────────────────────────────────────────┘
                              ↓
┌────────────────────────────────────────────────────────────────┐
│ Stage 4 — DIGEST                                (templating)   │
│ Rank, group by theme, render Markdown, deliver                 │
└────────────────────────────────────────────────────────────────┘
```

**Why this shape:** the expensive judgment runs on ~4% of collected volume. Stages 1–2
are deterministic and auditable, so when something wrong gets through you can tell
whether the filter or the model made the mistake.

---

## 3. Stack

| Concern | Choice | Rationale |
|---|---|---|
| Language | Python 3.12 | Playwright, sqlite3, and sentence-transformers all first-class |
| Collection | Playwright (Chromium, persistent profile) | Survives login; can intercept XHR |
| Storage | SQLite (WAL mode) | Single file, no server, handles this volume for years |
| Vectors | `sqlite-vec` extension, or numpy brute-force | <50k vectors — brute force cosine is milliseconds |
| Embeddings | `sentence-transformers` / `BAAI/bge-small-en-v1.5` (384-dim) | Runs on CPU, no API cost, no rate limit |
| Judgment | Anthropic API — `claude-opus-5` (default) | See §7 for the model/cost tradeoff |
| Scheduling | cron (or systemd timer) | Three runs/day is not a scheduling problem |

Anthropic does not ship an embeddings endpoint, so embeddings are local. If quality
turns out to be the bottleneck, Voyage AI (`voyage-3`) is the drop-in upgrade — swap the
`embed()` function, re-embed the corpus, done.

---

## 4. Data model

```sql
-- Raw capture. Never mutated after insert except `deleted_at`.
CREATE TABLE posts (
    id              TEXT PRIMARY KEY,       -- X status id (snowflake, string to dodge JS int limits)
    author_handle   TEXT NOT NULL,
    author_name     TEXT,
    text            TEXT NOT NULL,          -- full_text, entities already unescaped
    created_at      TEXT NOT NULL,          -- ISO 8601 UTC
    fetched_at      TEXT NOT NULL,
    conversation_id TEXT,                   -- thread grouping
    reply_to_id     TEXT,
    is_retweet      INTEGER NOT NULL DEFAULT 0,
    quoted_id       TEXT,
    urls            TEXT,                   -- JSON array of expanded URLs
    raw_json        TEXT,                   -- full GraphQL node, for reprocessing
    deleted_at      TEXT
);
CREATE INDEX idx_posts_created ON posts(created_at);
CREATE INDEX idx_posts_author  ON posts(author_handle);

-- Pipeline state, one row per post. Separate table so reprocessing is safe.
CREATE TABLE triage (
    post_id         TEXT PRIMARY KEY REFERENCES posts(id),
    stage           TEXT NOT NULL,          -- collected|triaged|judged|published|dropped
    drop_reason     TEXT,                   -- retweet|too_short|off_topic|duplicate|low_value
    ai_score        REAL,                   -- Stage 1 topical relevance, 0..1
    max_similarity  REAL,                   -- Stage 2 nearest-neighbour cosine
    nearest_post_id TEXT,
    updated_at      TEXT NOT NULL
);

CREATE TABLE embeddings (
    post_id   TEXT PRIMARY KEY REFERENCES posts(id),
    model     TEXT NOT NULL,                -- e.g. bge-small-en-v1.5
    dim       INTEGER NOT NULL,
    vector    BLOB NOT NULL                 -- float32 little-endian
);

-- One row per Stage 3 call. Immutable audit log — this is how you tune the judge.
CREATE TABLE judgements (
    id              INTEGER PRIMARY KEY,
    post_id         TEXT NOT NULL REFERENCES posts(id),
    model           TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    verdict         TEXT NOT NULL,          -- surface|skip
    novelty         INTEGER,                -- 1..5
    value           INTEGER,                -- 1..5
    category        TEXT,                   -- research|tooling|product|opinion|meta
    rationale       TEXT,
    neighbours      TEXT,                   -- JSON array of post_ids shown as context
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    created_at      TEXT NOT NULL
);

CREATE TABLE digests (
    id           INTEGER PRIMARY KEY,
    generated_at TEXT NOT NULL,
    post_ids     TEXT NOT NULL,             -- JSON array, ranked
    markdown     TEXT NOT NULL
);

CREATE TABLE accounts (
    handle     TEXT PRIMARY KEY,
    added_at   TEXT NOT NULL,
    notes      TEXT,
    active     INTEGER NOT NULL DEFAULT 1
);
```

Design note: `posts` is append-only raw capture; every derived judgment lives in its own
table keyed by post. That means you can delete `judgements` and re-run the whole pipeline
against a new prompt without re-scraping anything — which you will want to do repeatedly
in the first few weeks.

---

## 5. Stage 0 — Collection

### Approach

Browser automation against a **private X List** containing all tracked accounts. One list
timeline = one page load for all accounts, chronologically. That's ~3 requests/day instead
of ~150.

### Mechanics

1. **Persistent profile.** `chromium.launch_persistent_context(user_data_dir=...)`. Log the
   burner account in once by hand; the session cookie persists across runs.
2. **Intercept, don't parse.** Register `page.on("response", ...)` and capture responses whose
   URL matches `/i/api/graphql/.*/ListLatestTweetsTimeline`. The JSON payload carries
   `full_text`, author, `created_at`, IDs, and entities in a stable shape. The DOM changes
   constantly; the JSON shape rarely does.
3. **Scroll until overlap.** Scroll the timeline until you hit a post ID already in the DB,
   plus a small overlap buffer (10 posts), then stop. Hard cap at 15 scrolls per run.
4. **Upsert.** `INSERT ... ON CONFLICT(id) DO NOTHING`. Re-running is always safe.

### Operational rules

- **Burner account only.** Never the personal account. Expect to rebuild it occasionally.
- **Human-ish cadence.** Every 4–6 hours with ±25 minutes of jitter. Not a fixed cron minute.
- **Fail loud, fail safe.** If zero posts are captured in a run, log an error and exit
  non-zero — don't silently record an empty day. Two consecutive empty runs → alert.
- **Snapshot on failure.** On any exception, write `debug/<timestamp>.png` and the last
  captured JSON blob. Scraper breakage is diagnosed from these, not from stack traces.

### Known risks

| Risk | Mitigation |
|---|---|
| Account lock / captcha | Burner account; low request volume; run headed with a real profile |
| GraphQL response shape change | `raw_json` is stored; a parser fix can backfill from it |
| ToS: this is against X's terms | Personal read-only use; realistic worst case is account suspension |
| Login expiry | Health check on each run; if the timeline renders logged-out, alert immediately |

### Escape hatches

If the scraper becomes more maintenance than it's worth, the collection layer is swappable
with no downstream changes — everything after Stage 0 reads from SQLite:

- Per-request resellers (e.g. twitterapi.io) — pay per call rather than $200/mo flat.
- Official X API — the API tier that reads other users' timelines starts around $200/mo.
- RSS for the subset of tracked people who cross-post to blogs, Substack, or Bluesky.

---

## 6. Stages 1–2 — Triage and novelty

### Stage 1: cheap drops (in order)

1. Retweet with no added comment → drop.
2. `len(text) < 120` chars and no link → drop. Short reaction posts are almost never insight.
3. Pure reply into someone else's thread where the tracked account isn't the author of the
   root → drop (keeps self-threads, drops conversational noise).
4. **Topical relevance.** Cosine similarity between the post embedding and a precomputed
   "AI centroid" — the mean embedding of ~50 hand-written exemplar sentences about ML
   research, tooling, evals, deployment, etc. Keep if `sim > 0.35` **or** the post matches
   the keyword allowlist (`RL`, `eval`, `fine-tun`, `inference`, `context window`, model
   names, etc.). Union, not intersection — keywords catch jargon-dense posts the centroid
   misses, and the centroid catches conceptual posts with no keywords.

Both thresholds live in config and want tuning against your first week of real data.

### Stage 2: novelty against your own corpus

The key insight: **novelty is relative to a corpus, not to the model's training data.**
Asking an LLM "is this novel?" cold gets you a judgment against a months-stale snapshot of
the internet, with no knowledge of what you've already been shown.

So:

1. Embed the candidate.
2. Retrieve the `k=8` most similar posts from the last `N=45` days (config).
3. Branch on max similarity:

| Band | Action |
|---|---|
| `sim ≥ 0.92` | Near-duplicate or rephrase. Drop. No model call. |
| `0.60 ≤ sim < 0.92` | Ambiguous. Send to Stage 3 **with the neighbours as context.** |
| `sim < 0.60` | Nothing similar seen. Send to Stage 3 with no neighbours. |

The middle band is where the value is. It turns a vague question ("is this novel?") into a
grounded comparison ("here are 5 things you already told me about; does this add anything
beyond them?") — which language models are markedly better at.

The retrieval window is over *everything collected*, not just what was published. If three
people say the same thing and only the first was surfaced, the other two still get
suppressed as duplicates.

---

## 7. Stage 3 — The judge

### Call shape

One `messages.create` per candidate, with structured output so the result is parseable
without regex. Model default: **`claude-opus-5`**. See the cost table below before changing it.

```python
import anthropic

client = anthropic.Anthropic()

SYSTEM = """You screen posts from AI researchers and practitioners for a daily digest
read by someone technical who already follows the field closely.

Judge two things independently:

NOVELTY — does this add something beyond the prior items shown to you? A rephrasing of a
known result, a reaction to news already covered, or a restatement of consensus is not
novel, however well written. If no prior items are shown, judge novelty against what a
well-read practitioner would already consider common knowledge.

VALUE — would a practitioner change what they read, build, or believe because of this?
Concrete results, non-obvious technical claims, useful tooling, and specific numbers score
high. Vague predictions, hype, engagement bait, self-promotion, and meta-commentary about
the AI discourse score low.

Score each 1-5. Surface only when novelty >= 3 AND value >= 4. Be strict: the cost of
missing one good post is far lower than the cost of a digest the reader stops trusting."""

SCHEMA = {
    "type": "object",
    "properties": {
        "novelty":   {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "value":     {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "category":  {"type": "string",
                      "enum": ["research", "tooling", "product", "opinion", "meta"]},
        "rationale": {"type": "string", "description": "One sentence. Why this score."},
        "verdict":   {"type": "string", "enum": ["surface", "skip"]},
    },
    "required": ["novelty", "value", "category", "rationale", "verdict"],
    "additionalProperties": False,
}

response = client.messages.create(
    model="claude-opus-5",
    max_tokens=16000,
    system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
    output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
    messages=[{"role": "user", "content": user_prompt}],
)
```

`user_prompt` is assembled as:

```
<candidate author="@handle" posted="2026-08-05T14:22Z">
{post text}
</candidate>

<previously_surfaced>
[1] (2026-07-28, @other, similarity 0.81) {neighbour text}
[2] (2026-08-01, @another, similarity 0.74) {neighbour text}
...
</previously_surfaced>
```

The stable system prompt sits behind a `cache_control` breakpoint and the volatile
candidate goes in the user turn — so the system prompt is a cache read on every call after
the first, at ~10% of input price.

### Cost model

Assumes 25 accounts, ~250 posts/day collected, ~70 reaching Stage 3, ~1.5K input and 300
output tokens per call.

| Model | Input $/MTok | Output $/MTok | ≈ $/day | ≈ $/month |
|---|---:|---:|---:|---:|
| `claude-opus-5` | $5 | $25 | $1.05 | ~$32 |
| `claude-sonnet-5` | $3 | $15 | $0.63 | ~$19 |
| `claude-haiku-4-5` | $1 | $5 | $0.21 | ~$6 |

Prompt caching on the system prompt cuts the input side substantially once the cache is warm.

Recommendation: **start on `claude-opus-5`** and treat its judgments as the reference. Once
you have a few hundred logged judgments you trust, replay them through a cheaper model
offline and compare verdicts. If a smaller model agrees on ≥90% of them, switch and keep the
saved money. Picking the cheap model first means you never find out what you were missing.

### Tuning loop

This is the part that makes or breaks the tool. **Log every judgment, including skips, with
its rationale** (`judgements` table). Once a week:

```
tracker review --since 7d --verdict skip --min-value 3
```

Read the near-misses. Every time you disagree, that's a prompt fix — usually a missing
definition of what "valuable" means *to you specifically*. Bump `prompt_version` on each
change so old judgments stay attributable to the prompt that produced them.

Your definition of valuable is personal and won't survive first contact with a generic
prompt. Budget two or three tuning passes before the digest feels right.

---

## 8. Stage 4 — Digest

Ranking: `value` desc, then `novelty` desc, then recency. Cap at 15 items. Group by
`category`. Render:

```markdown
# AI Signal — Wednesday 5 Aug 2026
_247 posts scanned · 68 judged · 11 surfaced_

## Research
**@handle** · 2h ago · novelty 4 · value 5
> post text, quoted

Why: one-line rationale from the judge.
[link](https://x.com/handle/status/…)
```

Delivery: write `digests/YYYY-MM-DD.md`, then optionally POST to a Discord webhook or send
via SMTP. Both are config-gated and off by default.

---

## 9. Configuration

`config.toml`, checked into the repo with secrets in `.env`:

```toml
[collect]
list_url          = "https://x.com/i/lists/1234567890"
profile_dir       = "./.browser-profile"
max_scrolls       = 15
overlap_posts     = 10

[triage]
min_length            = 120
ai_centroid_threshold = 0.35
keyword_allowlist     = ["RL", "eval", "fine-tun", "inference", "context window", "RLHF"]

[novelty]
window_days       = 45
neighbours_k      = 8
duplicate_at      = 0.92
clearly_novel_at  = 0.60

[judge]
model            = "claude-opus-5"
prompt_version   = "v1"
min_novelty      = 3
min_value        = 4
daily_call_cap   = 200          # hard stop, guards against a runaway scrape

[digest]
max_items        = 15
discord_webhook  = ""           # empty = disabled
```

---

## 10. CLI

```
tracker collect                 # Stage 0. Idempotent. Safe to run any time.
tracker process                 # Stages 1-3 over anything not yet judged.
tracker digest [--date]         # Stage 4. Render + deliver.
tracker run                     # collect && process && digest
tracker review [--since] [...]  # Inspect judgments for prompt tuning.
tracker replay --model X        # Re-judge a date range with a different model/prompt.
tracker accounts add|rm|list
tracker doctor                  # Login valid? DB writable? API key set? Last run OK?
```

Cron:

```
17 6,12,18 * * *  cd /path/to/tracker && ./venv/bin/tracker collect >> logs/collect.log 2>&1
40 18     * * *   cd /path/to/tracker && ./venv/bin/tracker process && ./venv/bin/tracker digest
```

Jitter is applied inside `collect`, not by cron.

---

## 11. Build order

Each milestone is independently useful — you can stop after any of them and still have
something that works.

| # | Milestone | Deliverable | Proves |
|---|---|---|---|
| 1 | **Scraper spike** | Playwright script dumps intercepted list-timeline JSON to stdout | The whole approach is viable. Do this before anything else. |
| 2 | **Persistence** | `collect` writes to SQLite, idempotent, survives reruns | Data layer is sound |
| 3 | **Triage** | Stage 1 filters + `--dry-run` showing what got dropped and why | Heuristics are tuned before spending on the model |
| 4 | **Novelty index** | Embeddings + kNN + duplicate suppression, no LLM yet | Dedup works standalone — already useful |
| 5 | **Judge** | Stage 3 + `judgements` logging | End-to-end pipeline |
| 6 | **Digest** | Markdown render + delivery | Daily habit |
| 7 | **Tuning tools** | `review`, `replay`, prompt versioning | Makes the judge yours |

Milestone 1 is a hard gate. If intercepting the GraphQL timeline turns out not to work
reliably, the collection strategy changes and parts of §5 get rewritten — but nothing
downstream of Stage 0 is affected.

---

## 12. Open questions

- **Threads.** A 12-post thread is one idea, not twelve. v1 concatenates by
  `conversation_id` when the tracked account is the sole author. Needs validation against
  real data — threads posted over hours may arrive split across collection runs.
- **Quote-tweets.** The quoted post carries most of the meaning. Include it in the
  embedded and judged text, or judge the comment alone? Leaning include-both.
- **Cold start.** With an empty corpus, everything looks novel for the first ~2 weeks.
  Options: backfill 30 days of history at setup, or suppress the digest until the corpus
  has ~500 posts. Backfill is better if the scraper can reach that far back.
- **Link-only posts.** "great paper: <arxiv link>" carries no text signal. Fetching and
  summarizing the linked page is a clear v2 feature; out of scope for v1.
- **Embedding model quality.** `bge-small` may not separate closely-related AI concepts
  well enough for the 0.60–0.92 band to be meaningful. Measure before optimizing —
  if the bands are noisy, upgrade to a larger local model or Voyage before touching prompts.
