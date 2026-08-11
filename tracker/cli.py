"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys

from . import accounts as accounts_mod
from . import amplify as amplify_mod
from . import collect as collect_mod
from . import curate as curate_mod
from . import db
from . import digest as digest_mod
from . import extract as extract_mod
from . import feedback as feedback_mod
from . import health as health_mod
from . import judge as judge_mod
from . import links as links_mod
from . import notify as notify_mod
from . import novelty as novelty_mod
from . import pipeline as pipeline_mod
from . import platforms as platforms_mod
from . import replies as replies_mod
from . import schedule as schedule_mod
from . import search as search_mod
from . import suggest as suggest_mod
from . import threads as threads_mod


def cmd_login(args) -> int:
    if args.wait:
        return collect_mod.login_wait()
    return collect_mod.login(headless=args.headless)


def cmd_collect(args) -> int:
    conn = db.connect(args.db)
    try:
        if args.all:
            return collect_mod.collect_all(
                conn, max_scrolls=args.scrolls, overlap_target=args.overlap,
                headless=args.headless, limit=args.max_accounts)
        if not args.url:
            print("give --url, or --all to sweep every tracked account")
            return 1
        return collect_mod.collect(
            args.url, conn,
            max_scrolls=args.scrolls,
            overlap_target=args.overlap,
            headless=args.headless,
            jitter=not args.no_jitter,
        )
    finally:
        conn.close()


def cmd_stats(args) -> int:
    conn = db.connect(args.db)
    try:
        s = db.stats(conn)
        print(f"posts            {s['posts_total']}  "
              f"({s['posts_timeline']} timeline, {s['posts_embedded']} embedded)")
        print(f"authors          {s['authors']}")
        print(f"date range       {s['oldest'] or '-'}  ->  {s['newest'] or '-'}")
        print(f"collection runs  {s['runs']} (last: {s['last_run'] or '-'})")

        rows = conn.execute(
            "SELECT author_handle, COUNT(*) n FROM posts WHERE capture_source='timeline' "
            "GROUP BY author_handle ORDER BY n DESC LIMIT 10"
        ).fetchall()
        if rows:
            print("\ntop authors (timeline posts only):")
            for r in rows:
                print(f"  {r['n']:5d}  @{r['author_handle']}")
        return 0
    finally:
        conn.close()


def cmd_doctor(args) -> int:
    """The same checks the app's status strip shows — one implementation."""
    conn = db.connect(args.db)
    try:
        marks = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}
        checks = health_mod.report(conn)
        for check in checks:
            print(f"{marks[check['level']]} {check['label']}: {check['detail']}")
            if check["fix"]:
                print(f"       -> {check['fix']}")
        s = db.stats(conn)
        print(f"OK   Database: {args.db} ({s['posts_total']} posts)")
        return 1 if health_mod.worst(checks) == "fail" else 0
    finally:
        conn.close()


def cmd_accounts(args) -> int:
    conn = db.connect(args.db)
    try:
        if args.action == "import":
            total, added = accounts_mod.import_seeds(conn)
            print(f"{total} in seeds.txt, {added} newly added")
        elif args.action == "list":
            rows = conn.execute(
                "SELECT handle, category, note, harvested_at FROM accounts "
                "WHERE active=1 ORDER BY category, handle").fetchall()
            for r in rows:
                mark = "h" if r["harvested_at"] else " "
                print(f" {mark} @{r['handle']:<20} {r['category'] or '':<14} {r['note'] or ''}")
            print(f"\n{len(rows)} active ('h' = following list already harvested)")
        return 0
    finally:
        conn.close()


def cmd_suggest(args) -> int:
    conn = db.connect(args.db)
    try:
        return suggest_mod.harvest(conn, limit_seeds=args.seeds,
                                   scrolls=args.scrolls, headless=not args.headed)
    finally:
        conn.close()


