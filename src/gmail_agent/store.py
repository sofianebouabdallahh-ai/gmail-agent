"""SQLite record of every message the pipeline touched: idempotency and run history.

One row per message id. `status` is `done`, `waiting` (paused for a human approval),
`failed` (`error` says why) or `dry_run` (decided but nothing applied; polled again). Older databases are migrated in place on open.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

COLUMNS = {  # added after the first version; created with ALTER TABLE when missing
    "status": "TEXT NOT NULL DEFAULT 'done'",
    "error": "TEXT",
    "cost_usd": "REAL",
    "duration_ms": "INTEGER",
    "backend": "TEXT",
}


class ProcessedStore:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed (
                message_id TEXT PRIMARY KEY,
                processed_at TEXT NOT NULL,
                action_kind TEXT,
                action_json TEXT
            )
            """
        )
        existing = {row[1] for row in self.conn.execute("PRAGMA table_info(processed)")}
        for name, ddl in COLUMNS.items():
            if name not in existing:
                self.conn.execute(f"ALTER TABLE processed ADD COLUMN {name} {ddl}")
        self.conn.commit()

    def status(self, message_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT status FROM processed WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row[0] if row else None

    def seen(self, message_id: str, retry_failed: bool = False) -> bool:
        """True when polling should skip this message."""
        status = self.status(message_id)
        if status is None or status == "dry_run":
            return False
        return not (retry_failed and status == "failed")

    def mark(self, message_id: str, *, status: str = "done", action_kind: str | None = None,
             action: dict | None = None, error: str | None = None,
             cost_usd: float | None = None, duration_ms: int | None = None,
             backend: str | None = None) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO processed
               (message_id, processed_at, action_kind, action_json,
                status, error, cost_usd, duration_ms, backend)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                message_id,
                datetime.now(timezone.utc).isoformat(),
                action_kind,
                json.dumps(action) if action is not None else None,
                status, error, cost_usd, duration_ms, backend,
            ),
        )
        self.conn.commit()

    def all(self) -> dict[str, dict]:
        cur = self.conn.execute(
            "SELECT message_id, status, error, cost_usd, duration_ms FROM processed")
        return {r[0]: {"status": r[1], "error": r[2], "cost_usd": r[3], "duration_ms": r[4]}
                for r in cur}
