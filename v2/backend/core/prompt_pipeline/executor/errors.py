# File: v2/backend/core/prompt_pipeline/executor/errors.py
from __future__ import annotations


class PipelineError(Exception):
    """Generic pipeline failure (recoverable by outer runners)."""


class OrchestratorError(PipelineError):
    """High-level orchestration failure (e.g., step wiring, fatal config)."""


class IoError(PipelineError):
    """Filesystem and path-related failures (missing files, outside scan root, etc.)."""


class ValidationError(PipelineError):
    """Response/shape validation failure from parsing/unpacking steps."""


class LlmClientError(PipelineError):
    """LLM provider/transport errors (HTTP, auth, rate limiting)."""


class AskSpecError(PipelineError):
    """Invalid or unsupported AskSpec / routing configuration."""



