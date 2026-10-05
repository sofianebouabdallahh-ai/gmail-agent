"""Structured logging: one JSON object per line on stderr, easy to grep or ship."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

logger = logging.getLogger("gmail_agent")


def setup(level: int = logging.INFO) -> None:
    if logger.handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False


def event(name: str, **fields) -> None:
    record = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": name}
    record.update({k: v for k, v in fields.items() if v is not None})
    logger.info(json.dumps(record, default=str, ensure_ascii=False))
