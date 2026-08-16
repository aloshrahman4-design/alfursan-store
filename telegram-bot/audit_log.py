"""Append-only audit trail: one JSON line per processed item.

A local JSON Lines file, not a database, by design: append-only, crash
safe, trivially greppable/diffable with normal shell tools, and this
bot's whole point is a handful of admins processing a modest number of
items per day -- no need for query/index machinery here. If that ever
changes, the log is still a clean import source for a real database.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

import config

logger = logging.getLogger(__name__)


def record(entry: Dict[str, Any]) -> None:
    path = Path(config.AUDIT_LOG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError:
        logger.exception("Failed to write audit log entry (item was still sent)")