def cmd_candidates(args) -> int:
    conn = db.connect(args.db)
    try:
        if args.approve:
            n = accounts_mod.approve(conn, [h.lstrip("@") for h in args.approve])
            print(f"{n} account(s) now tracked")
            return 0
        if args.by_replies:
            rows = conn.execute(
                "SELECT * FROM candidates WHERE reply_count >= ? AND status='new' "
                "ORDER BY reply_count DESC, handle LIMIT ?",
                (args.min_seeds, args.limit)).fetchall()
        else:
            rows = accounts_mod.top_candidates(conn, args.min_seeds, args.limit)
        if not rows:
            print("No candidates yet — run: ./run suggest")
            return 0
        for r in rows:
            print(f"  follows:{r['seed_count']:<3} replies:{r['reply_count']:<3} "
                  f"@{r['handle']:<20} {r['name'] or ''}")
            if r["bio"]:
                print(f"       {r['bio'][:100]}")
        signal = "seen replying under" if args.by_replies else "followed by"
        print(f"\n{len(rows)} candidates {signal} >= {args.min_seeds} tracked account(s)")
        print("approve with: ./run candidates --approve handle1 handle2 ...")
        return 0
    finally:
        conn.close()


def cmd_replies(args) -> int:
    conn = db.connect(args.db)
    try:
        return replies_mod.mine(conn, limit=args.limit, scrolls=args.scrolls,
                                headless=not args.headed)
    finally:
        conn.close()


def cmd_dedup(args) -> int:
    conn = db.connect(args.db)
    try:
        n = novelty_mod.embed_pending(conn)
        while n:
            print(f"embedded {n} posts")
            n = novelty_mod.embed_pending(conn)

        r = novelty_mod.scan(conn, window_days=args.window, duplicate_at=args.duplicate_at,
                             novel_at=args.novel_at, apply=args.apply)
        print(f"\nscanned {r['scanned']} posts against a corpus of {r['corpus']}")
        print(f"  duplicate  {r['duplicate']:5d}   (>= {args.duplicate_at})")
        print(f"  ambiguous  {r['ambiguous']:5d}   (needs the judge, with context)")
        print(f"  novel      {r['novel']:5d}   (< {args.novel_at})")
        print(f"  no text    {r['no_text']:5d}   (link/mention only — nothing to compare)")
        if r["examples"]:
            print("\nsample duplicates:")
            for ex in r["examples"]:
                print(f"  {ex['similarity']:.3f}  @{ex['author']}: {ex['text']}")
                print(f"         matched @{ex['nearest_author']}: {ex['nearest_text']}")
        if args.apply:
            pr = novelty_mod.prune(conn, window_days=args.window)
            if pr["removed"]:
                print(f"\npruned {pr['removed']} vectors older than "
                      f"{pr['cutoff_days']} days ({pr['remaining']} kept)")
        else:
            print("\n(dry run — nothing written. re-run with --apply to record)")
        return 0
    finally:
        conn.close()


def cmd_judge(args) -> int:
    conn = db.connect(args.db)
    try:
        return judge_mod.run(conn, limit=args.limit, model=args.model,
                             effort=args.effort, k=args.neighbours,
                             dry_run=args.dry_run)
    finally:
        conn.close()


def cmd_replay(args) -> int:
    conn = db.connect(args.db)
    try:
        return judge_mod.replay(conn, limit=args.limit, model=args.model,
                                effort=args.effort, version=args.version)
    finally:
        conn.close()


def cmd_rate(args) -> int:
    conn = db.connect(args.db)
    try:
        ok = feedback_mod.rate(conn, args.post_id, args.rating, args.note)
        if not ok:
            print(f"no post with id {args.post_id}")
            return 1
        s = feedback_mod.stats(conn)
        print(f"rated {args.post_id} as {args.rating}  "
              f"({s['good']} good / {s['bad']} bad, {s['disagreements']} disagreements)")
        return 0
    finally:
        conn.close()


