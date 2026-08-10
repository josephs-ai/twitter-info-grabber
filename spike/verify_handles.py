#!/usr/bin/env python3
"""Check that candidate X handles actually exist, and show who they are.

Handing someone a curated list containing invented handles is worse than handing
them nothing, so every suggestion gets verified against the live site first.
Reads the UserByScreenName GraphQL response, which carries name, bio, and
follower count without needing to parse the DOM.
"""

from __future__ import annotations

import re
import sys
import time
from urllib.parse import unquote
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from playwright.sync_api import sync_playwright  # noqa: E402
from tracker.collect import PROFILE_DIR, goto_with_retry  # noqa: E402

CANDIDATES = ["DrJimFan", "danshipper", "paulg", "levelsio", "emollick", "karpathy"]


def main() -> int:
    results: dict[str, dict] = {}

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR), headless=True,
            viewport={"width": 1280, "height": 900},
        )
        page = context.pages[0] if context.pages else context.new_page()

        def on_response(response):
            if "UserByScreenName" not in response.url:
                return
            # Read the handle out of the request URL, never from a shared
            # variable: responses arrive asynchronously, so a loop pointer has
            # usually moved on by the time this fires and every result lands
            # against the wrong person.
            match = re.search(r"screen_name%22%3A%22([^%]+)%22", unquote(response.url)) \
                or re.search(r'screen_name"\s*:\s*"([^"]+)"', unquote(response.url))
            if not match:
                return
            handle = match.group(1)
            try:
                body = response.json()
            except Exception:
                return
            user = (body.get("data") or {}).get("user") or {}
            result = user.get("result")
            if not result or result.get("__typename") == "UserUnavailable":
                results[handle] = {"exists": False}
                return
            core = result.get("core", {})
            legacy = result.get("legacy", {})
            results[handle] = {
                "exists": True,
                "name": core.get("name") or legacy.get("name") or "",
                "bio": (legacy.get("description") or "").replace("\n", " ")[:110],
                "followers": legacy.get("followers_count"),
            }

        page.on("response", on_response)

        for i, handle in enumerate(CANDIDATES, 1):
            try:
                goto_with_retry(page, f"https://x.com/{handle}", attempts=2)
                page.wait_for_timeout(6000)
            except Exception:
                results.setdefault(handle, {"exists": False})
            print(f"  [{i}/{len(CANDIDATES)}] @{handle}", file=sys.stderr, flush=True)
            time.sleep(4.0)

        context.close()

    print("\n" + "=" * 78)
    lower = {h.lower(): r for h, r in results.items()}
    results = {c: lower[c.lower()] for c in CANDIDATES if c.lower() in lower}
    live = [(h, r) for h, r in results.items() if r.get("exists")]
    dead = [h for h, r in results.items() if not r.get("exists")]
    missing = [h for h in CANDIDATES if h not in results]

    live.sort(key=lambda kv: -(kv[1].get("followers") or 0))
    for handle, info in live:
        followers = info.get("followers")
        count = f"{followers:>9,}" if followers else "        ?"
        print(f"OK   {count}  @{handle:<18} {info['name']}")
        if info["bio"]:
            print(f"                        {info['bio']}")

    for handle in dead:
        print(f"DEAD            -  @{handle}")
    for handle in missing:
        print(f"???             -  @{handle}  (no response captured)")

    print("=" * 78)
    print(f"{len(live)} verified, {len(dead)} dead, {len(missing)} unknown")
    return 0


if __name__ == "__main__":
    sys.exit(main())
