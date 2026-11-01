"""Local experience memory: scrubbed run metrics + queryable lessons (keyword/FTS5), fully local.

Phase 1 of docs/superpowers/specs/2026-10-31-experience-memory-design.md. Flows stay the structured
executor; this store only augments Jev with retrieved knowledge. Nothing leaves the device, and no
secret or obvious account/PII digit run is ever stored (ingest scrubs on the way in).
"""
import re
import sqlite3
import time
from pathlib import Path

# Strip obvious PII before anything retrievable is stored, even though the DB is local: long digit
# runs (account / reference / phone fragments) and masked-account patterns. Labels and verbs stay,
# so retrieval still works ("avoid the Quiero sheet", "hide keyboard before Continuar").
_MASKED = re.compile(r"[*•xX]{2,}\s?\d{2,6}")
_DIGIT_RUN = re.compile(r"\d{4,}")
_WORD = re.compile(r"\w+", re.UNICODE)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY, ts REAL, target TEXT, app TEXT, goal TEXT,
    status TEXT, reason TEXT, verified INTEGER, steps INTEGER, duration_ms INTEGER,
    loop_events INTEGER, flow_used TEXT, flow_saved TEXT);
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY, created_at REAL, target TEXT, app TEXT,
    goal_text TEXT, screen_tags TEXT, body TEXT, source TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS lessons_fts USING fts5(
    body, goal_text, screen_tags, content='lessons', content_rowid='id');
"""


def scrub_pii(text: str | None) -> str:
    if not text:
        return ""
    return _DIGIT_RUN.sub("####", _MASKED.sub("•••", text))


def _match_query(text: str) -> str:
    """Build a safe FTS5 MATCH expression from free text: OR of quoted word tokens, or "" if none."""
    tokens = [t for t in _WORD.findall(text or "") if len(t) >= 2]
    return " OR ".join(f'"{t}"' for t in dict.fromkeys(tokens)) if tokens else ""


class MemoryStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def record_run(self, *, target, app=None, goal="", status="", reason=None, verified=None,
                   steps=0, duration_ms=0, loop_events=0, flow_used=None, flow_saved=None) -> None:
        self._conn.execute(
            "INSERT INTO runs (ts, target, app, goal, status, reason, verified, steps, "
            "duration_ms, loop_events, flow_used, flow_saved) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), target, app, scrub_pii(goal), status, reason,
             None if verified is None else int(verified), steps, duration_ms, loop_events,
             flow_used, flow_saved))
        self._conn.commit()

    def add_lesson(self, *, target=None, app=None, goal="", body="", screen_tags="",
                   source="auto") -> int:
        body, goal, screen_tags = scrub_pii(body), scrub_pii(goal), scrub_pii(screen_tags)
        cur = self._conn.execute(
            "INSERT INTO lessons (created_at, target, app, goal_text, screen_tags, body, source) "
            "VALUES (?,?,?,?,?,?,?)",
            (time.time(), target, app, goal, screen_tags, body, source))
        self._conn.execute(
            "INSERT INTO lessons_fts (rowid, body, goal_text, screen_tags) VALUES (?,?,?,?)",
            (cur.lastrowid, body, goal, screen_tags))
        self._conn.commit()
        return cur.lastrowid

    def query_lessons(self, *, target=None, app=None, goal="", screen_tags="", k=5) -> list[dict]:
        """Top-k lessons relevant to this goal+screen, scoped by target/app, best match first."""
        filters, params = [], []
        if target:
            filters.append("l.target = ?")
            params.append(target)
        if app:
            filters.append("l.app = ?")
            params.append(app)
        where = (" AND " + " AND ".join(filters)) if filters else ""
        match = _match_query(f"{goal} {screen_tags}")
        if match:
            rows = self._conn.execute(
                "SELECT l.* FROM lessons_fts JOIN lessons l ON l.id = lessons_fts.rowid "
                f"WHERE lessons_fts MATCH ?{where} ORDER BY bm25(lessons_fts) LIMIT ?",
                [match, *params, k]).fetchall()
            if rows:
                return [dict(r) for r in rows]
        where2 = (" WHERE " + " AND ".join(filters)) if filters else ""
        rows = self._conn.execute(
            f"SELECT l.* FROM lessons l{where2} ORDER BY l.created_at DESC LIMIT ?",
            [*params, k]).fetchall()
        return [dict(r) for r in rows]

    def import_lessons_md(self, path, target=None) -> int:
        """One-time import of a freeform lessons.md into retrievable lessons (one per H2 section).
        After import the DB is the source of truth, so a second call is a no-op."""
        path = Path(path)
        if not path.exists():
            return 0
        if self._conn.execute(
                "SELECT COUNT(*) FROM lessons WHERE source = 'lessons.md'").fetchone()[0]:
            return 0  # already imported
        count = 0
        # [1:] drops the preamble before the first H2 (not a lesson); each remaining chunk is a section
        for part in re.split(r"(?m)^##\s+", path.read_text(errors="replace"))[1:]:
            body = part.strip()
            if len(body) < 20:  # skip tiny fragments
                continue
            self.add_lesson(target=target, goal=body.splitlines()[0], body=body, source="lessons.md")
            count += 1
        return count

    def stats(self) -> dict:
        """Counts for the status tool, so the accumulating learning is visible."""
        one = lambda sql: self._conn.execute(sql).fetchone()[0]
        return {
            "lessons": one("SELECT COUNT(*) FROM lessons"),
            "runs": one("SELECT COUNT(*) FROM runs"),
            "done": one("SELECT COUNT(*) FROM runs WHERE status = 'done'"),
            "verified": one("SELECT COUNT(*) FROM runs WHERE verified = 1"),
        }

    def close(self) -> None:
        self._conn.close()
