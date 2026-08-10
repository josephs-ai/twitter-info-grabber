"""Stage 0 — collection. Drives a browser, intercepts GraphQL, writes to SQLite."""

from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from . import db, parse

ROOT = Path(__file__).resolve().parent.parent
PROFILE_DIR = ROOT / ".browser-profile"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def goto_with_retry(page, url: str, attempts: int = 4) -> None:
    """Retry transient navigation errors before giving up.

    ERR_NETWORK_CHANGED fires when a network interface appears or disappears —
    on this host that was caused by container churn. Harmless once, fatal to an
    unattended run if unhandled.
    """
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            return
        except Exception as exc:
            last = exc
            if attempt == attempts:
                break
            backoff = 2 * attempt
            log(f"  navigation attempt {attempt}/{attempts} failed ({str(exc).splitlines()[0][:70]})")
            time.sleep(backoff)
    raise RuntimeError(f"could not load {url} after {attempts} attempts") from last


def open_context(playwright, headless: bool):
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=headless,
        viewport={"width": 1280, "height": 900},
    )


def login(headless: bool = False) -> int:
    log(f"Opening browser with profile: {PROFILE_DIR}")
    log("Sign in to the burner account in the window, then return here.")
    with sync_playwright() as p:
        context = open_context(p, headless)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            goto_with_retry(page, "https://x.com/")
        except RuntimeError as exc:
            log(f"WARNING: {exc}")
            log("The window is open — navigate to x.com manually.")

        input("\n>>> Press Enter (and nothing else) once you are logged in... ")

        try:
            goto_with_retry(page, "https://x.com/home")
            page.wait_for_timeout(3000)
            ok = "/login" not in page.url and "/i/flow" not in page.url
        except RuntimeError:
            # Never report success we did not observe.
            ok = False
        context.close()

    if ok:
        log("Session verified and saved.")
        return 0
    log("Session NOT saved — run login again.")
    return 1


def collect_all(conn, max_scrolls: int = 6, overlap_target: int = 5,
                headless: bool = True, pause: float = 8.0, limit: int | None = None) -> int:
    """Collect from every tracked account in turn.

    A single List timeline would be far cheaper — one page load instead of N —
    but it needs manual setup on X first. This works today with no setup, and
    switching to a List later changes only which URL gets passed to collect().

    Paced deliberately: this is the highest-volume browsing the tool does, and
    the burner account is the thing most worth protecting.
    """
    from . import accounts as accounts_mod

    handles = accounts_mod.active_handles(conn)
    if limit:
        handles = handles[:limit]
    log(f"Collecting from {len(handles)} accounts")

    ok = failed = 0
    for i, handle in enumerate(handles, 1):
        log(f"\n[{i}/{len(handles)}] @{handle}")
        try:
            rc = collect(f"https://x.com/{handle}", conn, max_scrolls=max_scrolls,
                         overlap_target=overlap_target, headless=headless)
            ok += (rc == 0)
            failed += (rc != 0)
        except Exception as exc:
            failed += 1
            log(f"  error: {str(exc).splitlines()[0][:70]}")
        if i < len(handles):
            time.sleep(pause + random.uniform(0, 4))

    log(f"\n{ok} accounts collected, {failed} failed")
    return 0 if ok else 1


def collect(
    url: str,
    conn,
    max_scrolls: int = 15,
    overlap_target: int = 10,
    headless: bool = False,
    jitter: bool = True,
) -> int:
    """Scroll a timeline until we have seen enough already-known posts.

    Stopping on overlap (rather than a fixed scroll count) means routine runs are
    cheap — a few scrolls when little is new — while a run after a long gap keeps
    paging until it reconnects with what we already have.
    """
    run_id = db.start_run(conn, url)
    responses = 0
    all_posts: dict[str, dict] = {}
    raw_by_id: dict[str, dict] = {}
    consecutive_known = 0
    scrolls_done = 0

    try:
        with sync_playwright() as p:
            context = open_context(p, headless)
            page = context.pages[0] if context.pages else context.new_page()

            def on_response(response):
                nonlocal responses
                op = parse.operation_name(response.url)
                if op not in parse.TIMELINE_OPS:
                    return
                try:
                    body = response.json()
                except Exception:
                    return
                responses += 1
                for post in parse.posts_from_response(body):
                    all_posts.setdefault(post["id"], post)
                    raw_by_id.setdefault(post["id"], {"op": op})

            page.on("response", on_response)

            log(f"Loading {url}")
            goto_with_retry(page, url)
            page.wait_for_timeout(4000)

            if "/login" in page.url or "/i/flow/login" in page.url:
                raise RuntimeError("redirected to login — session expired, run: tracker login")

            for i in range(max_scrolls):
                before = len(all_posts)
                page.mouse.wheel(0, 2400)
                delay = random.uniform(1.2, 2.6) if jitter else 0.8
                time.sleep(delay)
                scrolls_done = i + 1

                consecutive_known = len(db.known_ids(conn, list(all_posts.keys())))
                gained = len(all_posts) - before

                log(f"  scroll {scrolls_done}/{max_scrolls}: {len(all_posts)} posts "
                    f"(+{gained}), {consecutive_known} already known")

                if consecutive_known >= overlap_target and gained == 0:
                    log("  overlap reached — stopping early")
                    break

            page.wait_for_timeout(1500)
            context.close()

        seen, inserted = db.upsert_posts(conn, list(all_posts.values()), raw_by_id)
        status = "ok" if seen else "empty"
        db.finish_run(conn, run_id, responses=responses, posts_seen=seen,
                      posts_new=inserted, scrolls=scrolls_done, status=status)

        log(f"\n{seen} posts seen, {inserted} new, {seen - inserted} already had")
        if not seen:
            log("WARNING: captured nothing. Session expired, or the timeline op was renamed.")
            return 1
        return 0

    except Exception as exc:
        db.finish_run(conn, run_id, status="error", error=str(exc)[:500],
                      scrolls=scrolls_done, responses=responses)
        log(f"ERROR: {exc}")
        return 1
