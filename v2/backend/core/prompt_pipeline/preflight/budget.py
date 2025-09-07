# File: v2/backend/core/prompt_pipeline/preflight/budget.py
from __future__ import annotations

"""
Preflight budgeting utilities (domain-agnostic).

These helpers estimate token usage and clamp message payloads to a rough
budget to avoid provider-side errors for oversized requests.

NOTE: Estimation is heuristic. If your provider exposes a tokenizer/token
count API, prefer that and plug it in here later.
"""

from typing import Any, Dict, List, Tuple


# -------------------------- heuristics --------------------------

def _approx_tokens_for_text(s: str) -> int:
    """
    Very rough token estimator:
      - ~4 chars per token for English code/text blend (common rule of thumb)
    We round up to err on the safe side.
    """
    if not s:
        return 0
    # Avoid divide-by-zero and negative values
    n = max(1, len(s))
    return (n + 3) // 4


def _approx_tokens_for_messages(messages: List[Dict[str, str]]) -> int:
    """
    Sum content token estimates across chat messages.
    """
    total = 0
    for m in messages or []:
        content = m.get("content", "")
        total += _approx_tokens_for_text(str(content))
        # Small overhead per message (role, formatting)
        total += 3
    return total


# -------------------------- public API --------------------------

def estimate_tokens(messages: List[Dict[str, str]], model: str | None = None) -> int:
    """
    Estimate tokens for a chat message list.
    The `model` parameter is currently unused, but kept for compatibility
    with more precise future model-specific tokenizers.
    """
    return _approx_tokens_for_messages(messages)


def clamp_to_budget(
    messages: List[Dict[str, str]],
    max_input_tokens: int,
    *,
    prefer_keep_user_tail: bool = True,
) -> Tuple[List[Dict[str, str]], int]:
    """
    Clamp the messages to a rough token budget.

    Returns:
        (clamped_messages, estimated_tokens_after_clamp)
    """
    if max_input_tokens <= 0 or not messages:
        return messages, estimate_tokens(messages, None)

    est = estimate_tokens(messages, None)
    if est <= max_input_tokens:
        return messages, est

    # Strategy: drop/trim from the head (older system/assistant msgs) first,
    # keeping the tail user content which usually carries the concrete ask.
    remaining: List[Dict[str, str]] = []
    running = 0

    # Iterate from tail to head to prefer the newest content
    for m in reversed(messages):
        content = str(m.get("content", ""))
        t = _approx_tokens_for_text(content) + 3
        if running + t <= max_input_tokens:
            remaining.append(m)
            running += t
        else:
            # If we still have some slack, include a trimmed tail of this message
            slack = max_input_tokens - running
            if slack > 8:  # leave a small safety margin
                # Keep only the last ~4*slack chars (reverse of token est)
                approx_chars = slack * 4
                remaining.append({"role": m.get("role", "user"), "content": content[-approx_chars:]})
                running = max_input_tokens
        if running >= max_input_tokens:
            break

    remaining.reverse()
    return remaining, running
