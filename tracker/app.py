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
from . import judge as judge_mod
from . import notify as notify_mod
from . import amplify, db, digest, feedback, health, novelty
from . import cluster as cluster_mod
from . import curate as curate_mod
from . import search as search_mod
from . import schedule as schedule_mod
from . import strictness as strict_mod

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

            _where, _params = strict_mod.clause(strict_mod.load(conn))
            funnel = {r["k"]: r["n"] for r in conn.execute(
                "SELECT COALESCE(drop_reason, stage) k, COUNT(*) n FROM triage GROUP BY k")}
            fb = feedback.stats(conn)
            return {
                "posts": scalar("SELECT COUNT(*) FROM posts"),
                "authors": scalar("SELECT COUNT(DISTINCT author_handle) FROM posts"),
                "accounts": scalar("SELECT COUNT(*) FROM accounts WHERE active=1"),
                "candidates": scalar("SELECT COUNT(*) FROM candidates WHERE status='new'"),
                "judged": scalar("SELECT COUNT(DISTINCT post_id) FROM judgements"),
                # The rate that means anything is surfaced-per-judged. Posts
                # still queued for the judge have not been ruled on, so putting
                # them in the denominator understates the filter by ~10x.
                "waiting": judge_mod.backlog(conn)["waiting"],
                # The bar is a live setting, so this must be counted against it.
                # Reading the frozen verdict here made the hero number disagree
                # with the feed whenever the slider moved.
                "surfaced": scalar(
                    f"SELECT COUNT(DISTINCT post_id) FROM judgements j WHERE {_where}",
                    *_params),
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

    def _cluster(self, items: list[dict]) -> list[dict]:
        """Fold repeats of one story into its lead, keeping the others attached."""
        out = []
        for group in cluster_mod.group(items):
            lead = dict(group["lead"])
            lead["also"] = [
                {"handle": m["author_handle"], "id": m["id"],
                 "url": m["url"], "value": m.get("value")}
                for m in group["members"][1:]
            ]
            out.append(lead)
        return out

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
                where, params = strict_mod.clause(strict_mod.load(conn))
                rows = conn.execute(
                    base + f" WHERE {where} ORDER BY j.value DESC, p.amplifiers DESC LIMIT ?",
                    (*params, limit)).fetchall()
                # One story per entry. Six accounts announcing one release is
                # corroboration, not six things to read.
                return self._cluster(self._rows_to_items(conn, rows))
            level = strict_mod.load(conn)
            where, params = strict_mod.clause(level)
            if mode == "nearmiss":
                # Below the current bar but within one point of it — move the
                # slider and what counts as a near miss moves with it.
                sql = base + (f" WHERE NOT ({where}) AND j.value >= ? "
                              "ORDER BY j.value DESC, j.novelty DESC")
                args = [*params, max(1, level["value"] - 1)]
            elif mode == "unrated":
                sql = base + (f" WHERE j.post_id NOT IN (SELECT post_id FROM feedback) "
                              f"AND (({where}) OR j.value >= ?) ORDER BY j.value DESC")
                args = [*params, max(1, level["value"] - 1)]
            else:  # amplified
                sql = base + " WHERE p.amplifiers >= 2 ORDER BY p.amplifiers DESC"
                args = []
            rows = conn.execute(sql + " LIMIT ?", (*args, limit)).fetchall()
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

    def health(self) -> dict:
        conn = self._conn()
        try:
            checks = health.report(conn)
            return {"checks": checks, "worst": health.worst(checks)}
        finally:
            conn.close()

    def search(self, query: str, limit: int = 40) -> list[dict]:
        conn = self._conn()
        try:
            rows = search_mod.run(conn, query, limit=limit)
            return self._rows_to_items(conn, rows)
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

    # -- self-curation ---------------------------------------------------------

    def curate_status(self) -> dict:
        conn = self._conn()
        try:
            preview = curate_mod.run(conn, dry_run=True, force=True)
            return {"settings": curate_mod.load(conn),
                    "promote": preview["promoted"], "demote": preview["demoted"],
                    "history": curate_mod.history(conn, 12)}
        finally:
            conn.close()

    def curate_save(self, settings: dict) -> dict:
        conn = self._conn()
        try:
            return curate_mod.save(conn, settings)
        finally:
            conn.close()

    def curate_apply(self) -> dict:
        conn = self._conn()
        try:
            r = curate_mod.run(conn, dry_run=False, force=True)
            return {"promoted": len(r["promoted"]), "demoted": len(r["demoted"])}
        finally:
            conn.close()

    def curate_undo(self) -> int:
        conn = self._conn()
        try:
            return curate_mod.undo(conn)
        finally:
            conn.close()

    # -- strictness ------------------------------------------------------------

    def strictness_status(self) -> dict:
        conn = self._conn()
        try:
            return {"current": strict_mod.load(conn), "levels": strict_mod.preview(conn)}
        finally:
            conn.close()

    def strictness_set(self, key: str) -> dict:
        conn = self._conn()
        try:
            strict_mod.save(conn, key)
            return {"current": strict_mod.load(conn), "levels": strict_mod.preview(conn)}
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

    # -- notifications ---------------------------------------------------------

    def notify_status(self) -> dict:
        conn = self._conn()
        try:
            return {"settings": notify_mod.load(conn),
                    "pending": len(notify_mod.undelivered(conn, limit=50))}
        finally:
            conn.close()

    def notify_save(self, settings: dict) -> dict:
        conn = self._conn()
        try:
            return notify_mod.save(conn, settings)
        finally:
            conn.close()

    def notify_test(self) -> dict:
        """Send one now, whatever the watermark says — the point is to prove the
        channel works, and a test that silently does nothing proves nothing."""
        conn = self._conn()
        try:
            settings = notify_mod.load(conn)
            title = "AI Signal test"
            body = "If you can read this, notifications are working."
            sent = []
            if settings["desktop"] and notify_mod.desktop(title, body):
                sent.append("desktop")
            if settings["webhook_url"] and notify_mod.webhook(
                    settings["webhook_url"], title, body):
                sent.append("webhook")
            return {"sent": sent}
        finally:
            conn.close()

    # -- pipeline ------------------------------------------------------------

    def run_stage(self, stage: str) -> dict:
        """Run one pipeline stage in a worker thread so the UI stays responsive."""
        with self._lock:
            if self._running:
                return {"ok": False, "error": f"{self._running} is already running"}
            self._running = stage

        # Every stage the Pipeline page offers has to be here — `extract` was
        # on the page but missing from this map, so its button returned
        # "unknown stage".
        commands = {
            "collect": ["--all", "--scrolls", "5", "--headless"],
            "replies": ["--limit", "4"],
            "suggest": ["--seeds", "2"],
            "links": ["--limit", "40"],
            "curate": ["--apply"],
            "amplify": [],
            "threads": ["--apply"],
            "dedup": ["--apply"],
            "judge": ["--limit", "30"],
            "extract": ["--limit", "15"],
            "digest": [],
            "notify": [],
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
