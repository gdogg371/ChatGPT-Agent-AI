from __future__ import annotations

"""
LLM routing based on AskSpec (pipeline parameterisation).

Translates an AskSpec + global config into a fully resolved route
(model, temperature, max tokens, response format, tools).

This keeps the orchestrator simple and makes it easy to add new ask types
without invasive changes elsewhere.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional

from v2.backend.core.types.types import AskSpec, AskType
from v2.backend.core.configuration.config import PatchLoopConfig
from v2.backend.core.prompt_pipeline.llm.schema import get_response_format_by_name
from v2.backend.core.prompt_pipeline.executor.errors import AskSpecError


# Safe, opinionated defaults per ask type when neither AskSpec nor cfg.model specify a model.
# You can tune these centrally without touching the engine.
_DEFAULT_MODEL_BY_ASKTYPE: Dict[AskType, str] = {
    AskType.CODE_OPS: "gpt-4o-mini",
    AskType.QA: "gpt-4o-mini",
}


@dataclass(slots=True)
class LlmRoute:
    """A resolved provider-agnostic profile for a single LLM call/batch."""
    model: str
    temperature: float
    max_output_tokens: int
    response_format: Optional[Dict[str, Any]]  # provider-agnostic schema dict
    tools: list[Dict[str, Any]]
    tool_choice: Optional[str]


def _resolve_model(ask: AskSpec, cfg: PatchLoopConfig) -> str:
    # Priority: AskSpec.model → cfg.model (unless "auto") → default by ask type.
    if ask.model and isinstance(ask.model, str):
        return ask.model

    if cfg.model and cfg.model != "auto":
        return cfg.model

    fallback = _DEFAULT_MODEL_BY_ASKTYPE.get(ask.ask_type)
    if not fallback:
        # Extremely unlikely unless ask types grow and defaults aren't updated.
        raise AskSpecError(f"No default model configured for ask_type={ask.ask_type}")
    return fallback


def _resolve_temperature(ask: AskSpec, cfg: PatchLoopConfig) -> float:
    if ask.temperature is not None:
        return float(ask.temperature)
    # Keep a conservative default for code paths; slightly higher for QA if needed.
    return 0.1 if ask.ask_type == AskType.CODE_OPS else 0.2


def _resolve_max_output_tokens(ask: AskSpec, cfg: PatchLoopConfig) -> int:
    if ask.max_output_tokens is not None:
        return int(ask.max_output_tokens)
    # Fall back to cfg if present, otherwise a safety cap per type.
    if getattr(cfg, "max_output_tokens", None):
        return int(cfg.max_output_tokens)
    return 2048 if ask.ask_type == AskType.CODE_OPS else 1024


def _resolve_response_format(ask: AskSpec) -> Optional[Dict[str, Any]]:
    # Explicit object wins; else map from friendly name; else None (free-form).
    if ask.response_format is not None:
        return ask.response_format
    return get_response_format_by_name(ask.response_format_name)


def select_route(ask: AskSpec, cfg: PatchLoopConfig) -> LlmRoute:
    """
    Compute the final LLM route from AskSpec + global config.

    Raises
    ------
    AskSpecError
        When the AskSpec is inconsistent (e.g., invalid bounds) or we cannot
        derive a sane default (e.g., unknown ask type with no fallback).
    """
    try:
        # Validate AskSpec early for crisp failure modes.
        ask.validate()
    except Exception as e:
        raise AskSpecError(f"Invalid AskSpec: {e}") from e

    try:
        model = _resolve_model(ask, cfg)
        temperature = _resolve_temperature(ask, cfg)
        max_out = _resolve_max_output_tokens(ask, cfg)
        response_format = _resolve_response_format(ask)
        tools = ask.tools or []
        tool_choice = ask.tool_choice
    except AskSpecError:
        raise
    except Exception as e:
        raise AskSpecError(f"Failed to compute LLM route: {e}") from e

    return LlmRoute(
        model=model,
        temperature=temperature,
        max_output_tokens=max_out,
        response_format=response_format,
        tools=tools,
        tool_choice=tool_choice,
    )


__all__ = ["LlmRoute", "select_route"]
