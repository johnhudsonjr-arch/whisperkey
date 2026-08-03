"""Transcript history.

Mostly exists for one specific failure: dictation landing in the wrong window
because focus moved. ``whisperkey last`` re-pastes the previous result into
wherever the focus is now.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

from .config import HISTORY_DB, HistoryConfig

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS transcripts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  REAL    NOT NULL,
    duration    REAL    NOT NULL,
    latency     REAL    NOT NULL,
    raw_text    TEXT    NOT NULL,
    final_text  TEXT    NOT NULL,
    wm_class    TEXT    NOT NULL DEFAULT '',
    method      TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_created_at ON transcripts (created_at DESC);
"""


class History:
    def __init__(self, cfg: HistoryConfig, path: Path | None = None):
        self.cfg = cfg
        self.path = path or HISTORY_DB
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # The daemon writes from its worker thread, reads from the UI thread.
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        return self._conn

    def add(self, *, duration: float, latency: float, raw_text: str,
            final_text: str, wm_class: str = "", method: str = "") -> None:
        if not self.cfg.enabled:
            return
        try:
            conn = self.connect()
            conn.execute(
                "INSERT INTO transcripts (created_at, duration, latency,"
                " raw_text, final_text, wm_class, method)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time(), duration, latency, raw_text, final_text,
                 wm_class, method),
            )
            conn.commit()
            self._prune(conn)
        except sqlite3.Error:
            log.exception("could not record transcript")

    def _prune(self, conn: sqlite3.Connection) -> None:
        limit = self.cfg.max_rows
        if limit <= 0:
            return
        conn.execute(
            "DELETE FROM transcripts WHERE id NOT IN "
            "(SELECT id FROM transcripts ORDER BY created_at DESC LIMIT ?)",
            (limit,),
        )
        conn.commit()

    def last(self) -> str | None:
        try:
            row = self.connect().execute(
                "SELECT final_text FROM transcripts ORDER BY created_at DESC"
                " LIMIT 1"
            ).fetchone()
        except sqlite3.Error:
            log.exception("could not read history")
            return None
        return row[0] if row else None

    def recent(self, limit: int = 20) -> list[tuple[float, str]]:
        try:
            rows = self.connect().execute(
                "SELECT created_at, final_text FROM transcripts"
                " ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        except sqlite3.Error:
            log.exception("could not read history")
            return []
        return [(row[0], row[1]) for row in rows]

    def stats(self) -> dict:
        try:
            row = self.connect().execute(
                "SELECT COUNT(*), COALESCE(SUM(duration), 0),"
                " COALESCE(AVG(latency), 0) FROM transcripts"
            ).fetchone()
        except sqlite3.Error:
            return {"count": 0, "seconds": 0.0, "avg_latency": 0.0}
        return {"count": row[0], "seconds": row[1], "avg_latency": row[2]}

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
