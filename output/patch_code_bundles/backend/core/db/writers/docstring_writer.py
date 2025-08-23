"""Docstring Writer
Uses SQLAlchemy ORM to insert/update docstring records into:
- introspection_index  (default)
- agent_insights
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Any, Optional

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from v2.backend.core.db.access.db_init import get_sqlalchemy_session
from v2.models.introspection_index_OLD import IntrospectionIndex
from v2.models.agent_insights import AgentInsight


class DocstringWriter:
    def __init__(self, agent_id: int = 1, mode: str = "introspection_index"):
        self.agent_id = agent_id
        self.mode = mode  # "introspection_index" or "agent_insights"
        self.session: Session = get_sqlalchemy_session()

    # ------------ helpers -----------------------------------------------------

    @staticmethod
    def _coerce_int(v: Any, default: int = 0) -> int:
        try:
            return int(v)
        except Exception:
            return default

    @staticmethod
    def _symbol_name_from_row(row: Dict[str, Any]) -> Optional[str]:
        return row.get("function") or row.get("route") or row.get("name")

    # ------------ upsert for introspection_index ------------------------------

    def _upsert_introspection(self, row: Dict[str, Any], now: datetime) -> None:
        """Try INSERT; on UNIQUE conflict, UPDATE existing row (last_seen_at, occurrences, maybe description)."""
        record = IntrospectionIndex(
            filepath=row.get("file"),
            symbol_type=row.get("filetype", "unknown"),
            name=self._symbol_name_from_row(row),
            lineno=self._coerce_int(row.get("line"), 0),
            route_method=row.get("route_method"),
            route_path=row.get("route_path"),
            ag_tag=row.get("ag_tag") or "Docstring",
            description=row.get("description") or "",
            target_symbol=row.get("target"),
            relation_type=row.get("relation"),
            unique_key_hash=row.get("hash"),
            status=row.get("status", "active"),
            discovered_at=now,
            last_seen_at=now,
            resolved_at=None,
            occurrences=1,
            recurrence_count=0,
        )

        try:
            self.session.add(record)
            self.session.commit()
            return
        except IntegrityError:
            # Duplicate (violated uq_introspect_natural or uq_introspect_key) -> update existing row
            self.session.rollback()

            # Try resolving by the natural key first
            existing: Optional[IntrospectionIndex] = (
                self.session.query(IntrospectionIndex)
                .filter(
                    IntrospectionIndex.filepath == record.filepath,
                    IntrospectionIndex.symbol_type == record.symbol_type,
                    IntrospectionIndex.name == record.name,
                    IntrospectionIndex.lineno == record.lineno,
                )
                .one_or_none()
            )

            # Fallback: match by unique_key_hash if present
            if existing is None and record.unique_key_hash:
                existing = (
                    self.session.query(IntrospectionIndex)
                    .filter(IntrospectionIndex.unique_key_hash == record.unique_key_hash)
                    .one_or_none()
                )

            if existing is not None:
                # Bump last_seen/occurrences; only replace description if the new one is better (longer & not "Bad docstring")
                existing.last_seen_at = now
                try:
                    existing.occurrences = int(existing.occurrences or 0) + 1
                except Exception:
                    existing.occurrences = 1

                new_desc = (record.description or "").strip()
                old_desc = (existing.description or "").strip()
                if new_desc and new_desc != "Bad docstring" and len(new_desc) > len(old_desc):
                    existing.description = new_desc

                # keep status "active" unless caller passed something else explicitly
                explicit_status = row.get("status")
                if isinstance(explicit_status, str) and explicit_status:
                    existing.status = explicit_status

                try:
                    self.session.commit()
                except Exception:
                    self.session.rollback()
                return

            # Could not locate row to update; give up quietly
            return
        except Exception:
            # Any other error: rollback and continue
            self.session.rollback()
            return

    # ------------ write() public API ------------------------------------------

    def write(self, row: Dict[str, Any]) -> None:
        """
        Write a record using ORM.

        Expected keys for introspection_index mode:
          - file, filetype, line, description, hash
          - one of: function | route | name
          - optional: route_method, route_path, ag_tag, target, relation, status

        For agent_insights mode:
          - analyzer (insight_type), summary (content), subdir (source), file, function/route/name -> symbol_name
        """
        now = datetime.now(timezone.utc)

        if self.mode == "introspection_index":
            self._upsert_introspection(row, now)
            return

        if self.mode == "agent_insights":
            record = AgentInsight(
                agent_id=self.agent_id,
                insight_type=row.get("analyzer", "Docstring"),
                content=row.get("summary", "") or (row.get("description", "") or ""),
                source=row.get("subdir", "") or "internal",
                score=None,
                mdata="{}",  # keep simple; caller can extend later
                filepath=row.get("file"),
                symbol_name=self._symbol_name_from_row(row),
                line_number=self._coerce_int(row.get("line"), 0),
                unique_key_hash=None,
                status="active",
                discovered_at=now,
                last_seen_at=now,
                resolved_at=None,
                occurrences=1,
                recurrence_count=0,
                reviewed=False,
                reviewer=None,
                review_comment=None,
            )
            try:
                self.session.add(record)
                self.session.commit()
            except IntegrityError:
                # Duplicate agent insight (by (agent_id, unique_key_hash) if used) -> ignore
                self.session.rollback()
            except Exception:
                self.session.rollback()
            return

        raise ValueError(f"Unsupported docstring writer mode: {self.mode}")

