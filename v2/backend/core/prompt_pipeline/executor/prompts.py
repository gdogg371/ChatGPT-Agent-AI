#v2\backend\core\prompt_pipeline\executor\prompts.py
from __future__ import annotations
r"""
Prompt utilities used by the prompt_pipeline executor.

This module provides:
- A tiny, well-typed container for chat messages.
- Helpers to pack prompts/batches to JSON-safe dicts/JSONL.
- Bridge functions `build_system_prompt` / `build_user_prompt` that
  delegate to `steps.py` and (optionally) enforce JSON-only output
  when `ask_spec.response_format == "json"`.

Design notes
- No hardcoded provider params; `ask_spec` drives behavior.
- No environment variables. Pure functions over inputs.
"""

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional
import json

# Reuse canonical builders / unpacking logic from steps.py to avoid duplication.
from .steps import build_system_prompt as _base_system_prompt
from .steps import build_user_prompt as _base_user_prompt


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


# ----------------------------- JSON-only enforcement --------------------------

_JSON_ENFORCER = (
    "Respond ONLY with a single minified JSON object. "
    "Do not include markdown code fences, comments, or extra text."
)

def _augment_for_json_mode(system_text: str, ask_spec: Mapping[str, Any]) -> str:
    rf = ask_spec.get("response_format")
    if (isinstance(rf, str) and rf.strip().lower() == "json") or (
        isinstance(rf, Mapping) and str(rf.get("type", "")).lower() == "json_object"
    ):
        return (system_text.rstrip() + "\n\n" + _JSON_ENFORCER).strip()
    return system_text


# ----------------------------- builder bridges --------------------------------

def build_system_prompt(ask_spec: Mapping[str, Any], cfg: Any = None) -> str:
    """
    Delegate to steps.build_system_prompt(), then append an explicit JSON-only
    instruction when ask_spec.response_format requests JSON mode.
    """
    base = _base_system_prompt()
    return _augment_for_json_mode(base, ask_spec)


def build_user_prompt(items: List[Dict[str, Any]], ask_spec: Mapping[str, Any], cfg: Any = None) -> str:
    """
    Delegate to steps.build_user_prompt(items). We do not currently need to
    modify the user prompt for JSON mode, since the system prompt dictates the
    schema. Kept symmetrical with build_system_prompt for future extensions.
    """
    return _base_user_prompt(items)


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
    Convert a packed prompt to JSONL lines, one per batch item, preserving the
    full messages with each line so downstream workers can be stateless.

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
    """Raise ValueError if 'user' is empty (system is allowed to be empty)."""
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
    """One-shot convenience to produce a PackedPrompt from raw strings + items."""
    msgs = make_messages(system, user)
    if validate:
        assert_nonempty_messages(msgs.to_dict())
    return pack_prompt(messages=msgs, batch=items, include_ids=True)


# ----------------------------- back-compat alias -------------------------------

# Older call-sites: from ...executor.prompts import build_batches
build_batches = build_packed

__all__ = [
    "PromptMessages",
    "make_messages",
    "pack_prompt",
    "to_jsonl_lines",
    "assert_nonempty_messages",
    "normalize_messages",
    "build_packed",
    "build_batches",
    "build_system_prompt",
    "build_user_prompt",
]

