# File: v2/models/agent_insights.py
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional, Literal

import json
from sqlalchemy import (
    TIMESTAMP,
    Integer,
    String,
    Text,
    Float,
    Boolean,
    func,
    Index,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# ----------------------------- SQLAlchemy Base --------------------------------

class Base(DeclarativeBase):
    """Local declarative base for ORM models in this bundle."""
    pass


# ----------------------------- Type Aliases -----------------------------------

StatusType = Literal["active", "deprecated", "removed"]


# ------------------------------- ORM Model ------------------------------------

class AgentInsight(Base):
    """
    Stores insights generated about the codebase, agents, or runs.
    Ties to an agent (or run) id, has typed content, and optional metadata.
    """
    __tablename__ = "agent_insights"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Foreign-like linkage (kept as int to avoid cross-schema coupling here)
    agent_id: Mapped[int] = mapped_column(Integer, nullable=False)

    # Domain classification
    insight_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(64), default="internal")

    # Core payload
    content: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[Optional[float]] = mapped_column(Float)

    # JSON metadata (string column with JSON-encoded dict)
    mdata: Mapped[str] = mapped_column(Text, default="{}")

    # Optional code-location anchoring for the insight
    filepath: Mapped[Optional[str]] = mapped_column(Text)
    symbol_name: Mapped[Optional[str]] = mapped_column(Text)
    line_number: Mapped[int] = mapped_column(Integer, default=0)

    # De-duplication / identity
    unique_key_hash: Mapped[Optional[str]] = mapped_column(Text)

    # Lifecycle
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    discovered_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())
    resolved_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP)

    # Counts
    occurrences: Mapped[int] = mapped_column(Integer, default=1)
    recurrence_count: Mapped[int] = mapped_column(Integer, default=0)

    # Audit
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Review workflow
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reviewer: Mapped[Optional[str]] = mapped_column(String(128))
    review_comment: Mapped[Optional[str]] = mapped_column(Text)

    __table_args__ = (
        Index("idx_agent_insights_agent_id", "agent_id"),
        Index("idx_agent_insights_type", "insight_type"),
        Index("idx_agent_insights_reviewed", "reviewed"),
        Index("idx_agent_insights_key_hash", "unique_key_hash"),
        UniqueConstraint("agent_id", "unique_key_hash", name="uq_agent_insights_agent_key"),
    )

    # ------------- Convenience accessors for JSON metadata --------------------

    @property
    def mdata_obj(self) -> Dict[str, Any]:
        try:
            return json.loads(self.mdata or "{}")
        except Exception:
            return {}

    @mdata_obj.setter
    def mdata_obj(self, value: Dict[str, Any]) -> None:
        self.mdata = json.dumps(value or {})


# ------------------------------ Pydantic DTOs ---------------------------------

from pydantic import BaseModel, Field, field_validator  # noqa: E402


class AgentInsightIn(BaseModel):
    agent_id: int
    insight_type: str
    content: str
    source: str = "internal"
    score: Optional[float] = Field(None, ge=0.0, le=1.0)
    mdata: Dict[str, Any] = Field(default_factory=dict)

    filepath: Optional[str] = None
    symbol_name: Optional[str] = None
    line_number: int = 0
    unique_key_hash: Optional[str] = None

    status: StatusType = "active"
    discovered_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None

    occurrences: int = 1
    recurrence_count: int = 0

    reviewed: bool = False
    reviewer: Optional[str] = None
    review_comment: Optional[str] = None

    @field_validator("mdata", mode="before")
    @classmethod
    def _coerce_mdata(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                return {}
        return v or {}


class AgentInsightOut(AgentInsightIn):
    id: int
    created_at: datetime
    updated_at: datetime

    # Pydantic v2: replacement for pydantic v1's orm_mode
    model_config = {"from_attributes": True}


__all__ = ["Base", "AgentInsight", "AgentInsightIn", "AgentInsightOut"]
