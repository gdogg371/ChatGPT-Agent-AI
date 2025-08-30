# File: v2/backend/core/prompt_pipeline/executor/prompts.py
from __future__ import annotations

"""
Generic prompt-building helpers for the executor.

Notes:
- This module MUST remain domain-agnostic.
- Adapters wired via Spine (e.g., `prompts.build.v1`) should be used for
  domain-specific prompting. These helpers are safe fallbacks/utilities.
"""

from typing import Any, Dict, Iterable, List, Optional, Sequence


# ----------------------------- builders ---------------------------------

def make_system_prompt(
    *,
    purpose: str = "structured code editing",
    instructions: Optional[Sequence[str]] = None,
) -> str:
    """
    Build a conservative system prompt describing the assistant role.
    """
    base = [
        f"You are a careful software assistant focused on {purpose}.",
        "Follow user instructions exactly and prefer deterministic outputs.",
        "When asked for JSON, return only valid JSON without extra commentary.",
    ]
    if instructions:
        base.extend([str(x) for x in instructions if x])
    return " ".join(base)


def make_user_prompt(
    *,
    task: str,
    guidance: Optional[str] = None,
    inputs: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Build a neutral user prompt body.

    Args:
        task: one-line description of what to produce.
        guidance: optional instructions about format/constraints.
        inputs: optional key/value inputs to echo for context.
    """
    lines: List[str] = [str(task).strip()]
    if guidance:
        lines.append("")
        lines.append(str(guidance).strip())
    if inputs:
        lines.append("")
        lines.append("Inputs:")
        for k, v in inputs.items():
            lines.append(f"- {k}: {v}")
    return "\n".join(lines).strip() + "\n"


def to_messages(system: str | None, user: str | None) -> List[Dict[str, str]]:
    """
    Convert simple system/user strings into chat message list shape.
    """
    msgs: List[Dict[str, str]] = []
    if system:
        msgs.append({"role": "system", "content": str(system)})
    if user:
        msgs.append({"role": "user", "content": str(user)})
    return msgs


def ensure_chat_messages(messages: Any) -> List[Dict[str, str]]:
    """
    Best-effort normalization to `[{"role": "...", "content": "..."}]`.
    Accepts:
      - list of dicts with role/content
      - {"system": "...", "user": "..."} mapping
      - tuple/list with two strings (system, user)
    """
    if isinstance(messages, list):
        out: List[Dict[str, str]] = []
        for m in messages:
            if isinstance(m, dict) and "role" in m and "content" in m:
                out.append({"role": str(m["role"]), "content": str(m["content"])})
        return out

    if isinstance(messages, dict):
        sys = messages.get("system")
        usr = messages.get("user")
        return to_messages(sys, usr)

    if isinstance(messages, (tuple, list)) and len(messages) == 2:
        sys, usr = messages
        return to_messages(sys, usr)

    return []


# ----------------------------- formatting --------------------------------

def add_json_return_guidance(base_user_prompt: str) -> str:
    """
    Append minimal guidance to return a deterministic JSON object with 'items'.
    """
    suffix = (
        "\n\nReturn a JSON object with an 'items' array, where each item is an object. "
        "Do not include commentary outside the JSON."
    )
    return (base_user_prompt or "").rstrip() + suffix


def clamp_messages(messages: List[Dict[str, str]], max_chars: int = 12000) -> List[Dict[str, str]]:
    """
    Clamp total characters across all message contents to avoid oversized payloads.
    Keeps as much of the tail (user) content as possible.
    """
    if max_chars <= 0 or not messages:
        return messages

    # simple greedy trim from the front
    total = sum(len(m.get("content", "")) for m in messages)
    if total <= max_chars:
        return messages

    trimmed: List[Dict[str, str]] = []
    running = 0
    # keep later messages (often include the concrete request) by iterating from end
    for m in reversed(messages):
        c = m.get("content", "")
        if running + len(c) <= max_chars:
            trimmed.append(m)
            running += len(c)
        else:
            # take a tail slice if we still have room
            remain = max_chars - running
            if remain > 0:
                trimmed.append({"role": m.get("role", "user"), "content": c[-remain:]})
                running = max_chars
        if running >= max_chars:
            break

    return list(reversed(trimmed))
