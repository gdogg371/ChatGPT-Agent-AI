# v2/backend/core/prompt_pipeline/llm/providers.py
from __future__ import annotations

r"""
LLM providers for the prompt_pipeline.

Exposes Spine capabilities:
- llm.complete_batches.v1  → complete_batches_v1
- llm.complete.v1          → complete_v1    (single-item adapter)

Features:
- OpenAI support with strict JSON mode when ask_spec.response_format == "json"
  * Uses OpenAI JSON mode via response_format={"type": "json_object"}
  * Adds a minimal system guardrail if needed to discourage prose/fences
- Tolerates both modern (OpenAI>=1.x) and legacy (openai<=0.x) Python clients
- Returns clean, list-shaped results: [{"id": <id>, "raw": <content>}, ...]
"""

from typing import Any, Dict, List, Optional
import os

from v2.backend.core.spine.contracts import Artifact, Task

# ----------------------------- helpers: artifacts ------------------------------

def _ok(uri: str, meta: Dict[str, Any]) -> List[Artifact]:
    return [Artifact(kind="Result", uri=uri, sha256="", meta=meta)]

def _ng(uri: str, code: str, message: str, *, retryable: bool = False, details: Optional[Dict[str, Any]] = None) -> List[Artifact]:
    return [Artifact(kind="Problem", uri=uri, sha256="", meta={
        "problem": {"code": code, "message": message, "retryable": retryable, "details": details or {}}
    })]

# ----------------------------- secrets resolution ------------------------------

def _resolve_openai_api_key() -> Optional[str]:
    """
    Resolve OpenAI API key from (in order):
      1) env var OPENAI_API_KEY (or OpenAI_API_Key)
      2) repo-level secret_management/secrets.yml (preferred)
      3) repo-level config/secrets.yml or config/spine/secrets.yml
      4) loader.get_secrets() (if present)
    """
    import os
    k = os.environ.get("OPENAI_API_KEY") or os.environ.get("OpenAI_API_Key")
    if k:
        return k

    # Try repo-local secrets files
    try:
        from pathlib import Path
        import yaml

        here = Path(__file__).resolve()
        # .../v2/backend/core/prompt_pipeline/llm/providers.py
        # parents[0]=llm, [1]=prompt_pipeline, [2]=core, [3]=backend, [4]=v2, [5]=repo root
        repo = here.parents[5] if len(here.parents) >= 6 else here.parent

        candidates = [
            repo / "secret_management" / "secrets.yml",
            repo / "secret_management" / "secrets.yaml",
            repo / "config" / "secrets.yml",
            repo / "config" / "spine" / "secrets.yml",
        ]

        for p in candidates:
            if p.is_file():
                try:
                    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                except Exception:
                    continue
                openai_block = data.get("openai") or {}
                api = openai_block.get("api_key")
                if api:
                    return api
    except Exception:
        pass

    # Fallback: project loader (if wired)
    try:
        from v2.backend.core.configuration.loader import get_secrets  # type: ignore
        secrets = get_secrets() or {}
        if isinstance(secrets, dict):
            s = secrets.get("openai") or {}
            k = s.get("api_key")
            if k:
                return k
    except Exception:
        pass

    return None


# ----------------------------- client bootstrap --------------------------------

def _init_openai_client(api_key: str):
    """
    Return (client, is_modern):
      - client is OpenAI() (modern) or module 'openai' (legacy)
      - is_modern True when using `from openai import OpenAI`
    """
    try:
        from openai import OpenAI  # type: ignore
        client = OpenAI(api_key=api_key)
        return client, True
    except Exception:
        import openai  # type: ignore
        openai.api_key = api_key
        return openai, False

def _maybe_append_json_guardrail(msgs: List[Dict[str, str]], want_json: bool) -> List[Dict[str, str]]:
    """If strict JSON requested but no explicit system directive exists, append a minimal guardrail."""
    if not want_json:
        return msgs
    has_directive = any(
        m.get("role") == "system" and "json" in (m.get("content", "").lower())
        for m in msgs
    )
    if not has_directive:
        msgs = msgs + [{
            "role": "system",
            "content": (
                "Return ONLY a single valid JSON object with no prose, no code fences, "
                "and no surrounding text."
            ),
        }]
    return msgs

