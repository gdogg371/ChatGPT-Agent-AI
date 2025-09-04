# File: backend/core/utils/code_bundles/code_bundles_v2/src/packager/languages/csharp/plugin.py
from __future__ import annotations

"""
C# language plugin (regex-first)

Artifacts:
  analysis/cs_syntax.json   -- UTF-8 decode "ok"/error per file
  graphs/cs_usings.json     -- edges: file -> namespace (from `using ...;`)
  analysis/cs_symbols.json  -- namespaces/classes found (names only)
"""

from typing import Any, Dict, List, Tuple
import re

PLUGIN_NAME = "csharp"
EXTENSIONS = (".cs",)

_USING_RE = re.compile(r"^\s*using\s+([A-Za-z0-9_.]+)\s*;", re.MULTILINE)
_NAMESPACE_RE = re.compile(r"\bnamespace\s+([A-Za-z0-9_.]+)")
_CLASS_RE = re.compile(r"\b(class|record|struct|interface)\s+([A-Za-z_][A-Za-z0-9_]*)")


class _CsPlugin:
    name = PLUGIN_NAME
    extensions = EXTENSIONS

    def analyze(self, files: List[Tuple[str, bytes]]) -> Dict[str, Any]:
        syntax = []
        using_edges = []
        symbols = []
        for path, data in files:
            entry = {"path": path, "ok": False}
            try:
                text = data.decode("utf-8")
                entry["ok"] = True
            except Exception as e:
                entry["error"] = f"decode: {type(e).__name__}: {e}"
                syntax.append(entry)
                continue

            for ns in _USING_RE.findall(text):
                using_edges.append({"from": path, "to": ns})

            ns = _NAMESPACE_RE.search(text)
            ns_name = ns.group(1) if ns else None
            classes = [m.group(2) for m in _CLASS_RE.finditer(text)]
            symbols.append({"path": path, "namespace": ns_name, "classes": classes})

            syntax.append(entry)

        return {
            "analysis/cs_syntax.json": {"version": "1", "files": syntax},
            "graphs/cs_usings.json": {"version": "1", "edges": using_edges},
            "analysis/cs_symbols.json": {"version": "1", "files": symbols},
        }

PLUGIN = _CsPlugin()
