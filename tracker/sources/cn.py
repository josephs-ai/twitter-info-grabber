"""Weibo and Xiaohongshu, by the same trick that works on X.

Both services sign their API requests — Xiaohongshu especially, with per-request
signature headers derived from a device fingerprint. Reverse-engineering that
signing is a treadmill: it changes, and every change silently breaks
collection.

So do not sign anything. Load the page in a real browser, let it sign its own
requests, and read the JSON that comes back — the same GraphQL-interception
approach that has held up on X, applied to a different set of endpoints. The
page is the client; we are only listening.

What this costs you:

  A separate login per service, in its own browser profile, so a ban on one
  cannot touch the others.
  Real ban risk. Use accounts you would not mind losing — Xiaohongshu in
  particular is aggressive about automation.
  Terms of service. Both prohibit this, exactly as X does.

What it gets you: Chinese AI discussion, which runs days ahead of its English
translation on some topics and never gets translated at all on others.

Note Xiaohongshu is image-first. A note is a few lines of caption over a photo
carousel, so expect less text per post than anywhere else here. Weibo is much
closer to X and is the better of the two for this pipeline.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from .. import paths
from ..collect import goto_with_retry, log

WEIBO_PROFILE = paths.data_dir() / ".browser-profile-weibo"
XHS_PROFILE = paths.data_dir() / ".browser-profile-xhs"

# Endpoints whose responses carry posts. Matched as substrings of the URL,
# because both services version and re-path these without warning — the same
# reason TIMELINE_OPS exists for X.
WEIBO_ENDPOINTS = ("/ajax/statuses/mymblog", "/api/container/getIndex",
                   "/ajax/feed/friendstimeline", "/ajax/statuses/searchAll")
# Deliberately unversioned. Pinning these to v1 was already wrong on the day it
# was written — search actually answers on /api/sns/web/v2/search/notes, so the
# harvester matched nothing while the parser worked perfectly on the same data.
# Same lesson as TIMELINE_OPS on X: match the endpoint, not its version.
XHS_ENDPOINTS = ("/search/notes", "/homefeed", "/user_posted", "/note/feed")

# Not every success:false is a login problem. /search/onebox returns
# success:false with msg "成功" as a matter of course, which read as "the
# service rejected every request: success".
XHS_LOGOUT_HINTS = ("无登录信息", "登录", "login")

_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return " ".join(_TAG_RE.sub(" ", text).split())


_REL_MIN = re.compile(r"(\d+)\s*分钟前")
_REL_HOUR = re.compile(r"(\d+)\s*小时前")
_YESTERDAY = re.compile(r"昨天\s*(\d{1,2}):(\d{2})")
_MONTH_DAY = re.compile(r"^(\d{1,2})-(\d{1,2})")


def _weibo_time(value: str | None) -> str:
    """Weibo dates a post two ways, and only one of them is a date.

    The desktop API gives 'Mon Aug 10 12:00:00 +0800 2026'. The mobile API
    gives '刚刚', '25分钟前', '昨天 12:30' or '08-10' for anything recent.
    Falling back to now() on all of those would date every old post to this
    minute, which admission control reads as "fresh when collected" — exactly
    the posts it exists to hold back.
    """
    if not value:
        return datetime.now(timezone.utc).isoformat()
    value = value.strip()
    try:
        return datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y") \
            .astimezone(timezone.utc).isoformat()
    except ValueError:
        pass

    # Weibo renders these in Beijing time; anchor the arithmetic there.
    now = datetime.now(timezone(timedelta(hours=8)))
    if "刚刚" in value:
        stamp = now
    elif (m := _REL_MIN.search(value)):
        stamp = now - timedelta(minutes=int(m.group(1)))
    elif (m := _REL_HOUR.search(value)):
        stamp = now - timedelta(hours=int(m.group(1)))
    elif (m := _YESTERDAY.search(value)):
        stamp = (now - timedelta(days=1)).replace(
            hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    elif (m := _MONTH_DAY.match(value)):
        month, day = int(m.group(1)), int(m.group(2))
        year = now.year - (1 if month > now.month else 0)
        try:
            stamp = now.replace(year=year, month=month, day=day,
                                hour=12, minute=0, second=0, microsecond=0)
        except ValueError:
            stamp = now
    else:
        stamp = now
    return stamp.astimezone(timezone.utc).isoformat()


def walk_weibo(payload) -> list[dict]:
    """Find status objects anywhere in a response.

    Recursive rather than path-based, for the reason the X parser is: the
    envelope moves between endpoints and versions, the object does not.
    """
    found: list[dict] = []

    def visit(node):
        if isinstance(node, dict):
            # Two shapes, one object. The desktop API returns mblogid +
            # text_raw; the mobile API returns bid + text and neither of the
            # other two, so requiring the desktop fields rejected every mobile
            # post while the endpoint was being read correctly.
            ident = node.get("mblogid") or node.get("bid") or node.get("idstr")
            has_body = "text_raw" in node or "text" in node
            if ident and has_body:
                user = node.get("user") or {}
                text = _clean(node.get("text_raw") or node.get("text"))
                # A repost carries the original nested; keep both, as with X.
                retweet = node.get("retweeted_status")
                if retweet:
                    visit(retweet)
                    text = f"{text}\n\n转发：{_clean(retweet.get('text_raw') or retweet.get('text'))}"
                if text:
                    uid = user.get("idstr") or user.get("id") or ""
                    found.append({
                        "id": f"weibo:{ident}",
                        "author_handle": str(user.get("screen_name") or "weibo")[:80],
                        "author_name": user.get("screen_name") or "",
                        "text": text,
                        "created_at": _weibo_time(node.get("created_at")),
                        "platform": "weibo",
                        "url": f"https://weibo.com/{uid}/{ident}",
                    })
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(payload)
    return found


def walk_xhs(payload) -> list[dict]:
    """Find note objects. Xiaohongshu nests them under several key names."""
    found: list[dict] = []

    def visit(node):
        if isinstance(node, dict):
            card = node.get("note_card") or node.get("noteCard") or node
            title = _clean(card.get("display_title") or card.get("title"))
            body = _clean(card.get("desc"))
            ident = node.get("note_id") or node.get("id") or card.get("note_id")
            if ident and (title or body):
                user = card.get("user") or {}
                token = node.get("xsec_token") or card.get("xsec_token") or ""
                found.append({
                    "id": f"xhs:{ident}",
                    "author_handle": str(user.get("nickname") or "xiaohongshu")[:80],
                    "author_name": user.get("nickname") or "",
                    # Title then body: an XHS note leads with its hook, and the
                    # caption underneath is usually where the substance is.
                    "text": f"{title}\n\n{body}".strip(),
                    "created_at": _xhs_time(card.get("time") or card.get("last_update_time")),
                    "platform": "xhs",
                    "url": (f"https://www.xiaohongshu.com/explore/{ident}"
                            + (f"?xsec_token={token}" if token else "")),
                })
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(payload)
    return found


def _xhs_time(value) -> str:
    """Milliseconds since epoch, when present at all."""
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return datetime.now(timezone.utc).isoformat()


def _harvest(urls, profile_dir, endpoints, walker, headless=True,
             scrolls=3, pause=2500, mobile=False) -> list[dict]:
    """Open each URL, scroll, and keep whatever the page fetched for itself."""
    from playwright.sync_api import sync_playwright

    from ..collect import ensure_browser
    if not ensure_browser():
        return []

    seen: dict[str, dict] = {}
    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir), headless=headless,
            locale="zh-CN",
            **({"user_agent": MOBILE_UA, "viewport": {"width": 420, "height": 900}}
               if mobile else {"viewport": {"width": 1280, "height": 900}}))
        page = context.pages[0] if context.pages else context.new_page()

        rejected = []

        def on_response(response):
            try:
                payload = response.json()
            except Exception:
                return
            # Xiaohongshu answers every API call with success:false and
            # "无登录信息" when there is no session — the endpoints fire, the
            # JSON parses, and nothing comes back. Without this the run looks
            # like a quiet day instead of a missing login.
            if isinstance(payload, dict) and payload.get("success") is False:
                message = str(payload.get("msg") or "")
                if (any(h in message for h in XHS_LOGOUT_HINTS)
                        and message not in rejected):
                    rejected.append(message)
            if not any(e in response.url for e in endpoints):
                return
            for post in walker(payload):
                seen.setdefault(post["id"], post)

        page.on("response", on_response)
        for url in urls:
            try:
                goto_with_retry(page, url)
                page.wait_for_timeout(pause)
                for _ in range(scrolls):
                    page.mouse.wheel(0, 2000)
                    page.wait_for_timeout(pause)
            except Exception as exc:  # noqa: BLE001 - one profile, not the run
                log(f"  {url}: {str(exc).splitlines()[0][:70]}")
        context.close()
    if rejected and not seen:
        log(f"  the service rejected every request: {rejected[0]}")
        log(f"  sign in first, into {profile_dir.name}")
    return list(seen.values())


def _xhs_signed_in(url: str, payload) -> bool:
    """Xiaohongshu's own answer to "am I logged in?"."""
    if "/api/sns/web/v2/user/me" not in url:
        return False
    data = payload.get("data") if isinstance(payload, dict) else None
    return bool(payload.get("success") and isinstance(data, dict)
                and (data.get("user_id") or data.get("nickname")))


