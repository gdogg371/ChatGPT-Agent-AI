# File: v2/backend/core/prompt_pipeline/executor/prompts.py
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Tuple


# -----------------------------------------------------------------------------
# Public API (kept intentionally small and stable)
#   - build_system_prompt(ask_spec, cfg) -> str
#   - build_user_prompt(items, ask_spec, cfg) -> str
#   - build_packed(items, ask_spec, cfg) -> Tuple[str, str]
#   - build_batches(...)  -> alias to build_packed (back-compat)
# -----------------------------------------------------------------------------

def _get_style(ask_spec: Dict[str, Any] | None) -> str:
    style = (ask_spec or {}).get("style") or "google"
    style = str(style).strip().lower()
    if style not in {"google", "numpy", "rst"}:
        style = "google"
    return style


def _get_language(ask_spec: Dict[str, Any] | None) -> str:
    lang = (ask_spec or {}).get("language") or "en"
    return str(lang).strip().lower()


def _system_header(style: str) -> str:
    # Keep this concise and deterministic; the model-side format is enforced by steps.UnpackResultsStep
    common = [
        "You are a precise code documentation assistant.",
        "Write accurate, concise docstrings for Python symbols.",
        "Return ONLY JSON in the schema: {\"items\": [{\"id\":\"\",\"mode\":\"create|rewrite\",\"docstring\":\"\"}]}",
        "Do not include prose or Markdown fences.",
    ]
    if style == "google":
        extra = "Use Google-style docstrings where appropriate."
    elif style == "numpy":
        extra = "Use NumPy-style docstrings where appropriate."
    else:
        extra = "Use reStructuredText (reST) docstrings where appropriate."
    return " ".join(common + [extra])


def _item_slim(it: Dict[str, Any]) -> Dict[str, Any]:
    """Compact each item to the minimal, useful context for the LLM."""
    return {
        "id": str(it.get("id", "")),
        "mode": str(it.get("mode") or ("rewrite" if it.get("has_docstring") else "create")),
        "signature": it.get("signature"),
        "has_docstring": bool(it.get("has_docstring", False)),
        "existing_docstring": (it.get("existing_docstring") or "")[:1200],
        "description": (it.get("description") or "")[:1000],
        "context_code": (it.get("context_code") or "")[:2000],
    }


def build_system_prompt(ask_spec: Dict[str, Any] | None, cfg: Any | None = None) -> str:
    """
    Construct the system message. Minimal, deterministic, and keyed by style.
    `cfg` is accepted for parity with callers but not required here.
    """
    style = _get_style(ask_spec)
    return _system_header(style)


def build_user_prompt(items: Iterable[Dict[str, Any]], ask_spec: Dict[str, Any] | None, cfg: Any | None = None) -> str:
    """
    Build the user message by embedding a slimmed JSON view of items.
    """
    language = _get_language(ask_spec)
    # We keep instructions short; parsing strictness is handled downstream.
    preface = (
        f"Language: {language}. Target symbols below. "
        "For each item, produce a high-quality docstring that reflects the signature and context."
    )
    slim = [_item_slim(it) for it in items]
    return preface + "\n" + json.dumps({"items": slim}, ensure_ascii=False)


def build_packed(items: Iterable[Dict[str, Any]], *, ask_spec: Dict[str, Any] | None = None, cfg: Any | None = None) -> Tuple[str, str]:
    """
    Convenience wrapper that returns (system, user).
    """
    system = build_system_prompt(ask_spec, cfg)
    user = build_user_prompt(items, ask_spec, cfg)
    return system, user


# ----------------------------- Back-compat alias -------------------------------

def build_batches(items: Iterable[Dict[str, Any]], *, ask_spec: Dict[str, Any] | None = None, cfg: Any | None = None) -> Tuple[str, str]:
    """
    Legacy callers imported `build_batches` from this module.
    Keep behavior identical to `build_packed` for compatibility.
    """
    return build_packed(items, ask_spec=ask_spec, cfg=cfg)


# ------------------------------ Self-test -------------------------------------

if __name__ == "__main__":
    demo_items = [
        {
            "id": "A",
            "signature": "def foo(x: int) -> int",
            "context_code": "def foo(x: int) -> int:\n    return x + 1\n",
            "has_docstring": False,
            "description": "Compute foo of x.",
        },
        {
            "id": "B",
            "signature": "class Bar:\n    def baz(self): ...",
            "existing_docstring": "Old doc",
            "context_code": "class Bar:\n    def baz(self):\n        pass\n",
            "has_docstring": True,
            "description": "Container class.",
        },
    ]
    sys = build_system_prompt({"style": "google"})
    usr = build_user_prompt(demo_items, {"language": "en"})
    sys2, usr2 = build_packed(demo_items, ask_spec={"style": "numpy", "language": "en"})
    assert isinstance(sys, str) and isinstance(usr, str)
    assert isinstance(sys2, str) and isinstance(usr2, str)
    assert "JSON" in sys
    assert '"items"' in usr
    print("[prompts.selftest] OK")
