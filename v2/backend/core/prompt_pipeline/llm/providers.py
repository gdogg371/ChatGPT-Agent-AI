# v2/backend/core/prompt_pipeline/llm/providers.py
from __future__ import annotations
"""
LLM providers for the prompt pipeline.

Exports:
  - complete_batches_v1(task, context)  -> Artifact[list of {"id","raw"}]
  - complete_v1(task, context)          -> Artifact[{"id","raw"}]

Contract (what we return):
  meta = {
    "results": [
      {"id": "<str>", "raw": "<JSON string>"},
      ...
    ]
  }
Where each raw string is ONE JSON object shaped:
  {"id":"<id>","docstring":"<string>"}

This fits the engine’s post-LLM phases (sanitize → verify → patch).
"""

from typing import Any, Dict, List, Optional, Tuple
import json
import os
import traceback

# Spine contracts
try:
    from v2.backend.core.spine.contracts import Artifact  # type: ignore
except Exception:  # very defensive fallback
    class Artifact:  # type: ignore
        def __init__(self, kind: str, uri: str, sha256: str = "", meta: Dict[str, Any] = None):
            self.kind = kind
            self.uri = uri
            self.sha256 = sha256
            self.meta = meta or {}


# ----------------------------- Artifact helpers --------------------------------

def _ok(uri: str, meta: Dict[str, Any]) -> List[Artifact]:
    return [Artifact(kind="Result", uri=uri, sha256="", meta=meta)]

def _ng(uri: str, code: str, message: str, *, retryable: bool = False,
        details: Optional[Dict[str, Any]] = None) -> List[Artifact]:
    return [Artifact(kind="Problem", uri=uri, sha256="", meta={
        "problem": {"code": code, "message": message, "retryable": retryable, "details": details or {}}
    })]


# ----------------------------- Task/context utils ------------------------------

def _as_dict(x: Any) -> Dict[str, Any]:
    return x if isinstance(x, dict) else {}

def _task_payload(task: Any) -> Dict[str, Any]:
    if isinstance(task, dict):
        return task
    p = getattr(task, "payload", None)
    if isinstance(p, dict):
        return p
    for k in ("meta", "data"):
        v = getattr(task, k, None)
        if isinstance(v, dict):
            return v
    return {}

def _ctx_build_result(ctx: Any) -> Dict[str, Any]:
    try:
        s = ctx.get("state", {}) if isinstance(ctx, dict) else {}
        b = s.get("build", {}) if isinstance(s, dict) else {}
        return b.get("result") or {}
    except Exception:
        return {}

def _list_dicts(x: Any) -> List[Dict[str, Any]]:
    if isinstance(x, list):
        return [dict(i) for i in x if isinstance(i, dict)]
    return []


# ----------------------------- Secrets & config --------------------------------

def _resolve_openai_api_key() -> Optional[str]:
    # 1) Environment
    env = (os.environ.get("OPENAI_API_KEY") or
           os.environ.get("OpenAI_API_Key") or
           os.environ.get("openai_api_key"))
    if isinstance(env, str) and env.strip():
        return env.strip()

    # 2) Loader (authoritative)
    try:
        from v2.backend.core.configuration.loader import ConfigPaths, get_secrets  # type: ignore
        sec = get_secrets(ConfigPaths.detect())
        if hasattr(sec, "openai_api_key") and getattr(sec, "openai_api_key"):
            return str(getattr(sec, "openai_api_key")).strip()
        if isinstance(sec, dict):
            v = (sec.get("openai_api_key") or sec.get("OPENAI_API_KEY"))
            if isinstance(v, str) and v.strip():
                return v.strip()
            nested = sec.get("openai") or {}
            v = nested.get("api_key")
            if isinstance(v, str) and v.strip():
                return v.strip()
    except Exception:
        pass

    # 3) Canonical YAML fallback (no hard-coded key strings elsewhere)
    try:
        from pathlib import Path
        import yaml  # type: ignore
        here = Path(__file__).resolve()
        repo = here.parents[6] if len(here.parents) >= 7 else here.parents[-1]
        for p in [
            repo / "secret_management" / "secrets.yml",
            repo / "secret_management" / "secrets.yaml",
        ]:
            if p.is_file():
                data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                api = (
                    (data.get("openai") or {}).get("api_key")
                    or data.get("OPENAI_API_KEY")
                    or data.get("openai_api_key")
                )
                if isinstance(api, str) and api.strip():
                    return api.strip()
    except Exception:
        pass

    return None