def cmd_review(args) -> int:
    """Read what the judge decided — the loop that tunes the prompt."""
    conn = db.connect(args.db)
    try:
        sql = ("SELECT j.*, p.author_handle, p.text FROM judgements j "
               "JOIN posts p ON p.id = j.post_id WHERE 1=1")
        params = []
        if args.verdict:
            sql += " AND j.verdict = ?"; params.append(args.verdict)
        if args.min_value:
            sql += " AND j.value >= ?"; params.append(args.min_value)
        sql += " ORDER BY j.value DESC, j.novelty DESC LIMIT ?"
        params.append(args.limit)
        rows = conn.execute(sql, params).fetchall()
        if not rows:
            print("No judgements yet — run: ./run judge")
            return 0
        for r in rows:
            text = " ".join(r["text"].split())[:120]
            print(f"\n[{r['verdict']}] n{r['novelty']} v{r['value']} "
                  f"{r['category']} @{r['author_handle']}")
            print(f"  {text}")
            print(f"  why: {r['rationale']}")
            print(f"  id:  {r['post_id']}")
            if args.rate:
                ans = input("  rate [g]ood / [b]ad / [s]kip? ").strip().lower()
                if ans.startswith("g"):
                    feedback_mod.rate(conn, r["post_id"], "good",
                                      input("  why (optional): ").strip() or None)
                elif ans.startswith("b"):
                    feedback_mod.rate(conn, r["post_id"], "bad",
                                      input("  why (optional): ").strip() or None)
        print(f"\n{len(rows)} judgements")
        if args.rate:
            s = feedback_mod.stats(conn)
            print(f"feedback: {s['good']} good / {s['bad']} bad, "
                  f"{s['disagreements']} disagreements with the judge")
        return 0
    finally:
        conn.close()


def cmd_digest(args) -> int:
    conn = db.connect(args.db)
    try:
        md = digest_mod.build(conn, since_hours=args.since, limit=args.limit,
                              min_value=args.min_value, write=not args.stdout)
        if args.stdout:
            print(md)
        return 0
    finally:
        conn.close()


def cmd_threads(args) -> int:
    conn = db.connect(args.db)
    try:
        r = threads_mod.apply(conn, dry_run=not args.apply)
        print(f"{r['threads']} self-threads found, "
              f"{r['posts_folded']} continuation posts folded into roots")
        for ex in r["examples"]:
            print(f"\n  @{ex['author']} ({ex['size']} posts)")
            print(f"    {ex['preview']}")
        if not args.apply:
            print("\n(dry run — re-run with --apply)")
        return 0
    finally:
        conn.close()


def cmd_amplify(args) -> int:
    conn = db.connect(args.db)
    try:
        linked = amplify_mod.backfill_retweet_targets(conn)
        if linked:
            print(f"linked {linked} older retweets to their originals")
        r = amplify_mod.recompute(conn)
        print(f"{r['posts_with_amplifiers']} posts have >=1 tracked amplifier")
        for n, count in sorted(r["distribution"].items(), reverse=True):
            print(f"  {n} accounts shared it: {count} posts")
        promoted = amplify_mod.promote(conn)
        if promoted:
            print(f"\n{promoted} corroborated originals promoted back into the pipeline")
        rows = amplify_mod.top(conn, args.limit)
        if rows:
            print("\nmost corroborated:")
            for row in rows:
                text = " ".join(row["text"].split())[:80]
                print(f"  {row['amplifiers']}x  @{row['author_handle']:<16} {text}")
        return 0
    finally:
        conn.close()


def cmd_links(args) -> int:
    conn = db.connect(args.db)
    try:
        r = links_mod.resolve(conn, limit=args.limit)
        print(f"{r['attempted']} urls: {r.get('ok',0)} resolved, "
              f"{r.get('skipped',0)} skipped, {r.get('error',0)} failed, "
              f"{r.get('blocked',0)} blocked (private/loopback address)")
        rows = conn.execute(
            "SELECT site, title FROM links WHERE status='ok' AND title != '' "
            "ORDER BY fetched_at DESC LIMIT 6").fetchall()
        for row in rows:
            print(f"  {row['site'][:24]:<24} {row['title'][:60]}")
        return 0
    finally:
        conn.close()


def cmd_extract(args) -> int:
    conn = db.connect(args.db)
    try:
        return extract_mod.run(conn, limit=args.limit, effort=args.effort,
                               force=args.force)
    finally:
        conn.close()


