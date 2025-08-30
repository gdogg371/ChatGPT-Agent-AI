# File: v2/backend/core/prompt_pipeline/llm/providers.py
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)

def _save_json(obj: Any, path: Path) -> None:
    try:
        _ensure_dir(path.parent)
        path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        # best-effort only; never crash the pipeline because we couldn't write a file
        pass

def _to_dict(resp: Any) -> Dict[str, Any]:
    # OpenAI 1.x client returns Pydantic-like models with model_dump/json
    for attr in ("model_dump", "to_dict", "dict"):
        fn = getattr(resp, attr, None)
        if callable(fn):
            try:
                d = fn()
                if isinstance(d, dict):
                    return d
            except Exception:
                pass
    # Last resort: try JSON parse of a model_dump_json method
    for attr in ("model_dump_json",):
        fn = getattr(resp, attr, None)
        if callable(fn):
            try:
                return json.loads(fn())
            except Exception:
                pass
    # If it's already a dict
    if isinstance(resp, dict):
        return resp
    # Give up
    return {"raw": str(resp)}

def _openai_chat_complete(model: str, messages: List[Dict[str, Any]], ask_spec: Dict[str, Any]) -> Dict[str, Any]:
    # Import lazily to avoid hard dependency at import time
    try:
        from openai import OpenAI
    except Exception as e:
        return {"__provider_error__": f"OpenAI client import failed: {e}"}

    client = OpenAI()
    kwargs: Dict[str, Any] = {
        "model": model,
        "messages": messages,
    }

    # STRICTLY forward ask_spec parameters
    if "temperature" in ask_spec:
        kwargs["temperature"] = ask_spec["temperature"]
    if "top_p" in ask_spec:
        kwargs["top_p"] = ask_spec["top_p"]
    if "max_tokens" in ask_spec:
        kwargs["max_tokens"] = ask_spec["max_tokens"]
    if "response_format" in ask_spec:
        # <-- the thing you asked to be guaranteed
        kwargs["response_format"] = ask_spec["response_format"]

    try:
        resp = client.chat.completions.create(**kwargs)
        return _to_dict(resp)
    except Exception as e:
        return {"__provider_error__": f"OpenAI chat.completions.create failed: {e}"}

def complete_v1(payload: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> Any:
    """
    Capability: llm.complete.v1
    Inputs:
      - provider: "openai" (supported) | others (raise Problem)
      - model: model id
      - messages: OpenAI-style messages
      - ask_spec: {temperature, top_p, max_tokens, response_format, ...}
      - run_dir: optional path to persist raw results
    Returns:
      - raw provider response (dict) wrapped in a list (for consistency with batches)
        OR a Problem artifact on failure.
    """
    provider = payload.get("provider")
    model = payload.get("model")
    messages = payload.get("messages") or []
    ask_spec = payload.get("ask_spec") or {}
    run_dir = payload.get("run_dir")

    if provider != "openai":
        return [{
            "kind": "Problem",
            "uri": "spine://capability/llm.complete.v1",
            "meta": {"problem": {"code": "ProviderUnsupported", "message": f"Unsupported provider '{provider}'", "retryable": False, "details": {}}}
        }]

    result = _openai_chat_complete(model, messages, ask_spec)

    # Persist raw forensics
    if run_dir:
        _save_json({"provider": provider, "model": model, "messages": messages, "ask_spec": ask_spec, "result": result},
                   Path(run_dir) / "llm" / "result_single.json")

    return [result]

def complete_batches_v1(payload: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> Any:
    """
    Capability: llm.complete_batches.v1
    Inputs:
      - provider, model, ask_spec
      - batches: List[List[message]]  (each is a chat message sequence)
      - run_dir: optional path to persist raw results
    Returns:
      - List[raw provider response dicts] (one per batch)
    """
    provider = payload.get("provider")
    model = payload.get("model")
    batches = payload.get("batches") or []
    ask_spec = payload.get("ask_spec") or {}
    run_dir = payload.get("run_dir")

    if provider != "openai":
        return [{
            "kind": "Problem",
            "uri": "spine://capability/llm.complete_batches.v1",
            "meta": {"problem": {"code": "ProviderUnsupported", "message": f"Unsupported provider '{provider}'", "retryable": False, "details": {}}}
        }]

    results: List[Dict[str, Any]] = []
    for i, messages in enumerate(batches):
        result = _openai_chat_complete(model, messages, ask_spec)
        results.append(result)
        if run_dir:
            _save_json({"provider": provider, "model": model, "messages": messages, "ask_spec": ask_spec, "result": result},
                       Path(run_dir) / "llm" / f"result_batch_{i}.json")

    return results



