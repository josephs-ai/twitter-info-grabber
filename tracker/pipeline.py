"""The full daily cycle, in Python so it runs anywhere.

daily.sh does the same thing but only on a Unix shell — and it used `date -Is`,
which is GNU-only and fails on macOS. This is the portable path: same stages,
same order, no shell.

Every stage is idempotent, so the whole thing is safe to re-run and safe to
schedule. A stage that fails is logged and the run continues: a broken judge
should not stop collection, since collection is the part that cannot be
back-filled later.
"""

from __future__ import annotations

import os
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from . import paths


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(message: str) -> None:
    print(f"[{_stamp()}] {message}", flush=True)


# A scheduler gives the process a bare environment, so .env matters most here —
# but it is loaded for every entry point now, at package import.
load_env = paths.load_env


def run(conn, skip: set[str] | None = None) -> int:
    from . import amplify, collect, curate, digest, extract, judge, links, notify
    from . import novelty, replies, schedule, sources, suggest, threads

    load_env()
    skip = skip or set()
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if not has_key:
        log("ANTHROPIC_API_KEY unset — judge and extract will be skipped")

    # These two were settings in name only: the Schedule page saved them and
    # nothing ever read them, so the one dial that matters — how much of the
    # queue a run gets through — silently did nothing.
    settings = schedule.load(conn)
    judge_limit = int(settings.get("judge_limit", 60))
    scrolls = int(settings.get("collect_scrolls", 5))
    admit_age = int(settings.get("admit_max_age_days", 3))
    log(f"collect scrolls={scrolls}, judge limit={judge_limit}")

    stages: list[tuple[str, callable]] = [
        ("collect",  lambda: collect.collect_all(conn, max_scrolls=scrolls,
                                                 overlap_target=4, headless=True)),
        # Feeds, papers and forums: no browser, no login, nothing to violate.
        # Runs after X so a slow browser sweep never delays the cheap sources.
        ("sources",  lambda: log(str(sources.fetch(conn, limit=40)))),
        ("replies",  lambda: replies.mine(conn, limit=4, scrolls=3)),
        ("suggest",  lambda: suggest.harvest(conn, limit_seeds=2, scrolls=5)),
        ("links",    lambda: links.resolve(conn, limit=40)),
        # After discovery (so there are candidates to weigh) and before judging
        # (so a newly tracked account's posts are scored this run).
        ("curate",   lambda: curate.run(conn, dry_run=False)),
        ("amplify",  lambda: (amplify.backfill_retweet_targets(conn),
                              amplify.recompute(conn), amplify.promote(conn))),
        ("threads",  lambda: threads.apply(conn, dry_run=False)),
        ("dedup",    lambda: (novelty.embed_pending(conn),
                              novelty.scan(conn, apply=True))),
        ("judge",    lambda: judge.run(conn, limit=judge_limit,
                                        admit_max_age_days=admit_age)),
        # Extraction only runs on what cleared the bar, so it scales with the
        # judging rate rather than being a fixed number.
        ("extract",  lambda: extract.run(conn, limit=max(15, judge_limit // 4))),
        ("digest",   lambda: digest.build(conn)),
        # Last, so it announces only what the rest of the run actually produced.
        ("notify",   lambda: notify.deliver(conn)),
    ]

    started = time.time()
    failed = []
    for name, fn in stages:
        if name in skip:
            log(f"{name}: skipped")
            continue
        if name in ("judge", "extract") and not has_key:
            continue
        log(f"{name}: starting")
        try:
            fn()
        except Exception as exc:
            failed.append(name)
            log(f"{name}: FAILED — {exc}")
            traceback.print_exc()

    mins = (time.time() - started) / 60
    if failed:
        log(f"finished in {mins:.1f} min with failures: {', '.join(failed)}")
        return 1
    log(f"finished in {mins:.1f} min")
    return 0
