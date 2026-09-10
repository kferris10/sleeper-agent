"""SQLite history: weekly contexts, analyses, and (Phase 4) outcomes."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS contexts (
    id INTEGER PRIMARY KEY,
    season TEXT NOT NULL,
    week INTEGER NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY,
    season TEXT NOT NULL,
    week INTEGER NOT NULL,
    model TEXT NOT NULL,
    data TEXT NOT NULL,
    raw_response TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY,
    season TEXT NOT NULL,
    week INTEGER NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY,
    season TEXT NOT NULL,
    week INTEGER NOT NULL,
    channel TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, db_path: Path) -> None:
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def save_context(self, season: str, week: int, context_json: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO contexts (season, week, data, created_at) VALUES (?, ?, ?, ?)",
                (season, week, context_json, _now()),
            )

    def save_analysis(
        self, season: str, week: int, model: str, analysis_json: str, raw_response: str
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO analyses (season, week, model, data, raw_response, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (season, week, model, analysis_json, raw_response, _now()),
            )

    def get_analysis(self, season: str, week: int) -> str | None:
        """Most recent analysis JSON for a given week, or None."""
        row = self.conn.execute(
            "SELECT data FROM analyses WHERE season = ? AND week = ? ORDER BY id DESC LIMIT 1",
            (season, week),
        ).fetchone()
        return row[0] if row else None

    def was_delivered(self, season: str, week: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM deliveries WHERE season = ? AND week = ? LIMIT 1",
            (season, week),
        ).fetchone()
        return row is not None

    def mark_delivered(self, season: str, week: int, channel: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO deliveries (season, week, channel, created_at) VALUES (?, ?, ?, ?)",
                (season, week, channel, _now()),
            )