# ----------------------------- OpenAI client -----------------------------------

def _init_openai_client(api_key: str) -> Tuple[Any, bool]:
    """
    Returns (client, is_modern).
      • modern: from openai import OpenAI; client.chat.completions.create(...)
      • legacy: import openai; openai.ChatCompletion.create(...)
    """
    try:
        from openai import OpenAI  # type: ignore
        client = OpenAI(api_key=api_key)
        return client, True
    except Exception:
        import openai  # type: ignore
        openai.api_key = api_key
        return openai, False

def _call_openai_chat(client: Any, is_modern: bool, *,
                      model: str,
                      messages: List[Dict[str, str]],
                      temperature: float,
                      json_mode: bool) -> str:
    if is_modern:
        kwargs: Dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        resp = client.chat.completions.create(**kwargs)  # type: ignore
        return (resp.choices[0].message.content or "").strip()
    else:
        kwargs = {"model": model, "messages": messages, "temperature": temperature}
        try:
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}
        except Exception:
            pass
        resp = client.ChatCompletion.create(**kwargs)  # type: ignore
        return (resp["choices"][0]["message"]["content"] or "").strip()


# ----------------------------- Message shaping ---------------------------------

def _to_chat_messages(obj: Any) -> List[Dict[str, str]]:
    """
    Accept:
      • dict {"system": "...", "user": "..."}
      • list [{"role","content"}, ...]
      • str "..."
    Return a valid messages list.
    """
    if isinstance(obj, dict):
        msgs: List[Dict[str, str]] = []
        if isinstance(obj.get("system"), str) and obj["system"].strip():
            msgs.append({"role": "system", "content": obj["system"]})
        if isinstance(obj.get("user"), str) and obj["user"].strip():
            msgs.append({"role": "user", "content": obj["user"]})
        return msgs or [{"role": "user", "content": ""}]
    if isinstance(obj, list) and obj and isinstance(obj[0], dict) and "role" in obj[0]:
        return obj
    if isinstance(obj, str) and obj.strip():
        return [{"role": "user", "content": obj.strip()}]
    return [{"role": "user", "content": ""}]

def _ensure_json_guardrail(messages: List[Dict[str, str]], json_mode: bool, item_id: Optional[str] = None) -> List[Dict[str, str]]:
    if not json_mode:
        return messages
    rules = (
        "Return ONLY a single valid JSON object. "
        "No prose, no code fences, no markdown."
    )
    if item_id is not None:
        rules += f" The object MUST be for id='{item_id}'."
    return messages + [{"role": "system", "content": rules}]


# ----------------------------- Synthesis helpers -------------------------------

def _resolve_model(ask_spec: Dict[str, Any], meta: Dict[str, Any]) -> str:
    return ask_spec.get("model") or meta.get("model") or "gpt-4o-mini"

def _resolve_temperature(ask_spec: Dict[str, Any]) -> float:
    try:
        return float(ask_spec.get("temperature", 0.0))
    except Exception:
        return 0.0

def _wants_json(ask_spec: Dict[str, Any]) -> bool:
    rf = ask_spec.get("response_format")
    if isinstance(rf, dict):
        return str(rf.get("type", "")).lower() == "json_object"
    return isinstance(rf, str) and rf.lower() == "json"

