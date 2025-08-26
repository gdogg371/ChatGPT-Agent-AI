#v2\backend\core\prompt_pipeline\llm\providers.py
r"""
LLM provider adapters.

Purpose
- Take a Spine task payload produced by the Engine/Prompts layer and call the
  selected LLM provider.
- Forward ask_spec settings faithfully (no hidden defaults), including strict
  JSON mode when `ask_spec.response_format == "json"`.

Why this change
- Engine was failing later with “Unbalanced JSON braces in response”. The most
  robust fix is to ask the provider to emit proper JSON in the first place.
  This module now maps `ask_spec.response_format: json` to the provider’s
  native JSON mode (OpenAI: response_format={"type":"json_object"}).

Public targets (capabilities.yml)
- llm.complete.v1          → complete_v1(task, context)
- llm.complete_batches.v1  → complete_batches_v1(task, context)

Notes
- No environment variables are used; secrets are read via loader.get_secrets().
- Minimal surface: we implement OpenAI chat completions; other providers can be
  added without touching callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from v2.backend.core.spine.contracts import Artifact, Task  # Spine types
from v2.backend.core.configuration.loader import get_secrets, ConfigError

# OpenAI SDK (>=1.0)
try:
    from openai import OpenAI  # type: ignore
except Exception as e:  # pragma: no cover
    OpenAI = None  # type: ignore


# ------------------------------- helpers --------------------------------------

def _problem(uri: str, code: str, message: str, retryable: bool = False, details: Optional[Dict[str, Any]] = None) -> List[Artifact]:
    return [
        Artifact(
            kind="Problem",
            uri=uri,
            sha256="",
            meta={"problem": {"code": code, "message": message, "retryable": retryable, "details": details or {}}},
        )
    ]


def _result(uri: str, meta: Dict[str, Any]) -> List[Artifact]:
    return [Artifact(kind="Result", uri=uri, sha256="", meta=meta)]


def _bool(x: Any, default: bool = False) -> bool:
    if isinstance(x, bool):
        return x
    if isinstance(x, str):
        s = x.strip().lower()
        if s in {"1", "true", "yes", "on"}:
            return True
        if s in {"0", "false", "no", "off"}:
            return False
    return default


def _ensure_messages(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    """
    Accepts either:
      - messages: list[{role, content}]
      - prompt: str  → converted to a single user message
    """
    msgs = payload.get("messages")
    if isinstance(msgs, list) and msgs and isinstance(msgs[0], dict):
        # Shallow-validate role/content
        out: List[Dict[str, str]] = []
        for m in msgs:
            role = str(m.get("role") or "").strip() or "user"
            content = str(m.get("content") or "")
            out.append({"role": role, "content": content})
        return out

    prompt = payload.get("prompt")
    if isinstance(prompt, str) and prompt:
        return [{"role": "user", "content": prompt}]
    return [{"role": "user", "content": ""}]


def _openai_json_mode_args(ask_spec: Dict[str, Any]) -> Dict[str, Any]:
    """
    Map ask_spec.response_format → OpenAI response_format argument.
    Supported:
      - response_format: "json" → {"type": "json_object"}
      - response_format: {"type":"json_object"} → pass-through
    """
    rf = ask_spec.get("response_format")
    if isinstance(rf, str) and rf.strip().lower() == "json":
        return {"response_format": {"type": "json_object"}}
    if isinstance(rf, dict) and rf.get("type") == "json_object":
        return {"response_format": {"type": "json_object"}}
    return {}


def _openai_client() -> OpenAI:
    if OpenAI is None:
        raise RuntimeError("OpenAI SDK is not installed. Please `pip install openai>=1.0`.")
    try:
        key = get_secrets().openai_api_key
    except ConfigError as e:
        raise RuntimeError(f"Secrets error: {e}") from e
    return OpenAI(api_key=key)


# ------------------------------- providers ------------------------------------

def _call_openai(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Invoke OpenAI Chat Completions with strict forwarding of ask_spec.
    Expected payload keys:
      - model (str), messages (list[{role, content}])
      - ask_spec (dict): may include temperature, top_p, max_tokens, response_format, etc.
    """
    client = _openai_client()

    model = str(payload.get("model") or "").strip()
    if not model:
        raise ValueError("Missing 'model' for OpenAI call")

    messages = _ensure_messages(payload)
    ask_spec = dict(payload.get("ask_spec") or {})

    # Base args
    args: Dict[str, Any] = {
        "model": model,
        "messages": messages,
    }

    # Simple scalar forwards (only if present)
    if "temperature" in ask_spec:
        args["temperature"] = ask_spec["temperature"]
    if "top_p" in ask_spec:
        args["top_p"] = ask_spec["top_p"]
    if "max_tokens" in ask_spec:
        args["max_tokens"] = ask_spec["max_tokens"]

    # Enable JSON mode if requested
    args.update(_openai_json_mode_args(ask_spec))

    # Create completion
    resp = client.chat.completions.create(**args)

    # Normalize output
    choice = (resp.choices or [None])[0]
    text = ""
    finish_reason = None
    if choice and getattr(choice, "message", None) is not None:
        text = str(choice.message.content or "")
        finish_reason = getattr(choice, "finish_reason", None)

    return {
        "provider": "openai",
        "model": model,
        "raw": text,
        "finish_reason": finish_reason,
        "usage": {
            "prompt_tokens": getattr(resp.usage, "prompt_tokens", None),
            "completion_tokens": getattr(resp.usage, "completion_tokens", None),
            "total_tokens": getattr(resp.usage, "total_tokens", None),
        },
    }


