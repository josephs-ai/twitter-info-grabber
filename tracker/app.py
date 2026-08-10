"""Desktop app: a real OS window over the tracker database.

Built on pywebview, which wraps the system WebKit view — a genuine window in
the taskbar, no Electron, no Node, no Rust toolchain. The UI is HTML so post
images and charts render properly; the backend is the same Python the CLI uses,
so there is exactly one implementation of every rule.

The app exists mainly for the one thing only a human can do: rate judgements.
Everything else is read-only convenience.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

import webview

from . import accounts as accounts_mod
from . import extract as extract_mod
from . import amplify, db, digest, feedback, novelty
from . import schedule as schedule_mod

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui" / "index.html"


class Api:
    """Methods here are callable from JavaScript as window.pywebview.api.*"""

    def __init__(self):
        self._lock = threading.Lock()
        self._running = None

    # -- read ----------------------------------------------------------------

    def _conn(self):
        return db.connect()

    def overview(self) -> dict:
        conn = self._conn()
        try:
            def scalar(sql, *a):
                row = conn.execute(sql, a).fetchone()
                return (row[0] if row else 0) or 0

            funnel = {r["k"]: r["n"] for r in conn.execute(
                "SELECT COALESCE(drop_reason, stage) k, COUNT(*) n FROM triage GROUP BY k")}
            fb = feedback.stats(conn)
            return {
                "posts": scalar("SELECT COUNT(*) FROM posts"),
                "authors": scalar("SELECT COUNT(DISTINCT author_handle) FROM posts"),
                "accounts": scalar("SELECT COUNT(*) FROM accounts WHERE active=1"),
                "candidates": scalar("SELECT COUNT(*) FROM candidates WHERE status='new'"),
                "judged": scalar("SELECT COUNT(*) FROM judgements"),
                "surfaced": scalar("SELECT COUNT(*) FROM judgements WHERE verdict='surface'"),
                "funnel": funnel,
                "feedback": fb,
                "last_run": scalar("SELECT COUNT(*) FROM runs"),
            }
        finally:
            conn.close()

    def _rows_to_items(self, conn, rows) -> list[dict]:
        items = []
        for row in rows:
            item = dict(row)
            item["images"] = [
                r["url"] for r in conn.execute(
                    "SELECT url FROM media WHERE post_id=? AND kind='photo' LIMIT 2",
                    (item["id"],))
            ]
            item["rating"] = (conn.execute(
                "SELECT rating FROM feedback WHERE post_id=?", (item["id"],)).fetchone()
                or {"rating": None})["rating"]
            item["url"] = f"https://x.com/{item['author_handle']}/status/{item['id']}"
            item["extraction"] = extract_mod.for_post(conn, item["id"])
            items.append(item)
        return items

    def feed(self, mode: str = "surfaced", limit: int = 40) -> list[dict]:
        """mode: surfaced | nearmiss | unrated | amplified"""
        conn = self._conn()
        try:
            base = """
                SELECT p.id, p.author_handle, p.text, p.created_at, p.amplifiers,
                       p.capture_source, p.thread_size,
                       j.verdict, j.novelty, j.value, j.category, j.rationale
                FROM judgements j JOIN posts p ON p.id = j.post_id
            """
            if mode == "surfaced":
                sql = base + " WHERE j.verdict='surface' ORDER BY j.value DESC, p.amplifiers DESC"
            elif mode == "nearmiss":
                sql = base + (" WHERE j.verdict='skip' AND j.value >= 3 "
                              "ORDER BY j.value DESC, j.novelty DESC")
            elif mode == "unrated":
                sql = base + (" WHERE j.post_id NOT IN (SELECT post_id FROM feedback) "
                              "AND (j.verdict='surface' OR j.value >= 3) "
                              "ORDER BY j.value DESC")
            else:  # amplified
                sql = base + " WHERE p.amplifiers >= 2 ORDER BY p.amplifiers DESC"
            rows = conn.execute(sql + " LIMIT ?", (limit,)).fetchall()
            return self._rows_to_items(conn, rows)
        finally:
            conn.close()

    def candidates(self, by: str = "follows", limit: int = 40) -> list[dict]:
        conn = self._conn()
        try:
            order = "reply_count DESC, seed_count DESC" if by == "replies" \
                else "seed_count DESC, reply_count DESC"
            rows = conn.execute(
                f"SELECT handle, name, bio, seed_count, reply_count FROM candidates "
                f"WHERE status='new' ORDER BY {order} LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def digest_html(self) -> str:
        conn = self._conn()
        try:
            return digest.build(conn, since_hours=999999, limit=20, write=False)
        finally:
            conn.close()

    # -- write ---------------------------------------------------------------

    def rate(self, post_id: str, rating: str, note: str = "") -> dict:
        conn = self._conn()
        try:
            feedback.rate(conn, post_id, rating, note or None)
            return feedback.stats(conn)
        finally:
            conn.close()

    def unrate(self, post_id: str) -> dict:
        conn = self._conn()
        try:
            conn.execute("DELETE FROM feedback WHERE post_id=?", (post_id,))
            conn.commit()
            return feedback.stats(conn)
        finally:
            conn.close()

    def approve(self, handle: str) -> int:
        conn = self._conn()
        try:
            return accounts_mod.approve(conn, [handle])
        finally:
            conn.close()

    def reject(self, handle: str) -> bool:
        conn = self._conn()
        try:
            conn.execute("UPDATE candidates SET status='rejected' WHERE handle=?", (handle,))
            conn.commit()
            return True
        finally:
            conn.close()

    # -- schedule -------------------------------------------------------------

    def schedule_status(self) -> dict:
        conn = self._conn()
        try:
            return schedule_mod.status(conn)
        finally:
            conn.close()

    def schedule_save(self, settings: dict) -> dict:
        """Save and install in one action — a saved schedule that was never
        written to the OS would be a lie."""
        conn = self._conn()
        try:
            result = schedule_mod.install(conn, settings)
            result["status"] = schedule_mod.status(conn)
            return result
        finally:
            conn.close()

    # -- pipeline ------------------------------------------------------------

    def run_stage(self, stage: str) -> dict:
        """Run one pipeline stage in a worker thread so the UI stays responsive."""
        with self._lock:
            if self._running:
                return {"ok": False, "error": f"{self._running} is already running"}
            self._running = stage

        commands = {
            "collect": ["--all", "--scrolls", "5", "--headless"],
            "links": ["--limit", "40"],
            "amplify": [],
            "threads": ["--apply"],
            "dedup": ["--apply"],
            "judge": ["--limit", "30"],
            "digest": [],
        }
        if stage not in commands:
            self._running = None
            return {"ok": False, "error": f"unknown stage {stage}"}

        def worker():
            try:
                proc = subprocess.run(
                    [sys.executable, "-m", "tracker", stage, *commands[stage]],
                    capture_output=True, text=True, timeout=3600, cwd=str(ROOT))
                tail = (proc.stdout or proc.stderr or "").strip().splitlines()[-6:]
                payload = {"stage": stage, "ok": proc.returncode == 0,
                           "output": "\n".join(tail)}
            except Exception as exc:
                payload = {"stage": stage, "ok": False, "output": str(exc)[:300]}
            finally:
                self._running = None
            # Push the result into the page rather than making it poll.
            js = f"window.onStageDone && window.onStageDone({json.dumps(payload)})"
            for window in webview.windows:
                window.evaluate_js(js)

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "started": stage}

    def open_external(self, url: str) -> bool:
        """Open a post in the real browser, not inside the app window.

        webbrowser is stdlib and already knows the right incantation on each
        platform — xdg-open, `open`, or ShellExecute.
        """
        webbrowser.open(url)
        return True


def main() -> int:
    if not UI.exists():
        print(f"UI missing: {UI}")
        return 1
    webview.create_window(
        "AI Signal Tracker",
        str(UI),
        js_api=Api(),
        width=1180,
        height=860,
        min_size=(900, 600),
    )
    webview.start()
    return 0
