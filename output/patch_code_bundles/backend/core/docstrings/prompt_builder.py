from __future__ import annotations
from typing import Dict, List


def build_system_prompt() -> str:
    return (
        "You improve Python docstrings ONLY. Keep code semantics identical.\n"
        "Follow PEP 257 (summary on the same line as opening quotes, then a blank line for multi-line).\n"
        "Never invent parameters or return values not present in the signature or obvious from context.\n"
        "Style rules:\n"
        " - Do NOT start with 'This function...' / 'This method...' / 'This class...' / 'This module...'\n"
        " - Use an imperative, concise summary (≤ 72 chars) ending with a period.\n"
        " - Functions/classes: include sections only when applicable: Args:, Returns:, Raises:. Types if obvious.\n"
        " - Module docstrings: If capabilities/features are listed (e.g., bullets with AG-xx), rewrite them as\n"
        "   a 'Capabilities:' section using dash bullets WITHOUT any AG/AG-xx labels.\n"
        "Formatting contract:\n"
        " - Return the docstring CONTENT ONLY (no enclosing triple quotes, no code fences).\n"
        " - Output MUST be valid JSON of the shape: {\"items\":[{\"id\":\"...\",\"mode\":\"rewrite|create\",\"docstring\":\"...\"}, ...]}\n"
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


def build_user_prompt(batch: List[Dict[str, str]]) -> str:
    ids_list = ", ".join(f'"{it["id"]}"' for it in batch)
    header = (
        "Rewrite or create docstrings for each item below.\n"
        "IMPORTANT:\n"
        f" - Use the EXACT ids for this batch: [{ids_list}].\n"
        " - Do NOT renumber, add, or omit items. Echo each id exactly as provided.\n"
        "For module docstrings, include a 'Capabilities:' section as bullet points when the context hints at multiple responsibilities.\n"
        "Do NOT include any 'AG-' labels or numeric tags; keep the bullet text only.\n"
        "Return ONLY a single JSON object with an 'items' array in this exact schema:\n"
        "{\n"
        '  "items": [\n'
        '    {"id": "<string>", "mode": "rewrite|create", "docstring": "<string>"}\n'
        "  ]\n"
        "}\n"
    )
    blocks = "\n".join(_format_item_block(it) for it in batch)
    return header + "\n" + blocks


