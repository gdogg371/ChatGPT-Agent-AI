# File: backend/core/utils/code_bundles/code_bundles_v2/src/packager/languages/typescript/plugin.py
from __future__ import annotations

"""
TypeScript language plugin (v2)

Lightweight, dependency-free analysis:
- Syntax "ok" check = UTF-8 decode only (no full parser).
- Import graph extraction from common forms:
    import ... from "spec";
    import "spec";
    export * from "spec";
    export { x } from "spec";

Artifacts returned:
  analysis/ts_syntax.json
  graphs/ts_imports.json
"""

from typing import Dict, List, Tuple, Any
import hashlib
import re


_TS_IMPORT_RE = re.compile(
    r"""(?x)
    ^\s*
    (?:
        import\s+(?:.+?\s+from\s+)?   # import ... from
      | import\s*                    # or bare import "mod"
      | export\s+\*\s+from\s+        # export * from
      | export\s+\{[^}]*\}\s+from\s+ # export { a, b } from
    )
    ['"]([^'"]+)['"]                  # capture module specifier
    """,
    re.MULTILINE,
)


class _TsPlugin:
    name = "typescript"
    extensions = (".ts", ".tsx")

    def analyze(self, files: List[Tuple[str, bytes]]) -> Dict[str, Any]:
        syntax_list = []
        imports_graph = []

        for path, data in files:
            entry = {"path": path, "ok": False}
            try:
                text = data.decode("utf-8")
                entry["ok"] = True
            except Exception as e:
                entry["error"] = f"decode: {type(e).__name__}: {e}"
                syntax_list.append(entry)
                continue

            syntax_list.append(entry)

            # Simple import extraction
            edges = []
            for m in _TS_IMPORT_RE.finditer(text):
                spec = m.group(1)
                edges.append({"from": path, "to": spec})
            if edges:
                imports_graph.extend(edges)

        return {
            "analysis/ts_syntax.json": {"version": "1", "files": syntax_list},
            "graphs/ts_imports.json": {"version": "1", "edges": imports_graph},
        }


PLUGIN = _TsPlugin()
