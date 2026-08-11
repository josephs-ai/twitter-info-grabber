"""Getting from a clone to a working tracker, inside the app.

The setup this replaces was: read a README, run four commands in a terminal,
edit a dotfile, then wait two days before anything appears. Every one of those
is a place to give up, and the last one is the worst — the app opened on a
screen of zeros with no way to tell "not set up" from "nothing found today".

So the state is derived rather than remembered. There is no "setup complete"
flag to get out of sync with reality: each step asks the system a question it
can answer directly — is there a key, is there a browser profile, are there
accounts, are there posts. Delete the profile and step two goes back to
pending, which is correct.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from . import paths

ENV = paths.data_dir() / ".env"

# Not a checksum, just a shape check — enough to catch a pasted-wrong string
# without pretending to validate it offline.
KEY_RE = re.compile(r"^sk-ant-[A-Za-z0-9_\-]{20,}$")


def api_key_present() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    if not ENV.exists():
        return False
    for line in ENV.read_text().splitlines():
        if line.strip().startswith("ANTHROPIC_API_KEY="):
            return bool(line.split("=", 1)[1].strip().strip('"').strip("'"))
    return False


def save_api_key(key: str) -> dict:
    """Write the key into .env, leaving anything else in there alone."""
    key = (key or "").strip()
    if not KEY_RE.match(key):
        return {"ok": False, "error": "That does not look like an Anthropic key "
                                      "(they start with sk-ant-)."}

    lines = ENV.read_text().splitlines() if ENV.exists() else []
    out, replaced = [], False
    for line in lines:
        if line.strip().startswith("ANTHROPIC_API_KEY="):
            out.append(f"ANTHROPIC_API_KEY={key}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"ANTHROPIC_API_KEY={key}")
    ENV.write_text("\n".join(out) + "\n")
    # The key is a credential and .env sits in the project directory, so the
    # permissions matter more than the convenience.
    try:
        ENV.chmod(0o600)
    except OSError:
        pass
    os.environ["ANTHROPIC_API_KEY"] = key
    return {"ok": True}


def signed_in() -> bool:
    from . import collect

    profile = collect.PROFILE_DIR
    return profile.exists() and any(profile.iterdir())


def state(conn) -> dict:
    def scalar(sql):
        row = conn.execute(sql).fetchone()
        return (row[0] if row else 0) or 0

    accounts = scalar("SELECT COUNT(*) FROM accounts WHERE active=1")
    posts = scalar("SELECT COUNT(*) FROM posts")
    judged = scalar("SELECT COUNT(*) FROM judgements")

    steps = [
        {"key": "key", "title": "Anthropic API key",
         "note": "Scores and extracts. Around $5 a month at ~70 judgements a day.",
         "done": api_key_present()},
        {"key": "login", "title": "Sign in to X",
         "note": "Use a throwaway account. Scraping is against X's terms, and a "
                 "suspension should cost you nothing.",
         "done": signed_in()},
        {"key": "accounts", "title": "Who to follow",
         "note": "A starting list of AI researchers ships with the repo. It "
                 "curates itself from there.",
         "done": accounts > 0, "detail": f"{accounts} tracked"},
        {"key": "collect", "title": "First collection",
         "note": "Novelty is measured against your own corpus, so the first run "
                 "has nothing to compare against. Give it two or three days.",
         "done": posts > 0, "detail": f"{posts:,} posts"},
    ]
    return {
        "steps": steps,
        "done": all(s["done"] for s in steps),
        "posts": posts,
        "judged": judged,
        # Honest about the part no setup wizard can shortcut.
        "warming_up": posts > 0 and judged == 0,
    }
