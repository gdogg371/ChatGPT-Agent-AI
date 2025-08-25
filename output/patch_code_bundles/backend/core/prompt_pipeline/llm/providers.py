from __future__ import annotations

import os
from typing import Any, Dict, List

from v2.backend.core.spine.contracts import Artifact, Task
from v2.backend.core.prompt_pipeline.llm.client import LlmClient, LlmRequest
from v2.backend.core.prompt_pipeline.executor.errors import LlmClientError


def _result(uri: str, meta: Dict[str, Any]) -> List[Artifact]:
    return [Artifact(kind="Result", uri=uri, sha256="", meta=meta)]

def _problem(uri: str, code: str, message: str, *, retryable: bool = False,
             details: Dict[str, Any] | None = None) -> List[Artifact]:
    return [Artifact(kind="Problem", uri=uri, sha256="", meta={
        "problem": {"code": code, "message": message, "retryable": retryable, "details": dict(details or {})}
    })]

def _bool(v: Any) -> bool:
    if isinstance(v, bool): return v
    if isinstance(v, str): return v.strip().lower() in {"1", "true", "yes", "on"}
    return bool(v)


# -------- single-prompt provider (kept for direct use) --------

def complete_v1(task: Task, context: Dict[str, Any]) -> List[Artifact]:
    p = task.payload or {}
    if not isinstance(p, dict):
        return _problem("spine://problem/llm.complete.v1", "InvalidPayload", "payload must be a dict")

    provider = (p.get("provider") or "openai").strip()
    model = (p.get("model") or "").strip()
    api_key = (p.get("api_key") or os.getenv("OPENAI_API_KEY", "")).strip()
    system = (p.get("system") or "").strip()
    user = (p.get("user") or "").strip()
    temperature = float(p.get("temperature") or 0.0)
    max_out = int(p.get("max_output_tokens") or 1024)
    response_format = p.get("response_format") or None

    if not model:
        return _problem("spine://problem/llm.complete.v1", "InvalidPayload", "model is required")
    if not api_key:
        return _problem("spine://problem/llm.complete.v1", "AuthError", "missing api_key")
    if not system or not user:
        return _problem("spine://problem/llm.complete.v1", "InvalidPayload", "system and user are required")

    client = LlmClient(provider=provider)
    req = LlmRequest(
        system=system,
        user=user,
        model=model,
        api_key=api_key,
        temperature=temperature,
        max_output_tokens=max_out,
        response_format=response_format,
    )

    try:
        raw = client.complete(req)
    except LlmClientError as e:
        return _problem("spine://problem/llm.complete.v1", "LlmClientError", str(e), retryable=getattr(e, "retryable", False))
    except Exception as e:
        return _problem("spine://problem/llm.complete.v1", "UnhandledError", repr(e))

    return _result("spine://result/llm.complete.v1", {"result": raw})


# -------- batch provider (pipeline use) --------

def complete_batches_v1(task: Task, context: Dict[str, Any]) -> List[Artifact]:
    """
    Capability: llm.complete_batches.v1
    Accepts either:
      - payload.baton: {"batches":[...]}
      - payload.baton: [ ... ]   # old shape
    Each batch must provide messages.system/user, or top-level system/user.
    """
    p = task.payload or {}
    if not isinstance(p, dict):
        return _problem("spine://problem/llm.complete_batches.v1", "InvalidPayload", "payload must be a dict")

    baton_in = p.get("baton")
    if isinstance(baton_in, dict):
        baton: Dict[str, Any] = dict(baton_in)
        batches = baton.get("batches") or []
    elif isinstance(baton_in, list):
        # older shape: treat directly as batch list
        batches = baton_in
        baton = {"batches": batches}
    else:
        return _problem("spine://problem/llm.complete_batches.v1", "InvalidPayload", "baton must be a dict or list of batches")

    if not isinstance(batches, list):
        return _problem("spine://problem/llm.complete_batches.v1", "InvalidPayload", "baton.batches must be a list")

    provider = (p.get("provider") or "openai").strip()
    model = (p.get("model") or "").strip()
    api_key = (p.get("api_key") or os.getenv("OPENAI_API_KEY", "")).strip()
    temperature = float(p.get("temperature") or 0.0)
    max_out = int(p.get("max_output_tokens") or 1024)
    response_format = p.get("response_format") or None

    if not model:
        return _problem("spine://problem/llm.complete_batches.v1", "InvalidPayload", "model is required")
    if not api_key:
        return _problem("spine://problem/llm.complete_batches.v1", "AuthError", "missing api_key")

    client = LlmClient(provider=provider)
    raws: List[str] = []

    for i, bundle in enumerate(batches, 1):
        if not isinstance(bundle, dict):
            return _problem("spine://problem/llm.complete_batches.v1", "InvalidPayload", f"batch {i} must be a dict")

        msgs = bundle.get("messages") or {}
        # tolerate old shape: system/user at top-level
        if not msgs and ("system" in bundle and "user" in bundle):
            msgs = {"system": bundle.get("system"), "user": bundle.get("user")}

        system = (msgs.get("system") or "").strip()
        user = (msgs.get("user") or "").strip()
        if not system or not user:
            return _problem("spine://problem/llm.complete_batches.v1", "InvalidPayload", f"batch {i} missing system/user")

        req = LlmRequest(
            system=system,
            user=user,
            model=model,
            api_key=api_key,
            temperature=temperature,
            max_output_tokens=max_out,
            response_format=response_format,
        )
        try:
            raw = client.complete(req)
            raws.append(raw)
        except LlmClientError as e:
            return _problem("spine://problem/llm.complete_batches.v1", "LlmClientError", str(e), retryable=getattr(e, "retryable", False))
        except Exception as e:
            return _problem("spine://problem/llm.complete_batches.v1", "UnhandledError", repr(e))

    baton["raw"] = raws
    return _result("spine://result/llm.complete_batches.v1", {"result": baton})


# ---- optional back-compat alias ----
def llm_complete_v1(task: Task, context: Dict[str, Any]) -> List[Artifact]:
    return complete_v1(task, context)




