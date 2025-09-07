# File: v2/backend/core/prompt_pipeline/llm/client.py
"""
Domain-agnostic LLM client.

Public entry points:
  - complete_v1(provider, model, messages, ask_spec, api_key=None) -> str
  - complete(provider, model, messages, ask_spec, api_key=None) -> str  (compat)
  - run(provider, model, messages, ask_spec, api_key=None) -> str       (compat)

Notes:
- For provider="openai", this uses the Chat Completions API.
- The caller is responsible for supplying `api_key` (we do not read env here).
- If the HTTP call fails, we return a deterministic JSON diagnostic so the
  pipeline can continue.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError


# -------------------------- utilities --------------------------

def _shim_response_format(ask_spec: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert legacy ask_spec.response_format into OpenAI's expected shape when possible.
    - If ask_spec["response_format"] == "json", map to {"type": "json_object"}.
    - Otherwise, pass through unchanged.
    """
    out = dict(ask_spec or {})
    rf = out.get("response_format")
    if isinstance(rf, str) and rf.lower() == "json":
        out["response_format"] = {"type": "json_object"}
    return out


def _messages_to_openai(messages: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """
    Ensure each message is {role, content} string as the OpenAI API expects.
    """
    out: List[Dict[str, str]] = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role", "user"))
        content = m.get("content")
        if isinstance(content, (dict, list)):
            content = json.dumps(content, ensure_ascii=False)
        if content is None:
            content = ""
        out.append({"role": role, "content": str(content)})
    return out


def _mock_items_json(n: int = 1) -> str:
    items = []
    for i in range(max(1, int(n))):
        rid = f"mock-{i+1}"
        body = "Mock body. Replace with provider output."
        items.append({"id": rid, "result": body, "docstring": body, "mode": "rewrite"})
    return json.dumps({"items": items, "schema": "generic.v1"}, ensure_ascii=False)


def _http_post_json(url: str, headers: Dict[str, str], payload: Dict[str, Any]) -> str:
    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, headers=headers, method="POST")
    with urlopen(req, timeout=120) as resp:
        return resp.read().decode("utf-8")


# --------------------------- providers ---------------------------

def _openai_complete(model: str, messages: List[Dict[str, Any]], ask_spec: Dict[str, Any], *, api_key: str, base_url: Optional[str] = None) -> str:
    """
    Call OpenAI Chat Completions and return the top message content (string).
    """
    if not api_key or not isinstance(api_key, str):
        raise RuntimeError("OpenAI API key is required and must be a non-empty string")

    url = (base_url or ask_spec.get("base_url") or "").strip() or "https://api.openai.com/v1/chat/completions"
    ask = _shim_response_format(ask_spec or {})
    payload: Dict[str, Any] = {
        "model": model,
        "messages": _messages_to_openai(messages),
    }
    # Merge a conservative subset of ask_spec keys if present
    for k in ("temperature", "top_p", "max_tokens", "n", "response_format", "stop", "frequency_penalty", "presence_penalty"):
        if k in ask:
            payload[k] = ask[k]

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    raw = _http_post_json(url, headers, payload)
    obj = json.loads(raw)

    # Try to extract primary content; if absent, return the raw JSON
    choices = obj.get("choices") if isinstance(obj, dict) else None
    if isinstance(choices, list) and choices:
        msg = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            return str(msg["content"])

    # Fallback: return the JSON string (engine/parser can handle it)
    return raw


# --------------------------- public API ---------------------------

def complete_v1(provider: str, model: str, messages: List[Dict[str, Any]], ask_spec: Dict[str, Any], api_key: Optional[str] = None) -> str:
    """
    Preferred entry point used by llm.providers.
    Returns a *string* (raw model text). On failure, returns a deterministic mock JSON.
    """
    prov = (provider or "").lower().strip()
    try:
        if prov == "openai":
            if not api_key:
                raise RuntimeError("OPENAI_API_KEY missing (secrets not supplied to client)")
            return _openai_complete(model, messages, ask_spec, api_key=api_key)
        elif prov in ("mock", "", "none"):
            n = (ask_spec or {}).get("n", 1)
            return _mock_items_json(int(n) if isinstance(n, int) else 1)
        else:
            # Unknown provider — soft fallback to mock to keep pipeline alive
            n = (ask_spec or {}).get("n", 1)
            return _mock_items_json(int(n) if isinstance(n, int) else 1)
    except (HTTPError, URLError, TimeoutError, RuntimeError, ValueError) as e:
        diag = {
            "error": "provider_error",
            "provider": prov,
            "model": model,
            "message": str(e),
        }
        return json.dumps({"items": [], "diagnostic": diag, "schema": "generic.v1"}, ensure_ascii=False)


def complete(provider: str, model: str, messages: List[Dict[str, Any]], ask_spec: Dict[str, Any], api_key: Optional[str] = None) -> str:
    return complete_v1(provider, model, messages, ask_spec, api_key=api_key)


def run(provider: str, model: str, messages: List[Dict[str, Any]], ask_spec: Dict[str, Any], api_key: Optional[str] = None) -> str:
    return complete_v1(provider, model, messages, ask_spec, api_key=api_key)

