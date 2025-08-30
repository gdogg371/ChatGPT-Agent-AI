# File: backend/core/utils/code_bundles/code_bundles_v2/src/packager/languages/sql/plugin.py
from __future__ import annotations

"""
SQL language plugin (generic, dialect-agnostic first pass)

Artifacts:
  analysis/sql_syntax.json   -- UTF-8 decode "ok"/error per file
  graphs/sql_refs.json       -- naive refs per statement: tables seen in FROM/JOIN/INSERT/UPDATE
"""

from typing import Any, Dict, List, Tuple
import re

PLUGIN_NAME = "sql"
EXTENSIONS = (".sql",)

# Very light Statement splitter (naive): split on ';' not inside single/double quotes
_SPLIT_RE = re.compile(r"""
    ;                           # semicolon
    (?=(?:[^'"]|'[^']*'|"[^"]*")*$)  # not within quotes
""", re.VERBOSE)

# Naive table refs (dialect-agnostic): FROM/JOIN/INSERT INTO/UPDATE <ident or schema.ident>
_REF_PATTERNS = [
    re.compile(r"\bfrom\s+([A-Za-z0-9_.\"`\[\]]+)", re.IGNORECASE),
    re.compile(r"\bjoin\s+([A-Za-z0-9_.\"`\[\]]+)", re.IGNORECASE),
    re.compile(r"\binsert\s+into\s+([A-Za-z0-9_.\"`\[\]]+)", re.IGNORECASE),
    re.compile(r"\bupdate\s+([A-Za-z0-9_.\"`\[\]]+)", re.IGNORECASE),
    re.compile(r"\bdelete\s+from\s+([A-Za-z0-9_.\"`\[\]]+)", re.IGNORECASE),
]

# Heuristic dialect hint (best-effort; not authoritative)
def _dialect_hint(sql: str) -> str:
    s = sql.lower()
    if " language plpgsql" in s or "::" in s or "unnest(" in s: return "postgres"
    if "delimiter //" in s or "engine=" in s: return "mysql"
    if "pragma " in s or "sqlite_" in s: return "sqlite"
    if "create or replace procedure" in s and "nvarchar" in s: return "tsql"
    if "`" in s and "bigquery" in s: return "bigquery"
    return "generic"

def _split_statements(text: str) -> List[str]:
    parts = _SPLIT_RE.split(text)
    # keep non-empty trimmed statements
    return [p.strip() for p in parts if p.strip()]

def _extract_refs(sql: str) -> List[str]:
    refs: List[str] = []
    for pat in _REF_PATTERNS:
        for m in pat.finditer(sql):
            refs.append(m.group(1))
    return refs

class _SqlPlugin:
    name = PLUGIN_NAME
    extensions = EXTENSIONS

    def analyze(self, files: List[Tuple[str, bytes]]) -> Dict[str, Any]:
        syntax = []
        refs_edges = []
        for path, data in files:
            entry = {"path": path, "ok": False}
            try:
                text = data.decode("utf-8")
                entry["ok"] = True
            except Exception as e:
                entry["error"] = f"decode: {type(e).__name__}: {e}"
                syntax.append(entry)
                continue

            # Split + scan
            stmts = _split_statements(text)
            for i, st in enumerate(stmts, start=1):
                hint = _dialect_hint(st)
                for t in _extract_refs(st):
                    refs_edges.append({"file": path, "stmt": i, "dialect": hint, "to": t})

            syntax.append(entry)

        return {
            "analysis/sql_syntax.json": {"version": "1", "files": syntax},
            "graphs/sql_refs.json": {"version": "1", "edges": refs_edges},
        }

PLUGIN = _SqlPlugin()
