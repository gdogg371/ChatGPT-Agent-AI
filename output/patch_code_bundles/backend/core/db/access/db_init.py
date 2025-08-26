#v2\backend\core\db\access\db_init.py
"""
Database initialization for writers/readers (YAML-only; no environment use).

- Source of truth for the SQLAlchemy engine/session comes from
  v2.backend.core.configuration.loader.get_db().url
- Platform agnostic; handles SQLite connect args.
- Provides a shared SessionLocal factory for use by writers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker, Session

from v2.backend.core.configuration.loader import get_db, ConfigError

_engine: Optional[Engine] = None
SessionLocal: sessionmaker[Session]  # initialized below


def _make_engine(url: str) -> Engine:
    kwargs: dict[str, Any] = {}
    if url.startswith("sqlite:///"):
        # Ensure parent directory exists for SQLite DB files
        db_path = url[len("sqlite:///") :]
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kwargs)


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        url = get_db().url  # fail-fast if db.yml missing/invalid
        if not isinstance(url, str) or not url.strip():
            raise ConfigError("Empty SQLAlchemy URL from loader.get_db().url")
        _engine = _make_engine(url)
    return _engine


# Initialize SessionLocal at import time using the YAML-backed engine.
SessionLocal = sessionmaker(bind=get_engine(), autoflush=False, autocommit=False)


def get_session() -> Session:
    """Convenience helper to create a new Session."""
    return SessionLocal()