def _weibo_signed_in(url: str, payload) -> bool:
    if "/ajax/setting/getConfig" not in url and "/ajax/profile/info" not in url:
        return False
    data = payload.get("data") if isinstance(payload, dict) else None
    return bool(isinstance(data, dict) and (data.get("uid") or data.get("user")))


def login_weibo() -> int:
    return _login(WEIBO_PROFILE, "https://weibo.com/login.php", _weibo_signed_in)


def login_xhs() -> int:
    # The explore page does not offer a sign-in form until you ask for one;
    # this URL opens the login flow directly.
    return _login(XHS_PROFILE, "https://www.xiaohongshu.com/login", _xhs_signed_in)


def _login(profile_dir, url: str, verify, timeout_s: int = 900) -> int:
    """Wait for the service to confirm the session, not for a cookie to appear.

    The first version watched for a named cookie and closed as soon as it saw
    one. Xiaohongshu sets its session cookie for anonymous visitors too, so it
    fired instantly, reported "Session saved" and shut the window before anyone
    could type anything. A cookie says a request happened; only the service can
    say who you are.

    So the signal is the service's own reply to its own "who am I" call, and
    the window stays open until it arrives or you close it.
    """
    import time

    from playwright.sync_api import sync_playwright

    from ..collect import ensure_browser
    if not ensure_browser():
        return 1

    log(f"Opening {url}")
    log("Sign in — QR from the phone app is usually quickest.")
    log("The window stays open until the service confirms it. Close it to give up.")
    profile_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout_s
    confirmed = {"ok": False}

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir), headless=False,
            viewport={"width": 1280, "height": 900}, locale="zh-CN")
        page = context.pages[0] if context.pages else context.new_page()

        def on_response(response):
            try:
                payload = response.json()
            except Exception:
                return
            if verify(response.url, payload):
                confirmed["ok"] = True

        # Listen on the context, not the page. Signing in navigates, and can
        # open a tab; a handler bound to the first page stops hearing as soon
        # as that happens, and the wait below then blocks on a dead object.
        context.on("response", on_response)
        try:
            goto_with_retry(page, url)
            log(f"Page loaded: {page.title()[:50]!r}")
        except RuntimeError as exc:
            # Worth separating loudly: a page that never loaded is a network
            # problem, not a sign-in problem, and the two look identical from
            # a window that is just sitting there.
            log(f"COULD NOT LOAD THE PAGE: {exc}")
            log("That is a network problem, not a login problem — the window is")
            log("open, so try navigating there by hand.")

        # Sleep on the clock rather than on the page. page.wait_for_timeout
        # raises the moment its page is navigated away or replaced, and the old
        # loop treated any exception as "the user gave up" — so a perfectly
        # normal redirect during sign-in looked like a cancelled login and shut
        # the window.
        waited = 0
        while time.time() < deadline and not confirmed["ok"]:
            time.sleep(2)
            waited += 2
            if not context.pages:          # every window really is closed
                log("  the browser window was closed")
                break
            # Silence is indistinguishable from a hang. Say what is happening.
            if waited % 30 == 0:
                where = ""
                try:
                    where = f" — currently on {context.pages[-1].url[:60]}"
                except Exception:
                    pass
                log(f"  still waiting for the service to confirm a session "
                    f"({waited}s){where}")
        try:
            context.close()
        except Exception:
            pass

    if confirmed["ok"]:
        log("Signed in — the service confirmed it. Session saved.")
        return 0
    log("NOT signed in: the service never confirmed a session.")
    log("Nothing was saved. Run this again and complete the sign-in.")
    return 1


