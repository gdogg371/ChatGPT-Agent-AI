#v2\backend\core\db\writers\docstring_writer.py
"""
DocstringWriter (YAML-backed DB; no env).

Minimal writer used by the docstring scanner to persist entries into the
'introspection_index' table via the project ORM.

- Uses the shared SQLAlchemy engine/session from db.access.db_init (which is
  sourced from YAML via the central loader).
- No environment variables and no hardcoded paths.
- Upsert semantics: insert by natural key; on conflict, update rolling fields.

Expected input row keys (as produced by the analyzer adapter):
  file            : repo-relative path (str)
  filetype        : "module" | "class" | "function" (str)
  line            : line number (int)
  description     : summarized one-liner (str)
  hash            : stable unique hash for this symbol (str)
  status          : lifecycle status (e.g., "todo", "active") (str)
  function/name   : symbol name (str), one of these keys will be present
  route/route_*   : ignored (for compatibility), not used here
  subdir          : optional (ignored by the ORM), kept for compatibility
  analyzer        : optional (ignored)

API:
  w = DocstringWriter(agent_id=<int>, mode="introspection_index")
  w.write(row_dict)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from v2.backend.core.db.access.db_init import get_session
from v2.models.introspection_index import (
    Base as _Base,            # noqa: F401  (ensures metadata accessible if needed)
    IntrospectionIndex,
)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class DocstringWriter:
    """
    Thin wrapper around SQLAlchemy session for writing introspection records.
    """

    def __init__(self, agent_id: int = 0, mode: str = "introspection_index") -> None:
        if mode != "introspection_index":
            raise ValueError("DocstringWriter only supports mode='introspection_index'")
        self.agent_id = int(agent_id)

    @staticmethod
    def _extract_name(row: dict[str, Any]) -> Optional[str]:
        # Prefer function, then route, then name
        for k in ("function", "route", "name"):
            v = row.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None

    def write(self, row: dict[str, Any]) -> None:
        """
        Insert or update a single docstring record.
        """
        if not isinstance(row, dict):
            raise TypeError("row must be a dict")

        filepath = str(row.get("file") or "").strip().replace("\\", "/")
        symbol_type = str(row.get("filetype") or "").strip()
        name = self._extract_name(row)
        lineno = int(row.get("line") or 0) or 0
        description = (row.get("description") or "").strip()
        unique_key_hash = (row.get("hash") or "").strip()
        status = (row.get("status") or "active").strip() or "active"

        if not filepath or not symbol_type:
            raise ValueError("row must include non-empty 'file' and 'filetype'")
        if name is None and symbol_type != "module":
            # For modules, name may be absent; for class/function we require a name
            raise ValueError("row missing symbol name ('function'|'route'|'name')")

        rec = IntrospectionIndex(
            filepath=filepath,
            symbol_type=symbol_type,
            name=name,
            lineno=lineno,
            description=description,
            unique_key_hash=unique_key_hash,
            status=status,
            discovered_at=_now_utc(),
            last_seen_at=_now_utc(),
            occurrences=1,
            recurrence_count=0,
        )

        # Upsert using natural key first; fall back to unique_key_hash if needed
        with get_session() as session:  # type: Session
            try:
                session.add(rec)
                session.commit()
                return
            except IntegrityError:
                session.rollback()
                self._update_existing(session, rec, prefer_hash=bool(unique_key_hash))
            except Exception:
                session.rollback()
                raise

    def _update_existing(self, session: Session, rec: IntrospectionIndex, *, prefer_hash: bool) -> None:
        """
        Update existing record matched on natural key or unique_key_hash.
        """
        existing: Optional[IntrospectionIndex] = None

        # Try natural key: filepath + symbol_type + name + lineno
        try:
            existing = (
                session.query(IntrospectionIndex)
                .filter(
                    IntrospectionIndex.filepath == rec.filepath,
                    IntrospectionIndex.symbol_type == rec.symbol_type,
                    IntrospectionIndex.name == rec.name,
                    IntrospectionIndex.lineno == rec.lineno,
                )
                .one_or_none()
            )
        except Exception:
            existing = None

        # Fall back to unique_key_hash if requested
        if existing is None and prefer_hash and rec.unique_key_hash:
            try:
                existing = (
                    session.query(IntrospectionIndex)
                    .filter(IntrospectionIndex.unique_key_hash == rec.unique_key_hash)
                    .one_or_none()
                )
            except Exception:
                existing = None

        if existing is None:
            # Could not locate; attempt a blind insert once more
            try:
                session.add(rec)
                session.commit()
                return
            except Exception:
                session.rollback()
                # Give up silently to keep writer tolerant; caller logs context
                return

        # Update rolling fields
        existing.last_seen_at = _now_utc()
        try:
            existing.occurrences = int(existing.occurrences or 0) + 1
        except Exception:
            existing.occurrences = 1

        # Prefer longer, non-placeholder descriptions
        new_desc = (rec.description or "").strip()
        old_desc = (existing.description or "").strip()
        if new_desc and new_desc != "Bad docstring" and len(new_desc) > len(old_desc):
            existing.description = new_desc

        if rec.status:
            existing.status = rec.status

        try:
            session.commit()
        except Exception:
            session.rollback()
