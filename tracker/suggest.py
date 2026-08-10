"""Discover new accounts by harvesting who the tracked accounts follow.

Ranking by "how many of your seeds follow this person" beats any list a model
could generate from memory: it reflects who the frontline actually reads, it
cannot invent a handle that doesn't exist, and it surfaces niche researchers
nobody would think to name.

Harvesting is deliberately slow and resumable. Each seed's /following page is a
separate visit, so this is the most browsing-heavy thing the tool does — run it
in small batches rather than all at once.
"""

from __future__ import annotations

import re
import sys
import time
from urllib.parse import unquote

from playwright.sync_api import sync_playwright

from . import accounts as accounts_mod
from .collect import PROFILE_DIR, goto_with_retry, log

FOLLOWING_OP = "Following"
# The seed handle is in the request URL's variables blob; read it from there
# rather than a loop variable, because responses arrive asynchronously and a
# shared pointer attributes results to the wrong seed.
_USERID_RE = re.compile(r'userId"\s*:\s*"(\d+)"')


def _users_from_body(body) -> list[dict]:
    """Pull user nodes out of a Following response."""
    users: dict[str, dict] = {}

    def walk(node):
        if isinstance(node, dict):
            if node.get("__typename") == "User":
                core = node.get("core", {})
                legacy = node.get("legacy", {})
                handle = core.get("screen_name") or legacy.get("screen_name")
                if handle:
                    users[handle] = {
                        "handle": handle,
                        "name": core.get("name") or legacy.get("name") or "",
                        "bio": (legacy.get("description") or "").replace("\n", " ")[:200],
                    }
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(body)
    return list(users.values())


def harvest(conn, limit_seeds: int = 5, scrolls: int = 6, headless: bool = True) -> int:
    """Harvest following lists for up to `limit_seeds` un-harvested accounts."""
    seeds = accounts_mod.unharvested(conn, limit_seeds)
    if not seeds:
        log("No un-harvested accounts left. Use --reset to harvest again.")
        return 0

    log(f"Harvesting {len(seeds)} seed(s): {', '.join(seeds)}")
    total_new = 0

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR), headless=headless,
            viewport={"width": 1280, "height": 900},
        )
        page = context.pages[0] if context.pages else context.new_page()

        # Responses are keyed by the seed whose page was loading when the
        # request was issued; we resolve that from page.url at capture time.
        captured: dict[str, list[dict]] = {}

        def on_response(response):
            if FOLLOWING_OP not in response.url or "/i/api/graphql/" not in response.url:
                return
            try:
                body = response.json()
            except Exception:
                return
            # Attribute by the profile currently loaded in the tab.
            match = re.search(r"x\.com/([^/]+)/following", unquote(page.url))
            if not match:
                return
            captured.setdefault(match.group(1).lower(), []).extend(_users_from_body(body))

        page.on("response", on_response)

        for seed in seeds:
            log(f"\n@{seed}")
            try:
                goto_with_retry(page, f"https://x.com/{seed}/following", attempts=2)
                page.wait_for_timeout(3500)
            except Exception as exc:
                log(f"  skipped: {str(exc).splitlines()[0][:70]}")
                accounts_mod.mark_harvested(conn, seed)  # don't retry forever
                continue

            for i in range(scrolls):
                page.mouse.wheel(0, 2600)
                time.sleep(1.8)

            found = captured.get(seed.lower(), [])
            unique = {u["handle"]: u for u in found}
            new = accounts_mod.record_candidates(conn, seed, list(unique.values()))
            total_new += new
            log(f"  {len(unique)} follows seen, {new} new candidates")
            accounts_mod.mark_harvested(conn, seed)
            time.sleep(2.0)

        context.close()

    log(f"\n{total_new} new candidates discovered")
    return 0
