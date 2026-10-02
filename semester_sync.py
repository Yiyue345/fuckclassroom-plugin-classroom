from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .client import ClassroomClient, ClassroomClientError, Term
from fuckclassroom.core.atomic import atomic_write_text


SEMESTER_CACHE_TTL_SECONDS = 6 * 60 * 60


@dataclass(frozen=True)
class SemesterSnapshot:
    terms: tuple[Term, ...]
    current_term_id: str
    updated_at: datetime
    from_cache: bool = False
    stale: bool = False
    error: str | None = None

    @property
    def current_term(self) -> Term | None:
        return next((term for term in self.terms if term.id == self.current_term_id), None)


class SemesterSyncService:
    def __init__(
        self,
        client: ClassroomClient,
        cache_path: Path,
        *,
        ttl_seconds: int = SEMESTER_CACHE_TTL_SECONDS,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.client = client
        self.cache_path = cache_path
        self.ttl = timedelta(seconds=max(1, ttl_seconds))
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()

    def sync(self, *, force: bool = False) -> SemesterSnapshot:
        with self._lock:
            cached = self._load_cache()
            if cached is not None and not force and not self._is_expired(cached):
                return replace(cached, from_cache=True)

            try:
                terms = tuple(self.client.refresh_terms())
            except ClassroomClientError as exc:
                if cached is None:
                    raise
                return replace(
                    cached,
                    from_cache=True,
                    stale=self._is_expired(cached),
                    error=str(exc),
                )

            snapshot = SemesterSnapshot(
                terms=terms,
                current_term_id=_current_term_id(terms, self._now()),
                updated_at=self._now(),
            )
            self._save_cache(snapshot)
            return snapshot

    def _is_expired(self, snapshot: SemesterSnapshot) -> bool:
        return self._now() - snapshot.updated_at >= self.ttl

    def _load_cache(self) -> SemesterSnapshot | None:
        if not self.cache_path.exists():
            return None
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            updated_at = datetime.fromisoformat(str(payload["updated_at"]))
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            rows = payload.get("terms")
            if not isinstance(rows, list):
                return None
            terms = tuple(_term_from_cache(row) for row in rows if isinstance(row, dict))
            current_term_id = str(payload.get("current_term_id") or "")
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
        return SemesterSnapshot(
            terms=terms,
            current_term_id=current_term_id,
            updated_at=updated_at,
            from_cache=True,
        )

    def _save_cache(self, snapshot: SemesterSnapshot) -> None:
        payload = {
            "updated_at": snapshot.updated_at.isoformat(),
            "current_term_id": snapshot.current_term_id,
            "terms": [asdict(term) for term in snapshot.terms],
        }
        atomic_write_text(
            self.cache_path,
            json.dumps(payload, ensure_ascii=False, indent=2),
        )


def _current_term_id(terms: tuple[Term, ...], now: datetime) -> str:
    current = next((term for term in terms if term.current), None)
    if current is not None:
        return current.id

    today = now.date()
    for term in terms:
        try:
            begin = datetime.strptime(term.begin_date, "%Y-%m-%d").date()
            end = datetime.strptime(term.end_date, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        if begin <= today <= end:
            return term.id
    return ""


def _term_from_cache(row: dict[str, object]) -> Term:
    raw = row.get("raw")
    return Term(
        id=str(row.get("id") or ""),
        name=str(row.get("name") or ""),
        year=str(row.get("year") or ""),
        season=str(row.get("season") or ""),
        begin_date=str(row.get("begin_date") or ""),
        end_date=str(row.get("end_date") or ""),
        current=bool(row.get("current")),
        raw=dict(raw) if isinstance(raw, dict) else {},
    )
