"""ww/core/logger.py — WW structuredlogsystem v0.1"""

from __future__ import annotations
import json
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional
from pathlib import Path

LOG_DIR = os.path.expanduser("~/.ww/logs")


def _ensure_dir():
    Path(LOG_DIR).mkdir(parents=True, exist_ok=True)


class WWLogger:
    """WW's structuredlogsystem. Supports filtering by task/spiral/level."""

    def __init__(self, max_entries: int = 1000):
        self._max = max_entries
        self._entries: List[Dict] = []
        _ensure_dir()
        self._load()

    def _load(self):
        path = os.path.join(LOG_DIR, "recent.json")
        try:
            with open(path) as f:
                data = json.load(f)
                self._entries = data[-self._max:]
        except (FileNotFoundError, json.JSONDecodeError):
            self._entries = []

    def _save(self):
        path = os.path.join(LOG_DIR, "recent.json")
        try:
            with open(path, "w") as f:
                json.dump(self._entries[-self._max:], f)
        except OSError:
            pass

    def log(self, level: str, source: str, message: str,
            data: Optional[Dict] = None, session_id: str = ""):
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "source": source,
            "message": message[:200],
            "data": data or {},
            "session_id": session_id,
        }
        self._entries.append(entry)
        if len(self._entries) > self._max * 2:
            self._entries = self._entries[-self._max:]
        self._save()

    def info(self, source: str, message: str, **kwargs):
        self.log("INFO", source, message, **kwargs)

    def warn(self, source: str, message: str, **kwargs):
        self.log("WARN", source, message, **kwargs)

    def error(self, source: str, message: str, **kwargs):
        self.log("ERROR", source, message, **kwargs)

    def debug(self, source: str, message: str, **kwargs):
        self.log("DEBUG", source, message, **kwargs)

    def query(self, level: str = "", source: str = "",
              session_id: str = "", limit: int = 50) -> List[Dict]:
        result = self._entries
        if level:
            result = [e for e in result if e["level"] == level.upper()]
        if source:
            result = [e for e in result if source.lower() in e["source"].lower()]
        if session_id:
            result = [e for e in result if e["session_id"] == session_id]
        return result[-limit:]

    def summary(self) -> Dict:
        """Quick overview of logstate."""
        total = len(self._entries)
        by_level = {}
        for e in self._entries:
            l = e["level"]
            by_level[l] = by_level.get(l, 0) + 1
        last_10 = self._entries[-10:] if total > 0 else []
        return {
            "total_entries": total,
            "by_level": by_level,
            "last_10": [{"time": e["timestamp"][:19], "level": e["level"],
                         "source": e["source"], "message": e["message"][:80]}
                        for e in last_10],
        }


# Global instance
_default_logger = None


def get_logger() -> WWLogger:
    global _default_logger
    if _default_logger is None:
        _default_logger = WWLogger()
    return _default_logger
