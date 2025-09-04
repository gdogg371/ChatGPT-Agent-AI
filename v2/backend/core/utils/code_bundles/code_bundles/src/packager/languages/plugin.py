# File: backend/core/utils/code_bundles/code_bundles_v2/src/packager/languages/cpp/plugin.py
from __future__ import annotations

"""
C/C++ language plugin (header include graph)

Artifacts:
  analysis/cpp_syntax.json   -- UTF-8 decode "ok"/error per file
  graphs/cpp_includes.json   -- edges from file -> include target
"""

from typing import Any, Dict, List, Tuple
import re

PLUGIN_NAME = "cpp"
EXTENSIONS = (".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx")

_INCLUDE_RE = re.compile(
    r"""(?x)
    ^\s* \# \s* include \s*
    (?:
        <([^>]+)>    # system include
      | "([^"]+)"    # local include
    )
    """,
    re.MULTILINE,
)


class _CppPlugin:
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

            for m in _INCLUDE_RE.finditer(text):
                target = m.group(1) or m.group(2)
                edges.append({"from": path, "to": target})

            syntax.append(entry)

        return {
            "analysis/cpp_syntax.json": {"version": "1", "files": syntax},
            "graphs/cpp_includes.json": {"version": "1", "edges": edges},
        }

PLUGIN = _CppPlugin()