def _call_openai_chat(client, is_modern: bool, model: str, messages: List[Dict[str, str]], ask_spec: Dict[str, Any]) -> str:
    """
    Make a chat completion call to OpenAI and return message content.
    Enforces strict JSON if ask_spec.response_format == 'json'.
    """
    temperature = ask_spec.get("temperature", 0)
    want_json = str(ask_spec.get("response_format", "")).lower() == "json"

    if is_modern:
        kwargs = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if want_json:
            kwargs["response_format"] = {"type": "json_object"}
        resp = client.chat.completions.create(**kwargs)
        if not getattr(resp, "choices", None):
            return ""
        return getattr(resp.choices[0].message, "content", "") or ""

    # Legacy client
    kwargs = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if want_json:
        # Most legacy clients accept this; if not, it will be ignored.
        kwargs["response_format"] = {"type": "json_object"}
    resp = client.ChatCompletion.create(**kwargs)  # type: ignore[attr-defined]
    choices = getattr(resp, "choices", None) or (resp.get("choices") if isinstance(resp, dict) else None)
    if not choices:
        return ""
    msg = choices[0].get("message") if isinstance(choices[0], dict) else getattr(choices[0], "message", None)
    if isinstance(msg, dict):
        return msg.get("content") or ""
    return getattr(msg, "content", "") or ""

# ----------------------------- shared core -------------------------------------

def _run_openai_batches(payload: Dict[str, Any]) -> List[Artifact]:
    provider = str(payload.get("provider") or "").strip().lower()
    model = str(payload.get("model") or "").strip()
    ask_spec_default = dict(payload.get("ask_spec") or {})
    batches = payload.get("batches") or []

    if provider != "openai":
        return _ng("spine://problem/llm.complete_batches.v1", "UnsupportedProvider", f"Provider '{provider}' not supported in this build.")
    if not isinstance(batches, list) or not batches:
        return _ng("spine://problem/llm.complete_batches.v1", "InvalidPayload", "Missing or empty 'batches'")
    if not model:
        return _ng("spine://problem/llm.complete_batches.v1", "InvalidPayload", "Missing 'model'")

    api_key = _resolve_openai_api_key()
    if not api_key:
        return _ng("spine://problem/llm.complete_batches.v1", "MissingSecret", "OPENAI_API_KEY not configured")

    client, is_modern = _init_openai_client(api_key)

    results: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    for idx, item in enumerate(batches):
        try:
            msgs = list(item.get("messages") or [])
            if not msgs:
                raise ValueError("batch item missing 'messages'")
            item_ask = dict(item.get("ask_spec") or {}) or ask_spec_default
            want_json = str(item_ask.get("response_format", "")).lower() == "json"
            msgs = _maybe_append_json_guardrail(msgs, want_json)

            content = _call_openai_chat(client, is_modern, model, msgs, item_ask)
            results.append({"id": item.get("id", idx), "raw": content})
        except Exception as e:
            errors.append({"index": idx, "error": f"{type(e).__name__}: {e}"})

    meta: Dict[str, Any] = {"results": results}
    if errors:
        meta["errors"] = errors
    return _ok("spine://result/llm.complete_batches.v1", meta)

# ----------------------------- capability entrypoints --------------------------

def complete_batches_v1(task: Task, context: Dict[str, Any]) -> List[Artifact]:
    """
    Batch chat completions.
    Expects payload:
      {
        "provider": "openai",
        "model": "...",
        "batches": [ {"messages":[...], "ask_spec": {...}, "id": <opt>}, ... ],
        "ask_spec": {...}   # default, optional
      }
    """
    p = task.payload or {}
    return _run_openai_batches(p)

def complete_v1(task: Task, context: Dict[str, Any]) -> List[Artifact]:
    """
    Single-item chat completion adapter for registries that target llm.complete.v1.
    Expects payload:
      { "provider": "...", "model": "...", "messages": [...], "ask_spec": {...}, "id": <opt> }
    Internally calls the batch implementation with one element.
    """
    p = task.payload or {}
    single_batch = [{
        "messages": list(p.get("messages") or []),
        "ask_spec": dict(p.get("ask_spec") or {}),
        "id": p.get("id", 0),
    }]
    batched = {
        "provider": p.get("provider"),
        "model": p.get("model"),
        "batches": single_batch,
        "ask_spec": dict(p.get("ask_spec") or {}),
    }
    return _run_openai_batches(batched)

__all__ = ["complete_batches_v1", "complete_v1"]


