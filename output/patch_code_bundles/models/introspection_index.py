# File: v2/models/introspection_index.py
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Literal, Optional

import json
from sqlalchemy import (
    TIMESTAMP,
    Integer,
    String,
    Text,
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

SymbolType = Literal["module", "class", "function", "route", "unknown"]
StatusType = Literal["active", "deprecated", "removed"]


# ------------------------------- ORM Model ------------------------------------

class IntrospectionIndex(Base):
    """
    Canonical index of code symbols discovered during introspection scans.
    Stores minimal identity, location, routing info, and lightweight metadata.
    """
    __tablename__ = "introspection_index"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Identity / location
    filepath: Mapped[str] = mapped_column(Text, nullable=False)
    symbol_type: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    name: Mapped[Optional[str]] = mapped_column(Text)
    lineno: Mapped[int] = mapped_column(Integer, default=0)

    # HTTP route info (for web routes, if applicable)
    route_method: Mapped[Optional[str]] = mapped_column(String(16))
    route_path: Mapped[Optional[str]] = mapped_column(Text)

    # Project tagging / description
    ag_tag: Mapped[Optional[str]] = mapped_column(String(64))
    description: Mapped[Optional[str]] = mapped_column(Text)

    # Relations (for dependency/lineage graphs)
    target_symbol: Mapped[Optional[str]] = mapped_column(Text)
    relation_type: Mapped[Optional[str]] = mapped_column(String(32))

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

    # JSON metadata (string column with JSON-encoded dict)
    mdata: Mapped[str] = mapped_column(Text, default="{}")

    __table_args__ = (
        Index("idx_introspect_file_symbol", "filepath", "symbol_type"),
        Index("idx_introspect_relation_type", "relation_type"),
        Index("idx_introspect_ag_tag", "ag_tag"),
        Index("idx_introspect_key_hash", "unique_key_hash"),
        UniqueConstraint("unique_key_hash", name="uq_introspect_key"),
        UniqueConstraint("filepath", "symbol_type", "name", "lineno", name="uq_introspect_natural"),
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


class IntrospectionIndexIn(BaseModel):
    filepath: str
    symbol_type: SymbolType = "unknown"
    name: Optional[str] = None
    lineno: int = 0

    route_method: Optional[str] = None
    route_path: Optional[str] = None

    ag_tag: Optional[str] = None
    description: Optional[str] = None

    target_symbol: Optional[str] = None
    relation_type: Optional[str] = None

    unique_key_hash: Optional[str] = None
    status: StatusType = "active"

    discovered_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None

    occurrences: int = 1
    recurrence_count: int = 0

    mdata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("mdata", mode="before")
    @classmethod
    def _coerce_mdata(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                return {}
        return v or {}


class IntrospectionIndexOut(IntrospectionIndexIn):
    id: int
    created_at: datetime
    updated_at: datetime

    # Pydantic v2: replacement for pydantic v1's orm_mode
    model_config = {"from_attributes": True}


__all__ = ["Base", "IntrospectionIndex", "IntrospectionIndexIn", "IntrospectionIndexOut"]

