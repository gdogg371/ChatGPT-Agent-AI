from __future__ import annotations
from typing import Dict, List


def build_system_prompt() -> str:
    return (
        "You are a precise Python documentation assistant.\n"
        "Write PEP 257–compliant docstrings that are specific and useful.\n"
        "Rules:\n"
        "  - NEVER return placeholders like 'Add a concise summary', 'TBD', 'TODO', or empty strings.\n"
        "  - First line: short imperative summary.\n"
        "  - Then a blank line, then details.\n"
        "  - For functions: include Args (names/types if visible), Returns, Raises (if any).\n"
        "  - For classes: purpose, key attributes/behaviors, noteworthy methods.\n"
        "  - For modules: describe what the module provides, key components, and how it fits in the package.\n"
        "  - Be faithful to the provided code/context; if something is unknown, omit it.\n"
        "  - Output ONLY JSON as requested; no extra prose.\n"
    )



def _format_item_block(it: Dict[str, str]) -> str:
    """
    Produce a compact, deterministic block for one item.
    """
    id_ = it["id"]
    mode = it["mode"]
    sig = it["signature"]
    desc = (it.get("description") or "").strip()
    ctx = (it.get("context_code") or "").strip()
    has = "yes" if it.get("has_docstring") else "no"

    # Context is limited; the model should infer features but not fabricate.
    return (
        f"---\n"
        f'id: "{id_}"\n'          # ← quote id to discourage renumbering
        f"mode: {mode}\n"
        f"signature: {sig}\n"
        f"has_existing: {has}\n"
        f"description: {desc}\n"
        f"context:\n{ctx}\n"
    )


def build_user_prompt(batch: list[dict]) -> str:
    # Each item has: id, mode ('create'|'rewrite'), signature, context_code, description (maybe)
    # Ask for strict JSON: list[{"id": str, "docstring": str}]
    lines = []
    lines.append(
        "For each item below, return a JSON list like:\n"
        '[{"id": "<id>", "docstring": "<PEP257 docstring>"}]\n'
        "Constraints:\n"
        "  - 'docstring' MUST be non-empty and meaningful (≈20+ words unless the symbol is trivial).\n"
        "  - Do NOT include triple quotes; return just the string content.\n"
        "  - If the symbol is a module and code is sparse, still write a helpful module docstring:\n"
        "    summarize the module's purpose using file path, names seen, and imports (do not invent APIs).\n"
        "Items:\n"
    )
    for it in batch:
        lines.append("--")
        lines.append(f"id: {it.get('id')}")
        lines.append(f"mode: {it.get('mode')}")
        lines.append(f"signature: {it.get('signature')}")
        desc = (it.get("description") or "").strip()
        if desc:
            lines.append(f"description: {desc}")
        ctx = it.get("context_code") or ""
        if ctx:
            lines.append("context_code:\n```python")
            lines.append(ctx)
            lines.append("```")
    lines.append("\nReturn ONLY the JSON list as the entire response.")
    return "\n".join(lines)




