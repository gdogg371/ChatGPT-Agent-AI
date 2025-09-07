# src/packager/core/errors.py
from __future__ import annotations
from pathlib import Path
from typing import Optional, Mapping, Any

__all__ = [
    "PackagerError",
    "ConfigError",
    "TraversalError",
    "NormalizationError",
    "WriteError",
    "IntegrityError",
    "IngestionError",
    "PromptError",
]

class PackagerError(Exception):
    """
    Base class for all packager errors.

    Args:
        message: Human-readable description.
        path: Optional filesystem path relevant to the error.
        cause: Optional underlying exception.
        details: Optional extra context (will be shown in __str__).
    """
    def __init__(
        self,
        message: str,
        *,
        path: Optional[Path] = None,
        cause: Optional[BaseException] = None,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.path = path
        self.cause = cause
        self.details = dict(details) if details else None

    def __str__(self) -> str:
        parts = [self.message]
        ctx = []
        if self.path:
            ctx.append(f"path={self.path}")
        if self.cause:
            ctx.append(f"cause={type(self.cause).__name__}: {self.cause}")
        if self.details:
            ctx.append(f"details={self.details}")
        if ctx:
            parts.append(f"({', '.join(ctx)})")
        return " ".join(parts)

class ConfigError(PackagerError):
    """Invalid or inconsistent configuration detected."""

class TraversalError(PackagerError):
    """Filesystem traversal failed (permissions, symlink issues, missing paths, etc.)."""

class NormalizationError(PackagerError):
    """Normalization failed (encoding, newline policy, or content transforms)."""

class WriteError(PackagerError):
    """Failed to write an output artifact or intermediate file."""

class IntegrityError(PackagerError):
    """Checksum or integrity verification failed."""

class IngestionError(PackagerError):
    """External source ingestion (copy into codebase/) failed."""

class PromptError(PackagerError):
    """Prompt pack embedding failed (dir/zip/inline)."""
