#!/usr/bin/env python3
"""Smoke tests: the things most likely to break across platforms.

A real file rather than inline `python -c` in the workflow, because quoting
inside YAML behaves differently in bash and PowerShell — the first version of
this suite passed on Linux and macOS and failed on Windows purely on escaping.
As a file it is shell-agnostic, and you can run it locally:

    python tests/smoke.py
"""

from __future__ import annotations

import importlib
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASSED = 0
FAILED: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ok    {label} {detail}")
    else:
        FAILED.append(label)
        print(f"  FAIL  {label} {detail}")


def test_imports() -> None:
    print("\nmodules import")
    modules = ["db", "parse", "accounts", "collect", "context", "threads",
               "embed", "novelty", "amplify", "links", "replies", "suggest",
               "judge", "extract", "digest", "feedback", "pipeline", "cli",
               "cluster", "health", "notify", "search", "strictness", "curate",
               "paths", "onboard", "schedule", "app", "platforms", "sources"]
    for name in modules:
        try:
            importlib.import_module(f"tracker.{name}")
            ok = True
        except Exception as exc:  # noqa: BLE001 - want the message
            ok = False
            print(f"        {exc}")
        check(f"tracker.{name}", ok)


def test_schema(tmp: Path) -> None:
    print("\nschema and migrations")
    from tracker import db

    conn = db.connect(tmp / "a.db")
    names = sorted(r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'"))
    check("tables created", len(names) >= 11, f"({len(names)})")
    conn.close()

    # Reconnecting re-runs the schema and the migrations; both must be no-ops.
    again = db.connect(tmp / "a.db")
    check("reconnect is idempotent", True)
    cols = {r["name"] for r in again.execute("PRAGMA table_info(posts)")}
    check("migrated columns present",
          {"thread_root_id", "retweet_of_id", "amplifiers"} <= cols)
    again.close()


def test_parser() -> None:
    print("\nparser")
    from tracker import parse

    body = {"data": {"x": {"instructions": [{"entries": [{"content": {"itemContent": {
        "tweet_results": {"result": {
            "__typename": "Tweet", "rest_id": "1",
            "core": {"user_results": {"result": {
                "core": {"screen_name": "a", "name": "A"}}}},
            "legacy": {"full_text": "hello world",
                       "created_at": "Sun Aug 02 03:00:09 +0000 2026",
                       "entities": {}}}}}}}]}]}}}
    posts = parse.posts_from_response(body)
    check("one post parsed", len(posts) == 1)
    check("author read", posts and posts[0]["author_handle"] == "a")
    check("timestamp normalised to ISO",
          posts and posts[0]["created_at"].startswith("2026-08-02"))


def test_dedup() -> None:
    print("\ndedup")
    from tracker import embed, novelty

    lexical = embed.BACKENDS[0]
    base = "RL fine-tuning shows diminishing returns past 3 epochs"
    repost = "@someone " + base + " https://t.co/abc"
    check("repost matches original",
          float(lexical.encode(base) @ lexical.encode(repost)) >= 0.92)
    check("unrelated text scores low",
          float(lexical.encode(base) @ lexical.encode("carbonara recipe")) < 0.30)
    check("link-only post has no signal",
          not embed.has_signal("RT @a: https://t.co/x"))
    check("real post has signal", embed.has_signal(base))
    check("0.95 is a duplicate", novelty.classify(0.95) == "duplicate")
    check("0.75 is ambiguous", novelty.classify(0.75) == "ambiguous")
    check("0.30 is novel", novelty.classify(0.30) == "novel")


def test_multilingual() -> None:
    print("\nnon-latin text")
    from tracker import embed

    # The word-token guard read every CJK post as empty, so a dense Chinese
    # post was binned as "link only" before it reached dedup or the judge.
    check("Chinese text has signal",
          embed.has_signal("英伟达发布了新的H200芯片，显存带宽提升了40%"))
    check("Japanese text has signal", embed.has_signal("日本語のテキストもここにあります"))
    check("Korean text has signal", embed.has_signal("한국어 텍스트도 포함됩니다"))
    check("a single character still does not", not embed.has_signal("好"))
    check("a bare link still does not", not embed.has_signal("RT @a: https://t.co/x"))
    check("English is unaffected", embed.has_signal("Nvidia released the H200"))


def test_platform_urls() -> None:
    print("\nplatform addresses")
    from tracker import platforms

    x = {"platform": "x", "author_handle": "karpathy", "id": "123", "url": None}
    check("X posts still build the old URL",
          platforms.post_url(x) == "https://x.com/karpathy/status/123")
    check("and keep the @", platforms.byline(x) == "@karpathy")

    blog = {"platform": "rss", "author_handle": "OpenAI", "id": "rss:1",
            "url": "https://openai.com/index/thing/"}
    check("a stored URL wins", platforms.post_url(blog) == "https://openai.com/index/thing/")
    check("a blog byline has no @", platforms.byline(blog) == "OpenAI")
    check("the source is named", platforms.label("hn") == "Hacker News")

    hn = {"platform": "hn", "author_handle": "pg", "id": "hn:1", "url": None}
    check("a platform with no stored URL still derives one",
          "news.ycombinator.com" in platforms.post_url(hn))


def test_sources(tmp: Path) -> None:
    print("\nsources")
    from tracker import db
    from tracker import sources
    from tracker.sources import hackernews, rss

    atom = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <title>Example Lab</title>
      <entry>
        <id>tag:example,2026:1</id>
        <title>We trained a model</title>
        <link rel="alternate" href="https://example.com/post"/>
        <published>2026-08-01T10:00:00Z</published>
        <summary>&lt;p&gt;It went &lt;b&gt;well&lt;/b&gt;.&lt;/p&gt;</summary>
      </entry>
    </feed>"""
    posts = rss.parse(atom)
    check("atom parses", len(posts) == 1, f"({len(posts)})")
    check("markup is stripped", posts and "<b>" not in posts[0]["text"])
    check("title leads the text", posts and posts[0]["text"].startswith("We trained a model"))
    check("the link is kept", posts and posts[0]["url"] == "https://example.com/post")
    check("dates normalise", posts and posts[0]["created_at"].startswith("2026-08-01"))

    rss2 = """<?xml version="1.0"?><rss><channel><title>Blog</title>
      <item><title>A post</title><link>https://b.example/1</link>
      <guid>https://b.example/1</guid><description>Body text</description>
      <pubDate>Fri, 01 Aug 2026 10:00:00 GMT</pubDate></item></channel></rss>"""
    check("rss 2.0 parses too", len(rss.parse(rss2)) == 1)

    check("an off-topic HN title is filtered", not hackernews.relevant("Best sourdough recipe"))
    check("an AI title is kept", hackernews.relevant("Show HN: a faster LLM inference engine"))

    conn = db.connect(tmp / "src.db")
    seen, added = sources.store(conn, posts)
    check("a source post is stored", added == 1, f"({added})")
    again = sources.store(conn, posts)[1]
    check("and storing twice adds nothing", again == 0, f"({again})")
    row = conn.execute("SELECT platform, url FROM posts WHERE id=?",
                       (posts[0]["id"],)).fetchone()
    check("with its platform recorded", row["platform"] == "rss")
    queued = conn.execute("SELECT stage FROM triage WHERE post_id=?",
                          (posts[0]["id"],)).fetchone()
    check("and queued for triage like anything else", queued["stage"] == "collected")
    conn.close()


def test_threads(tmp: Path) -> None:
    print("\nthread stitching")
    from tracker import db, threads

    conn = db.connect(tmp / "t.db")

    def add(pid, who, conv, when, text):
        conn.execute(
            "INSERT INTO posts (id, author_handle, author_name, text, created_at, "
            "fetched_at, conversation_id, is_retweet, capture_source, urls) "
            "VALUES (?,?,'',?,?,?,?,0,'timeline','[]')",
            (pid, who, text, when, when, conv))
        conn.execute(
            "INSERT INTO triage (post_id, stage, updated_at) VALUES (?,'collected',?)",
            (pid, when))

    add("1", "a", "c1", "2026-08-01T00:00:00", "1/2 first part")
    add("2", "a", "c1", "2026-08-01T00:01:00", "2/2 second part")
    add("3", "b", "c2", "2026-08-01T01:00:00", "a claim about scaling")
    add("4", "c", "c2", "2026-08-01T01:05:00", "someone else disagreeing")
    conn.commit()

    result = threads.apply(conn, dry_run=False)
    check("one self-thread found", result["threads"] == 1, f"({result['threads']})")
    check("continuation folded", result["posts_folded"] == 1)
    check("multi-person discussion left alone",
          conn.execute("SELECT thread_root_id FROM posts WHERE id='3'"
                       ).fetchone()[0] is None)
    stitched = threads.text_for(conn, "1", "")
    check("stitched in order and numbering stripped",
          "first part" in stitched and "second part" in stitched
          and "1/2" not in stitched)
    conn.close()


def test_context(tmp: Path) -> None:
    print("\ncontext assembly")
    from tracker import context, db

    conn = db.connect(tmp / "c.db")
    conn.execute(
        "INSERT INTO posts (id, author_handle, author_name, text, created_at, "
        "fetched_at, is_retweet, capture_source, urls) "
        "VALUES ('9','a','','plain post','2026-08-01T00:00:00','2026-08-01T00:00:00',"
        "0,'timeline','[]')")
    conn.commit()
    view = context.describe(conn, "9", "plain post")
    check("describe returns a view", view.get("text") == "plain post")
    check("no quoted post when absent", view.get("quoted") is None)
    check("amplifier count defaults to zero", view.get("amplifiers") == 0)
    conn.close()


def test_curation(tmp: Path) -> None:
    print("\ncuration rails")
    from tracker import curate, db

    conn = db.connect(tmp / "cur.db")
    old, new = "2026-01-01T00:00:00+00:00", "2026-08-09T00:00:00+00:00"

    def account(handle, added):
        conn.execute("INSERT INTO accounts (handle, added_at, active) VALUES (?,?,1)",
                     (handle, added))

    def judged(handle, count, value, surfaced=0):
        for i in range(count):
            pid = f"{handle}{i}"
            conn.execute(
                "INSERT INTO posts (id, author_handle, author_name, text, created_at,"
                " fetched_at, is_retweet, capture_source, urls) "
                "VALUES (?,?,'','t',?,?,0,'timeline','[]')", (pid, handle, old, old))
            conn.execute(
                "INSERT INTO judgements (post_id, model, prompt_version, verdict,"
                " novelty, value, created_at) VALUES (?,'m','v',?,2,?,?)",
                (pid, "surface" if i < surfaced else "skip", value, old))

    account("noisy", old); judged("noisy", 20, 1)
    account("good", old); judged("good", 20, 4, surfaced=5)
    account("thin", old); judged("thin", 3, 1)
    account("newbie", new); judged("newbie", 20, 1)
    conn.commit()

    demoted = {a["handle"] for a in curate.run(conn, dry_run=True, force=True)["demoted"]}
    check("bad long record is demoted", "noisy" in demoted)
    check("productive account spared", "good" not in demoted)
    check("thin record spared", "thin" not in demoted)
    check("account inside grace period spared", "newbie" not in demoted)

    curate.run(conn, dry_run=False, force=True)
    active = {r["handle"] for r in conn.execute("SELECT handle FROM accounts WHERE active=1")}
    check("apply deactivates", "noisy" not in active)
    curate.undo(conn)
    restored = {r["handle"] for r in conn.execute("SELECT handle FROM accounts WHERE active=1")}
    check("undo restores", "noisy" in restored)
    conn.close()


def test_ssrf_guard() -> None:
    print("\nlink safety")
    from tracker import links

    for url in ["http://127.0.0.1:8080/admin", "http://169.254.169.254/latest/meta-data/",
                "http://localhost/x", "http://192.168.1.1/", "http://[::1]/",
                "file:///etc/passwd", "ftp://example.com/x"]:
        check(f"blocks {url[:38]}", not links.is_safe_url(url))
    check("allows a normal https url", links.is_safe_url("https://example.com/"))


def test_prune_stability(tmp: Path) -> None:
    print("\nembedding prune")
    from tracker import db, novelty

    conn = db.connect(tmp / "p.db")
    for i, age in enumerate([1, 5, 400, 500]):
        conn.execute(
            "INSERT INTO posts (id, author_handle, author_name, text, created_at,"
            " fetched_at, is_retweet, capture_source, urls) VALUES (?,?,'',?,"
            f"datetime('now','-{age} days'), datetime('now'), 0, 'timeline','[]')",
            (str(i), "a", f"a post about scaling laws number {i}"))
    conn.commit()

    novelty.embed_pending(conn)
    inside = conn.execute("SELECT COUNT(DISTINCT post_id) n FROM embeddings").fetchone()["n"]
    check("only in-window posts embedded", inside == 2, f"({inside} of 4)")

    # prune then embed must reach a fixed point, or the two churn every run
    novelty.prune(conn)
    again = novelty.embed_pending(conn)
    removed = novelty.prune(conn)["removed"]
    check("prune and embed reach a fixed point", again == 0 and removed == 0,
          f"(re-embedded {again}, re-pruned {removed})")
    conn.close()


def _seed_judged(conn, pid: str, days_old: int, novelty: int, value: int,
                 handle: str = "a", text: str = "some text here", verdict: str = "skip"):
    """A post that survived triage, plus a judgement on it."""
    stamp = f"datetime('now', '-{days_old} days')"
    conn.execute(
        f"INSERT INTO posts (id, author_handle, author_name, text, created_at,"
        f" fetched_at, is_retweet, capture_source, urls) "
        f"VALUES (?,?,'',?,{stamp},{stamp},0,'timeline','[]')", (pid, handle, text))
    conn.execute(
        f"INSERT INTO triage (post_id, stage, updated_at, triaged_at) "
        f"VALUES (?,'triaged',{stamp},{stamp})", (pid,))
    if novelty:
        conn.execute(
            f"INSERT INTO judgements (post_id, model, prompt_version, verdict,"
            f" novelty, value, created_at) VALUES (?,?,?,?,?,?,{stamp})",
            (pid, judge_model(), judge_version(), verdict, novelty, value))


def judge_model() -> str:
    from tracker import judge
    return judge.MODEL


def judge_version() -> str:
    from tracker import judge
    return judge.PROMPT_VERSION


def test_judge_queue(tmp: Path) -> None:
    print("\njudge queue")
    from tracker import db, judge

    conn = db.connect(tmp / "queue.db")
    for i in range(40):                       # fresh
        _seed_judged(conn, f"new{i}", 1, 0, 0)
    for i in range(40):                       # behind, but still judgeable
        _seed_judged(conn, f"old{i}", 30, 0, 0)
    for i in range(5):                        # scrolled-up history
        _seed_judged(conn, f"ancient{i}", 400, 0, 0)
    conn.commit()

    retired = judge.retire_stale(conn)
    check("posts past the window are retired", retired == 5, f"({retired})")
    reason = conn.execute("SELECT drop_reason FROM triage WHERE post_id='ancient0'"
                          ).fetchone()["drop_reason"]
    check("retirement keeps a reason", reason == "stale", f"({reason})")

    waiting = judge.backlog(conn)["waiting"]
    check("backlog counts only judgeable posts", waiting == 80, f"({waiting})")

    picked = judge.pending(conn, 20)
    ages = [p["id"][:3] for p in picked]
    check("queue is not newest-only", "old" in ages)
    check("queue still favours fresh posts", ages.count("new") > ages.count("old"),
          f"({ages.count('new')} new / {ages.count('old')} old)")
    check("queue respects the limit", len(picked) == 20, f"({len(picked)})")
    conn.close()


def test_extract_follows_bar(tmp: Path) -> None:
    print("\nextraction follows the strictness bar")
    from tracker import db, extract, strictness

    conn = db.connect(tmp / "ex.db")
    _seed_judged(conn, "hi", 1, 4, 5, verdict="surface")
    _seed_judged(conn, "mid", 1, 2, 3, verdict="skip")
    conn.commit()

    strictness.save(conn, "strict")
    ids = {p["id"] for p in extract.pending(conn, 10)}
    check("strict bar extracts only the top post", ids == {"hi"}, f"({ids})")

    strictness.save(conn, "permissive")
    ids = {p["id"] for p in extract.pending(conn, 10)}
    check("loosening the bar pulls in what the feed now shows", ids == {"hi", "mid"},
          f"({ids})")
    conn.close()


def test_cluster() -> None:
    print("\nstory clustering")
    from tracker import cluster

    day = "2026-08-10T00:00:00+00:00"
    far = "2026-06-10T00:00:00+00:00"
    items = [
        {"id": "1", "author_handle": "vendor", "created_at": day,
         "headline": "Acme released Muse 30B, an open-weight model under Apache 2.0"},
        {"id": "2", "author_handle": "runtime", "created_at": day,
         "headline": "Muse 30B is out from Acme, open weights, Apache 2.0 licensed"},
        {"id": "3", "author_handle": "other", "created_at": day,
         "headline": "A study finds that sparse attention degrades long-context recall"},
        {"id": "4", "author_handle": "late", "created_at": far,
         "headline": "Acme released Muse 30B, an open-weight model under Apache 2.0"},
    ]
    groups = cluster.group(items)
    sizes = sorted(g["size"] for g in groups)
    check("independent reports of one story merge", sizes[-1] >= 2, f"(sizes {sizes})")
    check("an unrelated story stays separate", len(groups) >= 2, f"({len(groups)})")
    lead = next(g for g in groups if g["size"] >= 2)
    check("the lead is the first-ranked member", lead["lead"]["id"] == "1")
    check("every member is kept",
          sum(g["size"] for g in groups) == len(items))
    check("a repeat months later is a different story",
          not any("late" in g["sources"] and "vendor" in g["sources"] for g in groups))


def test_notify(tmp: Path) -> None:
    print("\nnotification watermark")
    from tracker import db, notify, strictness

    conn = db.connect(tmp / "n.db")
    strictness.save(conn, "strict")
    _seed_judged(conn, "a", 1, 4, 5, handle="one", text="first finding here")
    _seed_judged(conn, "b", 1, 4, 5, handle="two", text="second finding here")
    conn.commit()

    settings = notify.save(conn, {"desktop": False, "webhook_url": ""})
    check("channels off by default in this test", settings["desktop"] is False)

    first = notify.deliver(conn)
    check("finds what cleared the bar", first["found"] == 2, f"({first['found']})")
    again = notify.deliver(conn)
    check("does not re-announce", again["found"] == 0, f"({again['found']})")

    _seed_judged(conn, "c", 1, 4, 5, handle="three", text="third finding here")
    conn.commit()
    third = notify.deliver(conn)
    check("announces what is genuinely new", third["found"] == 1, f"({third['found']})")
    conn.close()


def test_search(tmp: Path) -> None:
    print("\nsearch")
    from tracker import db, novelty, search

    conn = db.connect(tmp / "s.db")
    _seed_judged(conn, "p1", 1, 3, 4, text="speculative decoding doubles throughput")
    _seed_judged(conn, "p2", 1, 3, 4, text="a recipe for sourdough bread starter")
    # Never judged — most of the corpus is in this state, so excluding it would
    # hide most of what search exists for.
    _seed_judged(conn, "p3", 1, 0, 0, text="notes on speculative decoding kernels")
    conn.commit()
    novelty.embed_pending(conn)

    rows = search.run(conn, "speculative decoding", limit=5)
    hits = [r["id"] for r in rows]
    check("the exact phrase is found", "p1" in hits, f"({hits})")
    check("unrelated text is excluded", "p2" not in hits, f"({hits})")
    check("unjudged posts are searchable", "p3" in hits, f"({hits})")
    check("unjudged results carry no scores",
          all(r["novelty"] is None for r in rows if r["id"] == "p3"))
    check("an empty query returns nothing", search.run(conn, "  ") == [])
    conn.close()


def test_read_state(tmp: Path) -> None:
    print("\nread state")
    from tracker import db

    conn = db.connect(tmp / "reads.db")
    _seed_judged(conn, "r1", 1, 4, 5)
    conn.commit()
    conn.execute("INSERT INTO reads (post_id, seen_at) VALUES ('r1', ?)", (db.now(),))
    conn.commit()
    seen = conn.execute("SELECT 1 FROM reads WHERE post_id='r1'").fetchone()
    check("a post can be marked seen", seen is not None)
    conn.execute("INSERT INTO reads (post_id, seen_at) VALUES ('r1', ?) "
                 "ON CONFLICT(post_id) DO NOTHING", (db.now(),))
    n = conn.execute("SELECT COUNT(*) c FROM reads").fetchone()["c"]
    check("marking twice is idempotent", n == 1, f"({n})")
    conn.close()


def test_throughput(tmp: Path) -> None:
    print("\nschedule throughput")
    from tracker import db, schedule

    conn = db.connect(tmp / "thr.db")
    for i in range(70):
        _seed_judged(conn, f"t{i}", 1, 0, 0)
    conn.commit()

    # 70 posts triaged over the 7-day window is 10 a day arriving; one run at 5.
    slow = schedule._throughput(conn, {"times": ["07:00"], "judge_limit": 5,
                                       "days": "everyday"})
    check("a schedule below the arrival rate is flagged",
          slow["keeping_up"] is False,
          f"({slow['judged_per_day']}/day vs {slow['arriving_per_day']})")

    fast = schedule._throughput(conn, {"times": ["07:00", "19:00"],
                                       "judge_limit": 120, "days": "everyday"})
    check("more runs and a bigger limit keep up", fast["keeping_up"] is True,
          f"({fast['judged_per_day']}/day)")
    check("cost tracks the judging rate",
          fast["cost_per_day"] > slow["cost_per_day"])
    check("weekdays-only counts as fewer runs",
          schedule._throughput(conn, {"times": ["07:00"], "judge_limit": 100,
                                      "days": "weekdays"})["judged_per_day"] < 100)
    conn.close()


def test_digest_clusters(tmp: Path) -> None:
    print("\ndigest clustering")
    from tracker import db, digest, strictness

    conn = db.connect(tmp / "dig.db")
    strictness.save(conn, "strict")
    shared = "Acme released Muse 30B, an open-weight model under Apache 2.0"
    _seed_judged(conn, "d1", 0, 4, 5, handle="vendor", text=shared)
    _seed_judged(conn, "d2", 0, 4, 5, handle="runtime",
                 text="Muse 30B is out from Acme, open weights, Apache 2.0 licensed")
    _seed_judged(conn, "d3", 0, 4, 5, handle="other",
                 text="Sparse attention degrades long-context recall, a study finds")
    conn.execute("UPDATE judgements SET category='research'")
    conn.commit()

    items = digest.gather(conn, since_hours=48, limit=20)
    check("the digest groups by story too", len(items) == 2, f"({len(items)})")
    lead = next((i for i in items if i.get("also")), None)
    check("the fold is recorded on the lead", lead is not None)
    markdown = digest.render(items, digest.counts(conn, 48), "Today", False)
    check("corroborating accounts are named", "Also reported by" in markdown)
    check("and counted", "reported by 2 accounts" in markdown)
    conn.close()


def test_arrival_rate(tmp: Path) -> None:
    print("\narrival rate")
    from tracker import db, schedule

    conn = db.connect(tmp / "arr.db")
    for i in range(20):
        _seed_judged(conn, f"a{i}", 2, 0, 0)
    conn.execute("UPDATE triage SET triaged_at = datetime('now', '-2 days')")
    conn.commit()

    before = schedule._throughput(conn, {"times": ["07:00"], "judge_limit": 60,
                                         "days": "everyday"})["arriving_per_day"]
    # A rescan rewrites updated_at for the whole backlog. Reading that as
    # arrivals is what produced a fictional "186 a day".
    conn.execute("UPDATE triage SET updated_at = datetime('now')")
    conn.commit()
    after = schedule._throughput(conn, {"times": ["07:00"], "judge_limit": 60,
                                        "days": "everyday"})["arriving_per_day"]
    check("a rescan does not inflate the arrival rate", before == after,
          f"({before} then {after})")

    fresh = db.connect(tmp / "arr2.db")
    none_yet = schedule._throughput(fresh, {"times": ["07:00"], "judge_limit": 60,
                                            "days": "everyday"})
    check("no measurements reports unknown, not 'keeping up'",
          none_yet["keeping_up"] is None and none_yet["arriving_per_day"] is None)
    fresh.close()
    conn.close()


def test_feed_window(tmp: Path) -> None:
    print("\nfeed window and archive")
    from tracker import db, strictness
    from tracker.app import Api

    path = tmp / "feed.db"
    conn = db.connect(path)
    strictness.save(conn, "strict")
    _seed_judged(conn, "fresh", 1, 4, 5, handle="a", text="a finding from today")
    _seed_judged(conn, "old", 30, 4, 5, handle="b", text="a finding from last month")
    conn.commit()
    conn.close()

    api = Api()
    api._conn = lambda: db.connect(path)

    check("a short window hides old posts",
          {i["id"] for i in api.feed("surfaced", 20, "new", 7)} == {"fresh"})
    check("a long window shows both",
          {i["id"] for i in api.feed("surfaced", 20, "new", 0)} == {"fresh", "old"})

    api.archive(["old"])
    check("archived posts leave the feed",
          {i["id"] for i in api.feed("surfaced", 20, "new", 0)} == {"fresh"})
    check("and are findable on the shelf",
          {i["id"] for i in api.feed("surfaced", 20, "new", 0, True)} == {"old"})
    api.archive(["old"], undo=True)
    check("restoring puts them back",
          {i["id"] for i in api.feed("surfaced", 20, "new", 0)} == {"fresh", "old"})

    n = api.archive_older_than(7)
    check("bulk archive clears the back of the feed", n == 1, f"({n})")
    check("and leaves the front alone",
          {i["id"] for i in api.feed("surfaced", 20, "new", 0)} == {"fresh"})


def test_paths() -> None:
    print("\npaths")
    import os

    from tracker import paths

    check("source checkout keeps code and data together",
          paths.code_dir() == paths.data_dir())
    check("the schema ships with the code", (paths.code_dir() / "schema.sql").exists())
    check("self_command targets this interpreter",
          paths.self_command()[:1] == [sys.executable])
    check("source builds invoke the module", "-m" in paths.self_command())

    # An override has to win, or a packaged build could never be pointed at a
    # different corpus — and the tests could not check it without side effects.
    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        os.environ["AI_SIGNAL_HOME"] = td
        try:
            check("AI_SIGNAL_HOME overrides the data directory",
                  paths.data_dir() == Path(td))
        finally:
            del os.environ["AI_SIGNAL_HOME"]


def test_onboarding(tmp: Path) -> None:
    print("\nfirst run")
    from tracker import db, onboard

    conn = db.connect(tmp / "onb.db")
    state = onboard.state(conn)
    check("an empty database is not set up", state["done"] is False)
    check("all four steps are reported", len(state["steps"]) == 4,
          f"({len(state['steps'])})")
    check("a garbage key is refused",
          onboard.save_api_key("hunter2")["ok"] is False)
    check("an empty key is refused", onboard.save_api_key("")["ok"] is False)

    conn.execute("INSERT INTO accounts (handle, added_at, active) VALUES ('x', ?, 1)",
                 (db.now(),))
    conn.commit()
    step = next(s for s in onboard.state(conn)["steps"] if s["key"] == "accounts")
    check("adding an account completes its step", step["done"] is True)
    conn.close()


def main() -> int:
    print(f"python {sys.version.split()[0]} on {sys.platform}")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_imports()
        test_schema(tmp)
        test_parser()
        test_dedup()
        test_multilingual()
        test_platform_urls()
        test_sources(tmp)
        test_threads(tmp)
        test_context(tmp)
        test_curation(tmp)
        test_ssrf_guard()
        test_prune_stability(tmp)
        test_judge_queue(tmp)
        test_extract_follows_bar(tmp)
        test_cluster()
        test_notify(tmp)
        test_search(tmp)
        test_read_state(tmp)
        test_throughput(tmp)
        test_arrival_rate(tmp)
        test_feed_window(tmp)
        test_digest_clusters(tmp)
        test_paths()
        test_onboarding(tmp)

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  failed: {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