def _synthesize_items(payload: Dict[str, Any], context: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Returns (items, ask_spec).
    Each item is {"id": str, "messages": List[...]}.
    """
    ask_spec = dict(payload.get("ask_spec") or {})

    # Preferred: explicit batches[].items or items
    batches = payload.get("batches")
    if isinstance(batches, list) and batches:
        b0 = _as_dict(batches[0])
        its = _list_dicts(b0.get("items"))
        if its:
            out = []
            msgs = b0.get("messages") or payload.get("messages")
            for it in its:
                mid = str(it.get("id") or "")
                m = it.get("messages") or msgs
                if not mid:
                    continue
                out.append({"id": mid, "messages": _to_chat_messages(m)})
            if out:
                return out, ask_spec

    # Fallback: ids + shared messages (payload or context.build.result)
    ids = payload.get("ids") or []
    messages = payload.get("messages")
    if not ids or not messages:
        br = _ctx_build_result(context)
        ids = ids or br.get("ids") or []
        messages = messages or br.get("messages")

    if ids and messages:
        return [{"id": str(i), "messages": _to_chat_messages(messages)} for i in ids], ask_spec

    # Last resort: single item from top-level messages
    if messages:
        return [{"id": payload.get("id") or "item-0", "messages": _to_chat_messages(messages)}], ask_spec

    return [], ask_spec


# ----------------------------- Public providers --------------------------------

def complete_v1(task: Any, context: Dict[str, Any]) -> List[Artifact]:
    uri_ok = "spine://result/llm.complete.v1"
    uri_ng = "spine://problem/llm.complete.v1"

    try:
        payload = _task_payload(task)
        items, ask_spec = _synthesize_items(payload, _as_dict(context))
        if not items:
            return _ng(uri_ng, "InvalidPayload", "No resolvable messages/ids")

        api_key = _resolve_openai_api_key()
        if not api_key:
            return _ng(uri_ng, "MissingSecret",
                       "OPENAI_API_KEY not found via loader or secret_management/secrets.yml",
                       details={"where": "providers._resolve_openai_api_key"})

        client, is_modern = _init_openai_client(api_key)
        model = _resolve_model(ask_spec, payload)
        temperature = _resolve_temperature(ask_spec)
        json_mode = _wants_json(ask_spec) or True  # force JSON mode for this pipeline

        it = items[0]
        item_id = str(it["id"])
        messages = _ensure_json_guardrail(list(it["messages"]), json_mode, item_id=item_id)

        # Nudge exact output shape
        messages = messages + [{
            "role": "system",
            "content": (
                "Produce a single JSON object with keys:\n"
                " - id: string (must equal the id you are working on)\n"
                " - docstring: string (the complete docstring content to write)\n"
            )
        }]

        content = _call_openai_chat(
            client, is_modern,
            model=model,
            messages=messages,
            temperature=temperature,
            json_mode=json_mode,
        )

        return _ok(uri_ok, {"result": {"id": item_id, "raw": content}})

    except Exception as e:
        return _ng(uri_ng, "ProviderError", f"{type(e).__name__}: {e}",
                   details={"trace": traceback.format_exc()})


def complete_batches_v1(task: Any, context: Dict[str, Any]) -> List[Artifact]:
    uri_ok = "spine://result/llm.complete_batches.v1"
    uri_ng = "spine://problem/llm.complete_batches.v1"

    try:
        payload = _task_payload(task)
        ctx = _as_dict(context)
        items, ask_spec = _synthesize_items(payload, ctx)

        if not items:
            return _ng(uri_ng, "InvalidPayload",
                       "No resolvable 'items' nor 'ids+messages' (checked payload and context)")

        api_key = _resolve_openai_api_key()
        if not api_key:
            return _ng(uri_ng, "MissingSecret",
                       "OPENAI_API_KEY not found via loader or secret_management/secrets.yml",
                       details={"where": "providers._resolve_openai_api_key"})

        client, is_modern = _init_openai_client(api_key)
        model = _resolve_model(ask_spec, payload)
        temperature = _resolve_temperature(ask_spec)
        json_mode = _wants_json(ask_spec) or True  # strongly prefer JSON in this pipeline

        results: List[Dict[str, Any]] = []
        for it in items:
            item_id = str(it["id"])
            messages = _ensure_json_guardrail(list(it["messages"]), json_mode, item_id=item_id)
            messages = messages + [{
                "role": "system",
                "content": (
                    "Produce a single JSON object with keys:\n"
                    " - id: string (must equal the id you are working on)\n"
                    " - docstring: string (the complete docstring content to write)\n"
                )
            }]

            content = _call_openai_chat(
                client, is_modern,
                model=model,
                messages=messages,
                temperature=temperature,
                json_mode=json_mode,
            )
            results.append({"id": item_id, "raw": content})

        return _ok(uri_ok, {"results": results})

    except Exception as e:
        return _ng(uri_ng, "ProviderError", f"{type(e).__name__}: {e}",
                   details={"trace": traceback.format_exc()})




