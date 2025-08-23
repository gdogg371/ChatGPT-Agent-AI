# File: v2/backend/core/prompt_pipeline/executor/prompts.py
from __future__ import annotations

"""
Prompt utilities scoped to the prompt_pipeline executor.

Design goals
------------
- No cross-package imports (e.g., no direct docstrings/* references).
- Provide a tiny, well-typed structure for LLM-ready messages.
- Offer helpers to assemble a "packed prompt" object that other layers
  (engine/providers or Spine steps) can pass around or persist as JSONL.

Shapes
------
PromptMessages:
  {"system": str, "user": str}

PackedPrompt:
  {
    "messages": {"system": str, "user": str},
    "batch": [ {...item...}, ... ],
    "ids": ["id1","id2", ...]    # optional convenience list
  }

Nothing here calls external services or reads files; callers decide where
to persist or how to route these payloads (e.g., via the Spine).
"""

from dataclasses import dataclass, asdict
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional
import json


# ----------------------------- data model -------------------------------------


@dataclass(slots=True)
class PromptMessages:
    """Minimal container for LLM chat messages."""
    system: str
    user: str

    def to_dict(self) -> Dict[str, str]:
        return {"system": str(self.system or ""), "user": str(self.user or "")}

    def preview(self, max_len: int = 400) -> str:
        s = (self.system or "").strip().replace("\r\n", "\n")
        u = (self.user or "").strip().replace("\r\n", "\n")
        joined = f"{s}\n\n{u}".strip()
        if len(joined) <= max_len:
            return joined
        return joined[: max(0, max_len - 3)] + "..."


def make_messages(system: str, user: str) -> PromptMessages:
    """Factory with defensive str() coercion."""
    return PromptMessages(system=str(system or ""), user=str(user or ""))


# ----------------------------- packing helpers --------------------------------


def pack_prompt(
    *,
    messages: PromptMessages | Mapping[str, str],
    batch: Iterable[Mapping[str, Any]],
    include_ids: bool = True,
) -> Dict[str, Any]:
    """
    Create a PackedPrompt dict from messages + an iterable of items.

    Parameters
    ----------
    messages : PromptMessages | Mapping[str, str]
        Either our dataclass or a plain dict with 'system'/'user' keys.
    batch : Iterable[Mapping[str, Any]]
        Items destined for the LLM. Each item MUST have a stable 'id' for
        downstream correlation (this function does not enforce uniqueness).
    include_ids : bool
        When True, add a top-level 'ids' list extracted from the batch.

    Returns
    -------
    Dict[str, Any] with keys: messages, batch, (optional) ids.
    """
    if isinstance(messages, PromptMessages):
        msgs = messages.to_dict()
    else:
        # Make a defensive copy (avoid mutating caller dict)
        msgs = {
            "system": str((messages.get("system") if isinstance(messages, Mapping) else "") or ""),
            "user": str((messages.get("user") if isinstance(messages, Mapping) else "") or ""),
        }

    batch_list: List[Dict[str, Any]] = [dict(it) for it in batch]
    packed: Dict[str, Any] = {"messages": msgs, "batch": batch_list}

    if include_ids:
        ids = [str(it.get("id")) for it in batch_list if "id" in it]
        packed["ids"] = ids
    return packed


def to_jsonl_lines(packed_prompt: Mapping[str, Any]) -> List[str]:
    """
    Convert a packed prompt to JSONL lines, one per batch item, preserving
    the full messages with each line so downstream workers can be stateless.

    Line shape:
      {"id":"...", "messages":{"system":"...","user":"..."}, "item":{...}}
    """
    messages = packed_prompt.get("messages") or {}
    batch = packed_prompt.get("batch") or []
    out: List[str] = []
    for i, it in enumerate(batch):
        line = {
            "id": str(it.get("id", i)),
            "messages": {
                "system": str((messages.get("system") if isinstance(messages, Mapping) else "") or ""),
                "user": str((messages.get("user") if isinstance(messages, Mapping) else "") or ""),
            },
            "item": it,
        }
        out.append(json.dumps(line, ensure_ascii=False))
    return out


# ----------------------------- light validation --------------------------------


def assert_nonempty_messages(messages: Mapping[str, Any]) -> None:
    """
    Raise ValueError if 'user' is empty (system is allowed to be empty).
    """
    user = (messages.get("user") if isinstance(messages, Mapping) else None) or ""
    if not str(user).strip():
        raise ValueError("user message must not be empty")


def normalize_messages(obj: PromptMessages | Mapping[str, Any]) -> PromptMessages:
    """
    Accepts either a PromptMessages or a mapping; returns a normalized PromptMessages.
    """
    if isinstance(obj, PromptMessages):
        return obj
    return PromptMessages(system=str(obj.get("system") or ""), user=str(obj.get("user") or ""))


# ----------------------------- convenience API --------------------------------


def build_packed(
    system: str,
    user: str,
    items: Iterable[Mapping[str, Any]],
    *,
    validate: bool = True,
) -> Dict[str, Any]:
    """
    One-shot convenience to produce a PackedPrompt from raw strings + items.
    """
    msgs = make_messages(system, user)
    if validate:
        assert_nonempty_messages(msgs.to_dict())
    return pack_prompt(messages=msgs, batch=items, include_ids=True)