# ------------------------------- capability targets ---------------------------

def complete_v1(task: Task, context: Dict[str, Any]) -> List[Artifact]:
    """
    Single-completion provider entrypoint.

    Payload (required):
      provider: "openai" (currently supported)
      model: str
      messages: list[{role, content}]  OR  prompt: str
      ask_spec: dict (optional; forwarded)
    """
    uri = "spine://result/llm.complete.v1"
    p = dict(task.payload or {})
    try:
        provider = str(p.get("provider") or "").strip().lower()
        if provider in {"openai", "oai", "openai-chat"}:
            meta = _call_openai(p)
            return _result(uri, {"result": meta})

        return _problem("spine://problem/llm.complete.v1", "UnsupportedProvider", f"Unsupported provider: {provider}")

    except Exception as e:
        return _problem("spine://problem/llm.complete.v1", "UnhandledError", f"{type(e).__name__}: {e}")


def complete_batches_v1(task: Task, context: Dict[str, Any]) -> List[Artifact]:
    """
    Batch-completion provider entrypoint.

    Payload (required):
      provider: "openai"
      model: str
      batches: list[ { messages|prompt, ask_spec? }, ... ]
      ask_spec: dict (optional; default for the batch items)
    """
    uri_ok = "spine://result/llm.complete_batches.v1"
    uri_ng = "spine://problem/llm.complete_batches.v1"
    p = dict(task.payload or {})
    try:
        provider = str(p.get("provider") or "").strip().lower()
        if provider not in {"openai", "oai", "openai-chat"}:
            return _problem(uri_ng, "UnsupportedProvider", f"Unsupported provider: {provider}")

        batches = p.get("batches")
        if not isinstance(batches, list) or not batches:
            return _problem(uri_ng, "InvalidPayload", "Missing or empty 'batches'")

        model = str(p.get("model") or "").strip()
        default_ask = dict(p.get("ask_spec") or {})

        results: List[Dict[str, Any]] = []
        for item in batches:
            if not isinstance(item, dict):
                results.append({"error": "InvalidBatchItem"})
                continue
            merged = dict(item)
            # Ensure provider/model present
            merged["provider"] = "openai"
            merged["model"] = merged.get("model") or model
            # Merge ask_spec (item overrides default)
            ia = dict(default_ask)
            ia.update(dict(merged.get("ask_spec") or {}))
            merged["ask_spec"] = ia
            try:
                results.append(_call_openai(merged))
            except Exception as e:
                results.append({"error": f"{type(e).__name__}: {e}"})

        return _result(uri_ok, {"results": results})

    except Exception as e:
        return _problem(uri_ng, "UnhandledError", f"{type(e).__name__}: {e}")


__all__ = ["complete_v1", "complete_batches_v1"]
