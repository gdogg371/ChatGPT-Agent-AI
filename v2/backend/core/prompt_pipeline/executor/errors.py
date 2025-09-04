# File: v2/backend/core/prompt_pipeline/executor/errors.py
from __future__ import annotations

"""
Generic error types and helpers for the prompt pipeline executor.

This module is intentionally domain-agnostic. Use these exceptions in providers
and engine code to signal structured, catchable failures.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional


# -------------------------- Exceptions --------------------------

class PipelineError(RuntimeError):
    """Base class for pipeline-related errors."""


class CapabilityError(PipelineError):
    """Raised when a Spine capability is missing or fails."""


class ProviderError(PipelineError):
    """Raised when an underlying provider (LLM, DB, etc.) fails."""


class ValidationError(PipelineError):
    """Raised when inputs/outputs violate expected shapes."""


# -------------------------- Problem helper --------------------------

@dataclass
class ProblemSpec:
    code: str
    message: str
    retryable: bool = False
    details: Optional[Dict[str, Any]] = None


def to_problem_meta(spec: ProblemSpec) -> Dict[str, Any]:
    """
    Convert a ProblemSpec to a canonical 'problem' meta mapping that matches
    the Spine Problem artifact expectation (no imports required).
    """
    return {
        "problem": {
            "code": spec.code,
            "message": spec.message,
            "retryable": bool(spec.retryable),
            "details": dict(spec.details or {}),
        }
    }




