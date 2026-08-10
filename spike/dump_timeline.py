#!/usr/bin/env python3
"""
Milestone 1 spike: prove we can intercept X's GraphQL timeline responses.

This is throwaway code. Its only job is to answer one question: can we reliably
pull structured post data out of X's internal API responses, without parsing the
DOM? If yes, the rest of the pipeline in SPEC.md is viable. If no, the collection
strategy changes.

Usage:
    ./dump_timeline.py --login
        Opens a real browser. Log in by hand, then press Enter here. The session
        is saved to .browser-profile/ and reused by every later run.

    ./dump_timeline.py --url https://x.com/i/lists/123456
        Loads the timeline, scrolls a few times, saves every intercepted GraphQL
        response to spike/out/, and prints what it managed to parse.

    ./dump_timeline.py --url ... --scrolls 8 --verbose
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
PROFILE_DIR = ROOT.parent / ".browser-profile"
OUT_DIR = ROOT / "out"

# The hash in a GraphQL URL rotates when X ships frontend changes, so match on
# the operation name only. We capture *every* graphql response and record its
# operation name, so that when the expected one stops appearing we can see what
# replaced it instead of just getting zero results.
GRAPHQL_RE = re.compile(r"/i/api/graphql/[^/]+/(?P<op>[A-Za-z0-9_]+)")

# Operations known to carry timeline entries. Anything else is saved but not parsed.
TIMELINE_OPS = {
    "ListLatestTweetsTimeline",  # a List's chronological timeline — what we want
    "UserTweets",                # a single profile's timeline
    "UserTweetsAndReplies",
    "HomeLatestTimeline",
    "HomeTimeline",
}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Parsing
#
# We deliberately do NOT hardcode the instructions[] path. It differs between
# list / user / home timelines and X reshuffles it periodically. Instead we walk
# the whole response and pick up anything that looks like a tweet node. Slower,
# but it survives payload reshuffling, which is the entire point of this spike.
# --------------------------------------------------------------------------

def walk(node, found: list) -> None:
    """Recursively collect dicts that look like a tweet result node."""
    if isinstance(node, dict):
        typename = node.get("__typename")
        if typename == "Tweet" and "legacy" in node:
            found.append(node)
        elif typename == "TweetWithVisibilityResults" and isinstance(node.get("tweet"), dict):
            found.append(node["tweet"])
        for value in node.values():
            walk(value, found)
    elif isinstance(node, list):
        for item in node:
            walk(item, found)


def extract_author(tweet: dict) -> tuple[str, str]:
    """Return (handle, display_name). X has moved these fields around; try both homes."""
    user = (
        tweet.get("core", {})
        .get("user_results", {})
        .get("result", {})
    )
    # Newer payloads promote screen_name/name to user.core; older ones keep them in legacy.
    core = user.get("core", {})
    legacy = user.get("legacy", {})
    handle = core.get("screen_name") or legacy.get("screen_name") or "?"
    name = core.get("name") or legacy.get("name") or ""
    return handle, name


def extract_text(tweet: dict) -> str:
    """Prefer note_tweet (long posts, untruncated) over legacy.full_text."""
    note = (
        tweet.get("note_tweet", {})
        .get("note_tweet_results", {})
        .get("result", {})
        .get("text")
    )
    if note:
        return note
    return tweet.get("legacy", {}).get("full_text", "")


def parse_tweet(tweet: dict) -> dict | None:
    legacy = tweet.get("legacy") or {}
    post_id = tweet.get("rest_id") or legacy.get("id_str")
    if not post_id:
        return None
    handle, name = extract_author(tweet)
    return {
        "id": str(post_id),
        "author_handle": handle,
        "author_name": name,
        "text": extract_text(tweet),
        "created_at": legacy.get("created_at"),
        "conversation_id": legacy.get("conversation_id_str"),
        "reply_to_id": legacy.get("in_reply_to_status_id_str"),
        "is_retweet": bool(legacy.get("retweeted_status_result")),
        "quoted_id": legacy.get("quoted_status_id_str"),
        "urls": [u.get("expanded_url") for u in legacy.get("entities", {}).get("urls", [])],
    }


# --------------------------------------------------------------------------
# Browser driving
# --------------------------------------------------------------------------

def goto_with_retry(page, url: str, attempts: int = 4) -> None:
    """Navigate, retrying transient network errors.

    VMs in particular throw ERR_NETWORK_CHANGED / ERR_CONNECTION_RESET when the
    virtual NIC renegotiates. Those are momentary and clear on a retry; letting
    them abort a whole scrape run would be silly.
    """
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            return
        except Exception as exc:
            last_error = exc
            first_line = str(exc).splitlines()[0]
            if attempt == attempts:
                break
            backoff = 2 * attempt
            log(f"  navigation attempt {attempt}/{attempts} failed ({first_line})")
            log(f"  retrying in {backoff}s...")
            time.sleep(backoff)
    raise RuntimeError(f"could not load {url} after {attempts} attempts") from last_error


def open_context(playwright, headless: bool):
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=headless,
        viewport={"width": 1280, "height": 900},
        args=["--disable-blink-features=AutomationControlled"],
    )


def do_login() -> int:
    log(f"Opening a browser using profile: {PROFILE_DIR}")
    log("Log into the burner account in the window, then come back here.")
    with sync_playwright() as p:
        context = open_context(p, headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            # Land on x.com itself and sign in from there. /login redirects into
            # a flow URL that doesn't always render cleanly on a cold profile.
            goto_with_retry(page, "https://x.com/")
        except RuntimeError as exc:
            # Not fatal: the window is open, so navigate by hand and continue.
            log(f"WARNING: {exc}")
            log("The browser window is still open — type x.com into the address bar yourself.")
        input("\n>>> Press Enter here once you are logged in and see your timeline... ")
        try:
            goto_with_retry(page, "https://x.com/home")
            page.wait_for_timeout(3000)
            logged_in = "/login" not in page.url and "/i/flow" not in page.url
        except RuntimeError:
            # Never claim success we did not observe. An unverified session is a
            # failure here: reporting it as saved sends the user off to debug the
            # wrong thing later.
            log("ERROR: could not load x.com/home to verify the session.")
            logged_in = False
        context.close()
    if logged_in:
        log("Session verified and saved. Future runs will reuse it.")
        return 0
    log("Session NOT saved — you will need to run --login again.")
    return 1


def do_dump(url: str, scrolls: int, headless: bool, verbose: bool) -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = OUT_DIR / stamp
    run_dir.mkdir(parents=True, exist_ok=True)

    captured: list[dict] = []   # {"op":..., "path":..., "body":...}
    op_counts: dict[str, int] = {}

    with sync_playwright() as p:
        context = open_context(p, headless=headless)
        page = context.pages[0] if context.pages else context.new_page()

        def on_response(response):
            match = GRAPHQL_RE.search(response.url)
            if not match:
                return
            op = match.group("op")
            op_counts[op] = op_counts.get(op, 0) + 1
            try:
                body = response.json()
            except Exception as exc:  # non-JSON or already-consumed body
                if verbose:
                    log(f"  [skip] {op}: {exc}")
                return
            index = len(captured)
            path = run_dir / f"{index:03d}-{op}.json"
            path.write_text(json.dumps(body, indent=2, ensure_ascii=False))
            captured.append({"op": op, "path": path, "body": body})
            if verbose:
                log(f"  [capture] {op} -> {path.name}")

        page.on("response", on_response)

        log(f"Navigating to {url}")
        try:
            goto_with_retry(page, url)
        except RuntimeError as exc:
            log(f"ERROR: {exc}")
            context.close()
            return 1
        page.wait_for_timeout(4000)

        if "/login" in page.url or "/i/flow/login" in page.url:
            log("ERROR: redirected to login. Run with --login first.")
            context.close()
            return 1

        for i in range(scrolls):
            page.mouse.wheel(0, 2400)
            delay = random.uniform(1.2, 2.6)
            log(f"  scroll {i + 1}/{scrolls} (waiting {delay:.1f}s)")
            time.sleep(delay)

        page.wait_for_timeout(2000)
        context.close()

    # ---- report -----------------------------------------------------------
    print("\n" + "=" * 72)
    print("GRAPHQL OPERATIONS SEEN")
    print("=" * 72)
    if not op_counts:
        print("  (none — interception did not fire at all)")
    for op, count in sorted(op_counts.items(), key=lambda kv: -kv[1]):
        marker = " <-- timeline op" if op in TIMELINE_OPS else ""
        print(f"  {count:3d}x  {op}{marker}")

    posts: dict[str, dict] = {}
    for item in captured:
        if item["op"] not in TIMELINE_OPS:
            continue
        nodes: list = []
        walk(item["body"], nodes)
        for node in nodes:
            parsed = parse_tweet(node)
            if parsed:
                posts.setdefault(parsed["id"], parsed)

    print("\n" + "=" * 72)
    print(f"PARSED {len(posts)} UNIQUE POSTS from {len(captured)} captured responses")
    print("=" * 72)
    for post in list(posts.values())[:25]:
        text = " ".join(post["text"].split())
        flags = "".join(
            [
                "R" if post["is_retweet"] else "-",
                "Q" if post["quoted_id"] else "-",
                "T" if post["reply_to_id"] else "-",
            ]
        )
        print(f"\n[{flags}] {post['id']}  @{post['author_handle']}  {post['created_at']}")
        print(f"      {text[:180]}{'…' if len(text) > 180 else ''}")

    if len(posts) > 25:
        print(f"\n  ... and {len(posts) - 25} more")

    summary = run_dir / "_parsed.json"
    summary.write_text(json.dumps(list(posts.values()), indent=2, ensure_ascii=False))
    print(f"\nRaw responses: {run_dir}")
    print(f"Parsed posts:  {summary}")
    print("\nFlags: R=retweet Q=quote-tweet T=reply")

    return 0 if posts else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--login", action="store_true", help="interactive login, then save the session")
    parser.add_argument("--url", help="timeline URL to load (list, profile, or home)")
    parser.add_argument("--scrolls", type=int, default=5)
    parser.add_argument("--headless", action="store_true", help="not recommended: easier to detect")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.login:
        return do_login()
    if not args.url:
        parser.error("give --url, or --login for first-time setup")
    return do_dump(args.url, args.scrolls, args.headless, args.verbose)


if __name__ == "__main__":
    sys.exit(main())
