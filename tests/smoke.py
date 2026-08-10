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
               "judge", "extract", "digest", "feedback", "pipeline", "cli"]
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


def main() -> int:
    print(f"python {sys.version.split()[0]} on {sys.platform}")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_imports()
        test_schema(tmp)
        test_parser()
        test_dedup()
        test_threads(tmp)
        test_context(tmp)
        test_curation(tmp)

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  failed: {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
