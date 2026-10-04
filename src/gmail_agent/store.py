"""Tiny SQLite store for idempotency: which message ids have already been processed."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


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
        self.conn.commit()

    def seen(self, message_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM processed WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row is not None

    def mark(self, message_id: str, action_kind: str | None, action: dict | None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO processed VALUES (?, ?, ?, ?)",
            (
                message_id,
                datetime.now(timezone.utc).isoformat(),
                action_kind,
                json.dumps(action) if action is not None else None,
            ),
        )
        self.conn.commit()
