"""Persistent JSON job history (data/seen_jobs.json) with atomic writes + JSON helpers."""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .utils import utcnow_iso

log = logging.getLogger(__name__)

STATUS_NOTIFIED = "notified"  # email delivered
STATUS_SCORED = "scored"      # evaluated by AI and below the notification threshold


class HistoryError(Exception):
    pass


def load_json(path: Path, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as exc:
        raise HistoryError(f"{path} is not valid JSON: {exc}") from exc


def save_json_atomic(path: Path, data: Any) -> None:
    """Write via temp file + os.replace so a crash never leaves a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


class JobHistory:
    """
    Layout:
      {"version": 1, "jobs": {"<primary key>": {"keys": [...], "title":..., "company":..., "url":...,
                                               "status": "notified"|"scored", "score": 83,
                                               "first_seen": ISO, "notified_at": ISO|null}}}
    A job is a duplicate if ANY of its keys (normalized URL, source:id, fingerprint) is known.
    """

    def __init__(self, path: Path, jobs: Optional[Dict[str, dict]] = None):
        self.path = Path(path)
        self.jobs: Dict[str, dict] = jobs or {}
        self._index = self._build_index()

    @classmethod
    def load(cls, path: Path) -> "JobHistory":
        data = load_json(path, {"version": 1, "jobs": {}})
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), dict):
            # Refuse to continue with a damaged history: starting empty would re-send every old job.
            raise HistoryError(f"{path} has an unexpected structure")
        return cls(path, data["jobs"])

    def _build_index(self) -> Dict[str, str]:
        return {k: primary for primary, rec in self.jobs.items() for k in rec.get("keys", [])}

    def __len__(self) -> int:
        return len(self.jobs)

    def contains_any(self, keys: Iterable[str]) -> bool:
        return any(k in self._index for k in keys)

    def add(self, *, keys: list, title: str, company: str, url: str, status: str, score: Optional[int]) -> None:
        primary = keys[0]
        now = utcnow_iso()
        existing = self.jobs.get(self._index.get(primary, primary), {})
        self.jobs[primary] = {
            "keys": sorted(set(keys) | set(existing.get("keys", []))),
            "title": title,
            "company": company,
            "url": url,
            "status": status,
            "score": score,
            "first_seen": existing.get("first_seen", now),
            "notified_at": now if status == STATUS_NOTIFIED else existing.get("notified_at"),
        }
        for k in self.jobs[primary]["keys"]:
            self._index[k] = primary

    def prune(self, retention_days: int) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        stale = []
        for primary, rec in self.jobs.items():
            try:
                seen = datetime.fromisoformat(rec["first_seen"])
            except (KeyError, ValueError):
                continue
            if seen < cutoff:
                stale.append(primary)
        for primary in stale:
            del self.jobs[primary]
        if stale:
            self._index = self._build_index()
        return len(stale)

    def save(self) -> None:
        save_json_atomic(self.path, {"version": 1, "jobs": self.jobs})
