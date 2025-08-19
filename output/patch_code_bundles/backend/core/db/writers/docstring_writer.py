"""
Docstring Writer
Uses SQLAlchemy ORM to insert docstring records into introspection_index or agent_insights.
"""

from datetime import datetime, timezone
from typing import Dict, Any
from sqlalchemy.orm import Session
from v2.backend.core.db.access.db_init import get_sqlalchemy_session
from v2.models.introspection_index_OLD import IntrospectionIndex
from v2.models.agent_insights import AgentInsight


class DocstringWriter:
    def __init__(self, agent_id: int = 1, mode: str = "introspection_index"):
        self.agent_id = agent_id
        self.mode = mode  # "introspection_index" or "agent_insights"
        self.session: Session = get_sqlalchemy_session()

    def write(self, row: Dict[str, Any]):
        """
        Write a docstring record using ORM.
        Expected keys: analyzer, subdir, file, line, filetype, class, function, summary
        """
        now = datetime.now(timezone.utc)  # ✅ correct usage — no need for datetime.datetime

        if self.mode == "introspection_index":
            record = IntrospectionIndex(**{
                "filepath": row.get("file"),
                "symbol_type": row.get("filetype", "unknown"),
                "name": row.get("function") or row.get("route") or row.get("name"),
                "lineno": int(row.get("line", 0)),
                "route_method": row.get("route_method"),
                "route_path": row.get("route_path"),
                "ag_tag": row.get("ag_tag") or "Docstring",
                "description": row.get("description") or "",
                "target_symbol": row.get("target"),
                "relation_type": row.get("relation"),
                "unique_key_hash": row.get("hash"),
                "status": row.get("status", "active"),
                "discovered_at": now,
                "last_seen_at": now,
                "resolved_at": None,
                "occurrences": 1,
                "recurrence_count": 0
            })

        elif self.mode == "agent_insights":
            record = AgentInsight(**{
                "agent_id": self.agent_id,
                "insight_type": row.get("analyzer", "Docstring"),
                "content": row.get("summary", ""),
                "source": row.get("subdir", ""),
                "score": None,
                "mdata": {},
                "filepath": row.get("file"),
                "symbol_name": row.get("function") or row.get("route") or row.get("name"),
                "line_number": int(row.get("line", 0)),
                "unique_key_hash": None,
                "status": "active",
                "discovered_at": now,
                "last_seen_at": now,
                "resolved_at": None,
                "occurrences": 1,
                "recurrence_count": 0,
                "reviewed": False,
                "reviewer": None,
                "review_comment": None
                # Let DB handle created_at and updated_at
            })

        else:
            raise ValueError(f"Unsupported docstring writer mode: {self.mode}")

        self.session.add(record)
        self.session.commit()
