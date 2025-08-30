"""
Introspection Index Models
Captures structural code symbols (functions, classes, routes) and their relationships.

Schema: introspection_index
"""

from typing import Optional
from pydantic import BaseModel, Field
from datetime import datetime
from sqlalchemy import (
        Column, Integer, String, Text, TIMESTAMP
    )
from sqlalchemy.sql import func
from sqlalchemy.ext.declarative import declarative_base
Base = declarative_base()
SQLALCHEMY_AVAILABLE = True

# ---- SQLAlchemy ORM Model ----



if SQLALCHEMY_AVAILABLE:
    class IntrospectionIndex(Base):
        __tablename__ = "introspection_index"

        id = Column(Integer, primary_key=True)
        filepath = Column(Text, nullable=False)
        symbol_type = Column(String(64), nullable=False, default="unknown")
        name = Column(Text)
        lineno = Column(Integer, default=0)
        route_method = Column(String(16))
        route_path = Column(Text)
        ag_tag = Column(String(32))
        description = Column(Text)
        target_symbol = Column(Text)
        relation_type = Column(String(32))

        unique_key_hash = Column(Text, nullable=True)
        status = Column(String(32), default="active")
        discovered_at = Column(TIMESTAMP, server_default=func.now())
        last_seen_at = Column(TIMESTAMP, server_default=func.now())
        resolved_at = Column(TIMESTAMP, nullable=True)

        occurrences = Column(Integer, default=1)
        recurrence_count = Column(Integer, default=0)

        created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
        updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)


# ---- Pydantic Models ----

class IntrospectionIndexIn(BaseModel):
    filepath: str
    symbol_type: str = "unknown"
    name: Optional[str] = None
    lineno: int = 0
    route_method: Optional[str] = None
    route_path: Optional[str] = None
    ag_tag: Optional[str] = None
    description: Optional[str] = None
    target_symbol: Optional[str] = None
    relation_type: Optional[str] = None

    unique_key_hash: Optional[str] = None
    status: str = "active"
    discovered_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None

    occurrences: int = 1
    recurrence_count: int = 0


class IntrospectionIndexOut(IntrospectionIndexIn):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True


