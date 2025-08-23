from __future__ import annotations
from typing import Optional, Any, Dict, Literal
from datetime import datetime
import json

from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Text, Integer, Float, Boolean, TIMESTAMP, func, Index, UniqueConstraint

class Base(DeclarativeBase):
    pass

StatusType = Literal["active", "deprecated", "removed"]

class AgentInsight(Base):
    __tablename__ = "agent_insights"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_id: Mapped[int] = mapped_column(Integer, nullable=False)
    insight_type: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String, default="internal")

    score: Mapped[Optional[float]] = mapped_column(Float)
    mdata: Mapped[str] = mapped_column(Text, default="{}")  # JSON as text

    filepath: Mapped[Optional[str]] = mapped_column(Text)
    symbol_name: Mapped[Optional[str]] = mapped_column(Text)
    line_number: Mapped[int] = mapped_column(Integer, default=0)

    unique_key_hash: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)

    discovered_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())
    resolved_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP)

    occurrences: Mapped[int] = mapped_column(Integer, default=1)
    recurrence_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)

    reviewed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reviewer: Mapped[Optional[str]] = mapped_column(String)
    review_comment: Mapped[Optional[str]] = mapped_column(Text)

    __table_args__ = (
        Index("idx_agent_insights_agent_id", "agent_id"),
        Index("idx_agent_insights_type", "insight_type"),
        Index("idx_agent_insights_reviewed", "reviewed"),
        Index("idx_agent_insights_key_hash", "unique_key_hash"),
        UniqueConstraint("agent_id", "unique_key_hash", name="uq_agent_insights_agent_key"),
    )

    @property
    def mdata_obj(self) -> Dict[str, Any]:
        try:
            return json.loads(self.mdata or "{}")
        except Exception:
            return {}

    @mdata_obj.setter
    def mdata_obj(self, value: Dict[str, Any]) -> None:
        self.mdata = json.dumps(value or {})


# ---------- Pydantic DTOs ----------
from pydantic import BaseModel, Field, validator

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

    @validator("mdata", pre=True)
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

    class Config:
        orm_mode = True