def cmd_search(args) -> int:
    conn = db.connect(args.db)
    try:
        rows = search_mod.run(conn, " ".join(args.query), limit=args.limit)
        if not rows:
            print("nothing matched")
            return 0
        for row in rows:
            scored = (f"  [n{row['novelty']} v{row['value']}]"
                      if row["novelty"] is not None else "  [unjudged]")
            print(f"\n{platforms_mod.byline(row)}  {row['created_at'][:10]}{scored}")
            print("  " + " ".join((row["text"] or "").split())[:220])
            print(f"  {platforms_mod.post_url(row)}")
        print(f"\n{len(rows)} results")
        return 0
    finally:
        conn.close()


def cmd_notify(args) -> int:
    conn = db.connect(args.db)
    try:
        if args.webhook is not None or args.desktop is not None:
            settings = notify_mod.load(conn)
            if args.webhook is not None:
                settings["webhook_url"] = args.webhook
            if args.desktop is not None:
                settings["desktop"] = args.desktop == "on"
            notify_mod.save(conn, settings)

        settings = notify_mod.load(conn)
        print(f"desktop   {'on' if settings['desktop'] else 'off'}")
        print(f"webhook   {settings['webhook_url'] or '(none)'}")

        result = notify_mod.deliver(conn, dry_run=args.dry_run)
        print(f"\n{result['found']} undelivered")
        if result.get("title"):
            print(f"\n{result['title']}\n{result['body']}")
        if result["skipped"]:
            print(f"\nnot sent: {result['skipped']}")
        elif result["sent"]:
            print(f"\nsent via {', '.join(result['sent'])}")
        elif result["found"]:
            print("\nno channel delivered — is a notification daemon running?")
        return 0
    finally:
        conn.close()


def cmd_sources(args) -> int:
    """Feeds, papers and forums — everything that does not need a browser."""
    from . import sources as sources_mod
    from .sources import rss as rss_mod

    conn = db.connect(args.db)
    try:
        if args.add:
            rss_mod.add(conn, args.add, args.title)
            print(f"added {args.add}")
        if args.list:
            for f in rss_mod.listing(conn):
                mark = "  " if f["active"] else "off"
                note = f"  ERROR: {f['last_error'][:60]}" if f["last_error"] else ""
                print(f" {mark} {(f['title'] or '')[:28]:<28} {f['url']}{note}")
            return 0

        results = sources_mod.fetch(conn, args.only, limit=args.limit)
        for name, r in results.items():
            if "error" in r:
                print(f"  {name:<8} FAILED  {r['error']}")
            else:
                print(f"  {name:<8} {r['seen']:4d} seen, {r['new']:4d} new")
        return 0
    finally:
        conn.close()


def cmd_daily(args) -> int:
    conn = db.connect(args.db)
    try:
        return pipeline_mod.run(conn, skip=set(args.skip or []))
    finally:
        conn.close()


def cmd_app(args) -> int:
    from .app import main as app_main
    return app_main()


def cmd_schedule(args) -> int:
    conn = db.connect(args.db)
    try:
        if args.install or args.disable:
            settings = schedule_mod.load(conn)
            if args.times:
                settings["times"] = args.times
            if args.days:
                settings["days"] = args.days
            settings["enabled"] = not args.disable
            r = schedule_mod.install(conn, settings)
            print("installed" if r.get("ok") else f"failed: {r.get('error')}",
                  f"({r.get('entries', 0)} entries)")
        st = schedule_mod.status(conn)
        s = st["settings"]
        print(f"\nenabled   {s['enabled']}")
        print(f"times     {', '.join(s['times'])}  ({s['days']})")
        print(f"stages    {len(s['stages'])} of {len(schedule_mod.STAGE_INFO)}")
        print(f"installed {st['installed']} entries on {st['platform']}")
        print(f"command   {st['command']}")
        return 0
    finally:
        conn.close()


