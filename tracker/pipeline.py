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


def load_env() -> None:
    """Read .env, because a scheduler gives the process a bare environment.

    Nothing fancy: KEY=value, ignore blanks and comments, never overwrite a
    variable that is already set.
    """
    path = paths.data_dir() / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def run(conn, skip: set[str] | None = None) -> int:
    from . import amplify, collect, curate, digest, extract, judge, links, notify
    from . import novelty, replies, suggest, threads

    load_env()
    skip = skip or set()
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if not has_key:
        log("ANTHROPIC_API_KEY unset — judge and extract will be skipped")

    stages: list[tuple[str, callable]] = [
        ("collect",  lambda: collect.collect_all(conn, max_scrolls=5,
                                                 overlap_target=4, headless=True)),
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
        ("judge",    lambda: judge.run(conn, limit=60)),
        ("extract",  lambda: extract.run(conn, limit=15)),
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
