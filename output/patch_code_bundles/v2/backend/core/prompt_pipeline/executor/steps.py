# File: v2/backend/core/prompt_pipeline/executor/steps.py
"""
Generic helper steps for the prompt pipeline.

This module is intentionally *domain-agnostic*. It avoids embedding any
domain-specific (e.g., docstrings) assumptions in prompts or payload shapes.

Notes:
- The main engine now delegates prompt/content building to adapters via Spine
  (e.g., `prompts.build.v1`, `context.build`), so these helpers are optional.
- We keep them as safe, neutral utilities to preserve import stability for any
  legacy callers that still reference them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


# ---------------------------- prompt helpers ----------------------------

def build_system_prompt(meta: Optional[Dict[str, Any]] = None) -> str:
    """
    Return a *generic* system prompt. Adapters should override via Spine.
    """
    meta = meta or {}
    purpose = meta.get("purpose", "structured editing")
    return (
        "You are a careful software assistant for {purpose}. "
        "Follow the user instructions exactly, return JSON if asked, "
        "and avoid including any unspecified fields."
    ).format(purpose=purpose)


def build_user_prompt(context: Optional[Dict[str, Any]] = None) -> str:
    """
    Return a *generic* user prompt body. Adapters should override via Spine.
    """
    ctx = context or {}
    task = ctx.get("task", "apply well-formed changes")
    guidance = ctx.get(
        "guidance",
        "Provide results in a deterministic JSON object with an 'items' array of objects."
    )
    return f"{task}\n\n{guidance}\n"


# ----------------------------- steps (legacy) -----------------------------

@dataclass
class BuildContextStep:
    """
    Legacy no-op context builder kept for compatibility.
    The engine now calls `context.build` via Spine.
    """

    options: Optional[Dict[str, Any]] = None

    def run(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not isinstance(items, list):
            items = []
        # Pass-through; adapters enrich via Spine.
        return {"items": items}


@dataclass
class UnpackResultsStep:
    """
    Legacy no-op unpack step kept for compatibility.
    The engine now expects adapters (via Spine) to unpack domain-specific results.
    """

    options: Optional[Dict[str, Any]] = None

    def run(self, raw_items: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not isinstance(raw_items, list):
            raw_items = []
        # Return items unchanged; adapter-based unpacking should occur via Spine.
        return {"items": raw_items}
