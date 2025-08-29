# File: v2/backend/core/prompt_pipeline/llm/providers.py
"""
LLM providers façade (domain-agnostic).

Implements Spine capabilities:
  - llm.complete.v1
  - llm.complete_batches.v1

Behavior:
- Reads OpenAI API key directly from `secret_management/secrets.yml|yaml` under the
  provided `root` (or `project_root`) — NO environment variables.
- Passes the key to the client; client does not read env.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml  # PyYAML
except Exception as e:  # pragma: no cover
    yaml = None  # we'll error clearly if needed

from .client import complete_v1 as client_complete


# ----------------------------- secrets loader -----------------------------

def _read_yaml(p: Path) -> Dict[str, Any]:
    if not p.exists():
        return {}
    if yaml is None:
        raise RuntimeError("PyYAML is required to read secrets (`pip install pyyaml`)")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def _load_openai_key(project_root: Path) -> Optional[str]:
    """
    Try several shapes:
      secret_management/secrets.yml(.yaml):
        - openai: { api_key: "..." }   # preferred
        - openai_api_key: "..."
        - OPENAI_API_KEY: "..."
    """
    sm_dir = project_root / "secret_management"
    for name in ("secrets.yml", "secrets.yaml"):
        data = _read_yaml(sm_dir / name)
        if not data:
            continue
        # nested
        if isinstance(data.get("openai"), dict):
            k = data["openai"].get("api_key") or data["openai"].get("key")
            if isinstance(k, str) and k.strip():
                return k.strip()
        # flat
        for k in ("openai_api_key", "OPENAI_API_KEY"):
            val = data.get(k)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return None


def _project_root_from_payload(payload: Dict[str, Any]) -> Path:
    root = payload.get("project_root") or payload.get("root") or "."
    return Path(root).resolve()


# ------------------------------ capabilities -------------------------------

def complete_v1(task_like: Any, context: Optional[Dict[str, Any]] = None, **kwargs) -> Dict[str, Any]:
    """
    Payload:
      {
        "root": "...",                 # used to locate secret_management/
        "project_root": "...",
        "provider": "openai",
        "model": "gpt-4o-mini",
        "messages": [ {"role":"system","content":"..."}, ... ],
        "ask_spec": { ... }
      }
    Returns:
      { "result": { "raw": "<provider text>" } }
    """
    payload: Dict[str, Any] = (getattr(task_like, "payload", None) or task_like or {})
    payload.update(kwargs or {})

    provider = (payload.get("provider") or "").strip()
    model = payload.get("model") or ""
    messages = payload.get("messages") or payload.get("message") or []
    ask_spec = payload.get("ask_spec") or {}

    project_root = _project_root_from_payload(payload)
    api_key = _load_openai_key(project_root) if provider.lower() == "openai" else None

    raw = client_complete(provider, model, messages, ask_spec, api_key=api_key)
    return {"result": {"raw": raw}}


def complete_batches_v1(task_like: Any, context: Optional[Dict[str, Any]] = None, **kwargs) -> Dict[str, Any]:
    """
    Payload:
      {
        "root": "...",                 # used to locate secret_management/
        "project_root": "...",
        "provider": "openai",
        "model": "gpt-4o-mini",
        "batches": [ {"id":"...", "messages":[...]}, ... ],
        "ask_spec": { ... }
      }
    Returns:
      { "results": [ {"id":"...", "raw":"..."}, ... ] }
    """
    payload: Dict[str, Any] = (getattr(task_like, "payload", None) or task_like or {})
    payload.update(kwargs or {})

    provider = (payload.get("provider") or "").strip()
    model = payload.get("model") or ""
    batches: List[Dict[str, Any]] = list(payload.get("batches") or [])
    ask_spec = payload.get("ask_spec") or {}

    project_root = _project_root_from_payload(payload)
    api_key = _load_openai_key(project_root) if provider.lower() == "openai" else None

    results: List[Dict[str, Any]] = []
    for b in batches:
        msgs = b.get("messages") or []
        bid = b.get("id") or ""
        raw = client_complete(provider, model, msgs, ask_spec, api_key=api_key)
        results.append({"id": bid, "raw": raw})

    return {"results": results}


