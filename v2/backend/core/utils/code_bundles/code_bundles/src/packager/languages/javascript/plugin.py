# File: backend/core/utils/code_bundles/code_bundles_v2/src/packager/languages/javascript/plugin.py
from __future__ import annotations

"""
JavaScript language plugin

Artifacts:
  analysis/js_syntax.json  -- UTF-8 decode "ok"/error per file
  graphs/js_imports.json   -- edges from file -> module specifier (ESM + CJS)
"""

from typing import Any, Dict, List, Tuple
import re

PLUGIN_NAME = "javascript"
EXTENSIONS = (".js", ".mjs", ".cjs", ".jsx")

_ESM_RE = re.compile(
    r"""(?x)
    ^\s*
    (?:
        import\s+(?:.+?\s+from\s+)?   # import ... from
      | import\s*                    # or bare import "mod"
      | export\s+\*\s+from\s+        # export * from
      | export\s+\{[^}]*\}\s+from\s+ # export { a, b } from
    )
    ['"]([^'"]+)['"]                  # module specifier
    """,
    re.MULTILINE,
)

_CJS_RE = re.compile(
    r"""(?x)
    require\(\s*['"]([^'"]+)['"]\s*\)
    """
)


class _JsPlugin:
    name = PLUGIN_NAME
    extensions = EXTENSIONS

    def analyze(self, files: List[Tuple[str, bytes]]) -> Dict[str, Any]:
        syntax = []
        edges = []
        for path, data in files:
            entry = {"path": path, "ok": False}
            try:
                text = data.decode("utf-8")
                entry["ok"] = True
            except Exception as e:
                entry["error"] = f"decode: {type(e).__name__}: {e}"
                syntax.append(entry)
                continue

            for m in _ESM_RE.finditer(text):
                edges.append({"from": path, "to": m.group(1), "kind": "esm"})
            for m in _CJS_RE.finditer(text):
                edges.append({"from": path, "to": m.group(1), "kind": "cjs"})

            syntax.append(entry)

        return {
            "analysis/js_syntax.json": {"version": "1", "files": syntax},
            "graphs/js_imports.json": {"version": "1", "edges": edges},
        }

PLUGIN = _JsPlugin()