def cmd_curate(args) -> int:
    conn = db.connect(args.db)
    try:
        if args.undo:
            print(f"reversed {curate_mod.undo(conn)} automatic changes")
            return 0
        if args.history:
            for h in curate_mod.history(conn):
                mark = " (undone)" if h["undone"] else ""
                print(f"  {h['created_at'][:16]}  {h['action']:<8} @{h['handle']:<18} "
                      f"{h['reason']}{mark}")
            return 0

        r = curate_mod.run(conn, dry_run=not args.apply, force=args.force)
        for a in r["promoted"]:
            print(f"  + track  @{a['handle']:<18} {a['reason']}")
        for a in r["demoted"]:
            print(f"  - drop   @{a['handle']:<18} {a['reason']}")
        if not r["promoted"] and not r["demoted"]:
            s = r["settings"]
            print("Nothing meets the bar.")
            print(f"  promote at: {s['promote_min_follows']} follows "
                  f"or {s['promote_min_replies']} replies")
            print(f"  demote at:  {s['demote_min_judged']} judged, none surfaced, "
                  f"mean value <= {s['demote_max_mean_value']}")
            if not (r["settings"]["auto_promote"] or r["settings"]["auto_demote"] or args.force):
                print("  (auto-curation is off — use --force to preview anyway)")
        elif not args.apply:
            print("\n(dry run — re-run with --apply)")
        return 0
    finally:
        conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="tracker", description="AI signal tracker")
    parser.add_argument("--db", default=str(db.DEFAULT_DB))
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("login", help="interactive login; saves the browser session")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--wait", action="store_true",
                   help="detect sign-in from the browser instead of asking here "
                        "(used by the app, which has no terminal)")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("collect", help="scrape a timeline into the database")
    p.add_argument("--url", help="list or profile timeline URL")
    p.add_argument("--all", action="store_true",
                   help="sweep every tracked account instead of one URL")
    p.add_argument("--max-accounts", type=int, help="cap accounts per sweep")
    p.add_argument("--scrolls", type=int, default=15)
    p.add_argument("--overlap", type=int, default=10,
                   help="stop once this many already-known posts are on screen")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--no-jitter", action="store_true", help="disable human-like delays")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("daily", help="run the whole pipeline once (cross-platform)")
    p.add_argument("--skip", nargs="+", metavar="STAGE",
                   help="stage names to skip this run")
    p.set_defaults(func=cmd_daily)

    p = sub.add_parser("app", help="open the desktop app")
    p.set_defaults(func=cmd_app)

    p = sub.add_parser("curate", help="let the tracked list grow and prune itself")
    p.add_argument("--apply", action="store_true", help="make the changes")
    p.add_argument("--force", action="store_true", help="preview even when disabled")
    p.add_argument("--undo", action="store_true", help="reverse recent auto changes")
    p.add_argument("--history", action="store_true")
    p.set_defaults(func=cmd_curate)

    p = sub.add_parser("schedule", help="show or set when the pipeline runs")
    p.add_argument("--install", action="store_true", help="write it to cron/Task Scheduler")
    p.add_argument("--disable", action="store_true", help="remove the schedule")
    p.add_argument("--times", nargs="+", metavar="HH:MM")
    p.add_argument("--days", choices=["everyday", "weekdays"])
    p.set_defaults(func=cmd_schedule)

    p = sub.add_parser("stats", help="what is in the database")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("doctor", help="check session, database, and last run")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("accounts", help="manage tracked accounts")
    p.add_argument("action", choices=["import", "list"])
    p.set_defaults(func=cmd_accounts)

    p = sub.add_parser("suggest", help="harvest who tracked accounts follow")
    p.add_argument("--seeds", type=int, default=5, help="seeds to harvest this run")
    p.add_argument("--scrolls", type=int, default=6)
    p.add_argument("--headed", action="store_true", help="show the browser window")
    p.set_defaults(func=cmd_suggest)

    p = sub.add_parser("replies", help="mine conversations under tracked posts")
    p.add_argument("--limit", type=int, default=5, help="conversations per run")
    p.add_argument("--scrolls", type=int, default=3)
    p.add_argument("--headed", action="store_true")
    p.set_defaults(func=cmd_replies)

    p = sub.add_parser("links", help="resolve URLs so link-only posts carry signal")
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(func=cmd_links)

    p = sub.add_parser("amplify", help="count how many tracked accounts shared each post")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_amplify)

    p = sub.add_parser("threads", help="stitch self-threads into single posts")
    p.add_argument("--apply", action="store_true")
    p.set_defaults(func=cmd_threads)

    p = sub.add_parser("dedup", help="embed posts and find near-duplicates")
    p.add_argument("--window", type=int, default=novelty_mod.WINDOW_DAYS)
    p.add_argument("--duplicate-at", type=float, default=novelty_mod.DUPLICATE_AT)
    p.add_argument("--novel-at", type=float, default=novelty_mod.NOVEL_AT)
    p.add_argument("--apply", action="store_true", help="write results (default: dry run)")
    p.set_defaults(func=cmd_dedup)

    p = sub.add_parser("judge", help="score posts for novelty and value")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--model", default=judge_mod.MODEL)
    p.add_argument("--effort", default="medium",
                   choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--neighbours", type=int, default=5)
    p.add_argument("--dry-run", action="store_true",
                   help="show the prompt and count, make no API calls")
    p.set_defaults(func=cmd_judge)

    p = sub.add_parser("replay", help="re-judge posts under a new prompt version")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--model", default=judge_mod.MODEL)
    p.add_argument("--effort", default="medium")
    p.add_argument("--version", help="prompt version label (default: current)")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("review", help="read past judgements to tune the prompt")
    p.add_argument("--verdict", choices=["surface", "skip"])
    p.add_argument("--min-value", type=int)
    p.add_argument("--limit", type=int, default=15)
    p.add_argument("--rate", action="store_true", help="rate each item interactively")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("rate", help="record whether a surfaced post was any good")
    p.add_argument("post_id")
    p.add_argument("rating", choices=["good", "bad"])
    p.add_argument("--note", help="why — this text goes into the judge prompt")
    p.set_defaults(func=cmd_rate)

    p = sub.add_parser("extract", help="pull claims, numbers and entities from surfaced posts")
    p.add_argument("--limit", type=int, default=15)
    p.add_argument("--effort", default="medium")
    p.add_argument("--force", action="store_true", help="re-extract even if done")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("digest", help="render surfaced posts to Markdown")
    p.add_argument("--since", type=int, default=24, help="hours to look back")
    p.add_argument("--limit", type=int, default=15)
    p.add_argument("--min-value", type=int, help="override the judge's verdict")
    p.add_argument("--stdout", action="store_true", help="print instead of writing")
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser("candidates", help="review discovered accounts")
    p.add_argument("--min-seeds", type=int, default=2)
    p.add_argument("--limit", type=int, default=60)
    p.add_argument("--approve", nargs="+", metavar="HANDLE")
    p.add_argument("--by-replies", action="store_true",
                   help="rank by reply presence instead of follow graph")
    p.set_defaults(func=cmd_candidates)

    p = sub.add_parser("sources", help="feeds, papers and forums — no browser needed")
    p.add_argument("--only", nargs="+", metavar="NAME", help="rss, hn, arxiv")
    p.add_argument("--limit", type=int, default=40, help="items per source")
    p.add_argument("--add", metavar="URL", help="add an RSS/Atom feed")
    p.add_argument("--title", help="name for the feed being added")
    p.add_argument("--list", action="store_true", help="show configured feeds")
    p.set_defaults(func=cmd_sources)

    p = sub.add_parser("search", help="search everything collected, judged or not")
    p.add_argument("query", nargs="+")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("notify", help="announce new findings, and configure how")
    p.add_argument("--webhook", metavar="URL",
                   help="POST findings here (Slack/Discord compatible); '' to clear")
    p.add_argument("--desktop", choices=["on", "off"])
    p.add_argument("--dry-run", action="store_true", help="show the message, send nothing")
    p.set_defaults(func=cmd_notify)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