def _targets(conn, platform: str) -> list[str]:
    """Who to read, from the accounts table, namespaced by platform."""
    rows = conn.execute(
        "SELECT handle FROM accounts WHERE active=1 AND handle LIKE ?",
        (f"{platform}:%",)).fetchall()
    return [r["handle"].split(":", 1)[1] for r in rows]


WEIBO_KEYWORDS = ["大模型", "开源模型", "AI Agent", "具身智能"]

# The mobile site serves the same posts as plain JSON with far less JavaScript,
# and — unlike the desktop site — returns search results without a login. It is
# the better target on every axis except that it needs a phone user-agent.
MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
             "Mobile/15E148 Safari/604.1")


def fetch_weibo(conn, limit: int = 40, headless: bool = True,
                keywords: list[str] | None = None) -> list[dict]:
    import urllib.parse

    urls = [f"https://m.weibo.cn/u/{uid}" for uid in _targets(conn, "weibo")]
    for keyword in (keywords or WEIBO_KEYWORDS):
        query = urllib.parse.quote(f"type=1&q={keyword}", safe="")
        urls.append(f"https://m.weibo.cn/search?containerid=100103{query}")
    return _harvest(urls, WEIBO_PROFILE, WEIBO_ENDPOINTS, walk_weibo,
                    headless=headless, mobile=True)[:limit]


# Verified against the live site: the personalised home feed of a fresh account
# returns hiking, dating advice and travel — Xiaohongshu is a lifestyle
# platform, and the AI content has to be asked for. Searching these returned 79
# notes on LLMs, agent development and local deployment in one pass.
XHS_KEYWORDS = ["大模型", "AI Agent", "AI编程", "开源模型", "提示词"]


def fetch_xhs(conn, limit: int = 40, headless: bool = True,
              keywords: list[str] | None = None) -> list[dict]:
    import urllib.parse

    urls = [f"https://www.xiaohongshu.com/user/profile/{i}"
            for i in _targets(conn, "xhs")]
    # Search rather than the home feed, which is personalised to an account
    # that by design has no history and no interests.
    urls += ["https://www.xiaohongshu.com/search_result?keyword="
             + urllib.parse.quote(k) for k in (keywords or XHS_KEYWORDS)]
    return _harvest(urls, XHS_PROFILE, XHS_ENDPOINTS, walk_xhs,
                    headless=headless)[:limit]


# The registry calls fetch(conn, limit=...); both services share one entry so a
# run reads whichever of them you have signed into.
def fetch(conn, limit: int = 40) -> list[dict]:
    return fetch_weibo(conn, limit) + fetch_xhs(conn, limit)
