from __future__ import annotations

import sqlite3
from pathlib import Path

from .models import Market


class SnapshotStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS snapshots (
                market_id TEXT NOT NULL, observed_at TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY (market_id, observed_at))""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_market_time ON snapshots(market_id, observed_at)")

    def _connect(self):
        return sqlite3.connect(self.path)

    def save(self, markets: list[Market]):
        with self._connect() as conn:
            conn.executemany("INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?)", [
                (m.market_id, m.observed_at.isoformat(), m.model_dump_json()) for m in markets
            ])

    def history(self, market_id: str, before, hours: int = 48) -> list[Market]:
        from datetime import timedelta
        start = (before - timedelta(hours=hours)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM snapshots WHERE market_id=? AND observed_at>=? AND observed_at<? ORDER BY observed_at",
                (market_id, start, before.isoformat()),
            ).fetchall()
        return [Market.model_validate_json(row[0]) for row in rows]
