-- Raw capture. Append-only: nothing here is rewritten once inserted, so a
-- parser bug can be fixed and replayed against raw_json without re-scraping.
CREATE TABLE IF NOT EXISTS posts (
    id              TEXT PRIMARY KEY,
    author_handle   TEXT NOT NULL,
    author_name     TEXT,
    text            TEXT NOT NULL,
    created_at      TEXT NOT NULL,          -- ISO 8601 UTC
    fetched_at      TEXT NOT NULL,          -- first time we saw it
    conversation_id TEXT,
    reply_to_id     TEXT,
    is_retweet      INTEGER NOT NULL DEFAULT 0,
    quoted_id       TEXT,
    urls            TEXT,                   -- JSON array of expanded URLs

    -- How this post entered the DB:
    --   timeline = it was an entry on the timeline we polled
    --   embedded = it was nested inside another post (retweet original / quoted)
    -- Embedded posts are kept for context but are not "posts by a tracked
    -- account", so triage treats them differently.
    capture_source  TEXT NOT NULL DEFAULT 'timeline',

    -- Thread stitching. A self-thread is one idea split across N posts, so it
    -- must be judged and deduped as ONE unit or it floods the digest with N
    -- near-identical entries. The earliest post is the root; every member
    -- points at it, and thread_size is set on the root only.
    retweet_of_id   TEXT,
    thread_root_id  TEXT,
    thread_size     INTEGER,

    raw_json        TEXT,
    deleted_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at);
CREATE INDEX IF NOT EXISTS idx_posts_author  ON posts(author_handle);
CREATE INDEX IF NOT EXISTS idx_posts_source  ON posts(capture_source);

-- Pipeline state, separate from raw capture so reprocessing never risks the
-- source data. One row per post.
CREATE TABLE IF NOT EXISTS triage (
    post_id         TEXT PRIMARY KEY REFERENCES posts(id),
    stage           TEXT NOT NULL,          -- collected|triaged|judged|published|dropped
    drop_reason     TEXT,
    ai_score        REAL,
    max_similarity  REAL,
    nearest_post_id TEXT,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_triage_stage ON triage(stage);

-- One row per (post, model): the pipeline stores a lexical AND a semantic
-- vector for every post, because neither backend dominates the other.
-- Lexical wins on typos and copy-paste; semantic wins on paraphrase.
CREATE TABLE IF NOT EXISTS embeddings (
    post_id   TEXT NOT NULL REFERENCES posts(id),
    model     TEXT NOT NULL,
    dim       INTEGER NOT NULL,
    vector    BLOB NOT NULL,
    PRIMARY KEY (post_id, model)
);

CREATE TABLE IF NOT EXISTS judgements (
    id              INTEGER PRIMARY KEY,
    post_id         TEXT NOT NULL REFERENCES posts(id),
    model           TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    verdict         TEXT NOT NULL,
    novelty         INTEGER,
    value           INTEGER,
    category        TEXT,
    rationale       TEXT,
    neighbours      TEXT,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_judgements_post ON judgements(post_id);

CREATE TABLE IF NOT EXISTS digests (
    id           INTEGER PRIMARY KEY,
    generated_at TEXT NOT NULL,
    post_ids     TEXT NOT NULL,
    markdown     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    handle       TEXT PRIMARY KEY,
    added_at     TEXT NOT NULL,
    notes        TEXT,
    active       INTEGER NOT NULL DEFAULT 1,
    category     TEXT,
    note         TEXT,
    harvested_at TEXT          -- last time we pulled this account's following list
);

-- Audit trail for collection runs. Makes "did the scraper silently stop
-- working?" answerable without reading logs.
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    url           TEXT,
    responses     INTEGER DEFAULT 0,
    posts_seen    INTEGER DEFAULT 0,
    posts_new     INTEGER DEFAULT 0,
    scrolls       INTEGER DEFAULT 0,
    status        TEXT,                     -- ok|empty|error
    error         TEXT
);

-- Candidate accounts discovered by harvesting who the seeds follow.
-- seed_count is the ranking signal: how many tracked accounts follow this
-- person. Followed by 20 of your seeds is a far better relevance signal than
-- anything a model could guess.
CREATE TABLE IF NOT EXISTS candidates (
    handle      TEXT PRIMARY KEY,
    name        TEXT,
    bio         TEXT,
    seed_count  INTEGER NOT NULL DEFAULT 0,
    followed_by TEXT,                       -- JSON array of seed handles
    discovered_at TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'new', -- new|approved|rejected

    -- Second, independent discovery signal: how many times this person showed
    -- up replying under a tracked account's post. Follow-graph presence is a
    -- one-off endorsement, possibly years stale; reply presence means they are
    -- in the conversation now.
    reply_count INTEGER NOT NULL DEFAULT 0,
    replied_under TEXT                       -- JSON array of seed handles
);
CREATE INDEX IF NOT EXISTS idx_candidates_rank ON candidates(seed_count DESC);

-- Your verdict on the judge's verdict. This is the only place the system learns
-- what YOU consider valuable, as opposed to what a generic prompt assumes.
-- Ratings become few-shot examples in the judge prompt, so disagreeing with it
-- is how you train it.
CREATE TABLE IF NOT EXISTS feedback (
    post_id    TEXT PRIMARY KEY REFERENCES posts(id),
    rating     TEXT NOT NULL,            -- good | bad
    note       TEXT,                     -- optional: why
    created_at TEXT NOT NULL
);

-- Content behind links in posts. "great paper: <arxiv link>" carries no text
-- signal at all until the link is resolved.
CREATE TABLE IF NOT EXISTS links (
    url         TEXT PRIMARY KEY,
    title       TEXT,
    summary     TEXT,
    site        TEXT,
    status      TEXT,                    -- ok | error | skipped
    fetched_at  TEXT NOT NULL
);

-- Images attached to posts. A large share of AI Twitter is screenshots of
-- results, charts, and code — invisible to a text-only pipeline.
CREATE TABLE IF NOT EXISTS media (
    id          TEXT PRIMARY KEY,        -- media key from X
    post_id     TEXT NOT NULL REFERENCES posts(id),
    url         TEXT NOT NULL,
    kind        TEXT,                    -- photo | video | animated_gif
    alt_text    TEXT,
    description TEXT,                    -- model-generated, once analysed
    analysed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_media_post ON media(post_id);

-- Structured facts pulled out of a post, so the digest can tell you WHAT
-- happened instead of handing you the raw text to read yourself.
-- One row per post per prompt version, mirroring judgements.
CREATE TABLE IF NOT EXISTS extractions (
    id             INTEGER PRIMARY KEY,
    post_id        TEXT NOT NULL REFERENCES posts(id),
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    headline       TEXT,     -- one sentence: what is actually new here
    claims         TEXT,     -- JSON array of specific factual claims
    entities       TEXT,     -- JSON array of {name, kind}
    numbers        TEXT,     -- JSON array of concrete figures with units
    so_what        TEXT,     -- why a practitioner should care
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_extractions_post ON extractions(post_id);
CREATE INDEX IF NOT EXISTS idx_extractions_ver  ON extractions(prompt_version);
