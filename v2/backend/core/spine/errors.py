# backend/core/spine/errors.py
from __future__ import annotations

class SpineError(Exception):
    """Base exception for spine boot/loader failures (not for business logic)."""

class ConfigError(SpineError):
    """Invalid or missing configuration (e.g., capabilities.yml)."""

class CapabilityNotFound(SpineError):
    """Requested capability is not registered."""

class TargetImportError(SpineError):
    """Target could not be imported/resolved."""

class ValidationError(SpineError):
    """Request or response failed validation (spine-level)."""
