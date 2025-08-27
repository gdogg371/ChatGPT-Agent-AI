# v2/backend/core/prompt_pipeline/llm/providers.py
from __future__ import annotations

r"""
LLM providers for the prompt_pipeline.

Exposes Spine capabilities:
  - llm.complete_batches.v1 → complete_batches_v1
  - llm.complete.v1         → complete_v1 (single-item adapter)

Features:
- OpenAI support with strict JSON mode when ask_spec.response_format == "json"
  * Uses OpenAI JSON mode via response_format={"type": "json_object"}
  * Adds a minimal system guardrail if needed to discourage prose/fences
- Tolerates both modern (OpenAI>=1.x) and legacy (openai<=0.x) Python clients
- Returns clean, list-shaped results: [{"id": , "raw": }, ...]
- NEW: Attaches Code Bundle (assistant_handoff + design_manifest or parts) as a
  system preface, and snapshots the exact request & bundle checksums to disk.
"""

from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path
import os
import json
import hashlib

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
    k = os.environ.get("OPENAI_API_KEY") or os.environ.get("OpenAI_API_Key")
    if k:
        return k

    # Try repo-local secrets files
    try:
        from pathlib import Path
        import yaml
        here = Path(__file__).resolve()  # .../v2/backend/core/prompt_pipeline/llm/providers.py
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


# ----------------------------- bundle preface + snapshots -----------------------
def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""

def _sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def _bundle_as_content_parts(bundle: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Convert a bundle descriptor to an ordered list of 'text' parts we will prefix
    to the chat messages as a synthetic system preface.

    Expected bundle shape (subset):
      {
        "run_dir": "<run_root>",
        "assistant_handoff": "<run_root>/bundle/assistant_handoff.v1.json",
        "manifest": "<run_root>/bundle/design_manifest.jsonl",            # optional if chunked
        "parts_dir": "<run_root>/bundle/design_manifest",                 # directory with 00.txt...
        "is_chunked": <bool>
      }
    """
    if not bundle:
        return []

    parts: List[Dict[str, Any]] = []

    # 1) assistant_handoff.v1.json (small control doc)
    handoff = Path(bundle.get("assistant_handoff") or "")
    if handoff and handoff.exists():
        parts.append({
            "type": "text",
            "text": f"__assistant_handoff.v1.json__\n{_read_text(handoff)}"
        })

    # 2) manifest: either monolith or chunked
    manifest = Path(bundle.get("manifest") or "")
    parts_dir = Path(bundle.get("parts_dir") or "")
    is_chunked = bool(bundle.get("is_chunked"))

    if not is_chunked and manifest and manifest.exists():
        parts.append({
            "type": "text",
            "text": f"__design_manifest.jsonl__\n{_read_text(manifest)}"
        })
    else:
        if parts_dir.exists() and parts_dir.is_dir():
            for p in sorted(parts_dir.iterdir()):
                if p.is_file() and p.suffix == ".txt":
                    parts.append({
                        "type": "text",
                        "text": f"__design_manifest__ part={p.name}\n{_read_text(p)}"
                    })

    return parts


# ----------------------------- shared core -------------------------------------
def _run_openai_batches(payload: Dict[str, Any]) -> List[Artifact]:
    provider = str(payload.get("provider") or "").strip().lower()
    model = str(payload.get("model") or "").strip()
    ask_spec_default = dict(payload.get("ask_spec") or {})
    batches = payload.get("batches") or []
    bundle = dict(payload.get("bundle") or {})  # <-- NEW: bundle descriptor

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

    # Build bundle preface once (same for all batches)
    bundle_parts = _bundle_as_content_parts(bundle)  # may be empty
    preface_text = ""
    meta_parts: List[Dict[str, Any]] = []
    for part in bundle_parts:
        if part.get("type") == "text":
            t = part.get("text", "")
            preface_text += t + "\n\n"
            header = t.split("\n", 1)[0].strip() if t else ""
            meta_parts.append({
                "header": header,
                "sha256": _sha256_text(t),
                "bytes": len(t.encode("utf-8")),
            })

    # Prepare request snapshot directory
    run_dir = Path(bundle.get("run_dir", ""))
    req_dir: Optional[Path] = None
    if run_dir.exists():
        req_dir = run_dir / "requests"
        req_dir.mkdir(parents=True, exist_ok=True)
        # Write attached_bundle_meta.json once (for the whole run)
        (req_dir / "attached_bundle_meta.json").write_text(
            json.dumps({
                "parts": meta_parts,
                "total_bytes": sum(mp["bytes"] for mp in meta_parts),
                "is_chunked": bool(bundle.get("is_chunked")),
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    results: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    for idx, item in enumerate(batches):
        try:
            msgs = list(item.get("messages") or [])
            if not msgs:
                raise ValueError("batch item missing 'messages'")
            item_ask = dict(item.get("ask_spec") or {}) or ask_spec_default
            want_json = str(item_ask.get("response_format", "")).lower() == "json"

            # Prepend bundle preface (if any)
            if preface_text.strip():
                msgs = [{"role": "system", "content": preface_text}] + msgs

            # Ensure JSON guardrail if requested
            msgs = _maybe_append_json_guardrail(msgs, want_json)

            # Snapshot exact request we will send (per batch)
            if req_dir is not None:
                (req_dir / f"{idx:04d}.json").write_text(
                    json.dumps({
                        "provider": provider,
                        "model": model,
                        "messages": msgs,
                        "ask_spec": item_ask,
                        "batch_id": item.get("id", idx),
                    }, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

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
        "batches": [ {"messages":[...], "ask_spec": {...}, "id": <any> }, ... ],
        "ask_spec": {...},    # default, optional
        "bundle": {...}       # optional (assistant_handoff + manifest/parts)
      }
    """
    p = task.payload or {}
    return _run_openai_batches(p)


def complete_v1(task: Task, context: Dict[str, Any]) -> List[Artifact]:
    """
    Single-item chat completion adapter for registries that target llm.complete.v1.

    Expects payload:
      {
        "provider": "...",
        "model": "...",
        "messages": [...],
        "ask_spec": {...},
        "id": <any>,
        "bundle": {...}   # optional
      }

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
        "bundle": dict(p.get("bundle") or {}),
    }
    return _run_openai_batches(batched)


__all__ = ["complete_batches_v1", "complete_v1"]
