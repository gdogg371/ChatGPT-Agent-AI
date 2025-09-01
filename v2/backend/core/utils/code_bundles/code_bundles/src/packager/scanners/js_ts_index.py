# File: v2/backend/core/utils/code_bundles/code_bundles/js_ts_index.py
"""
JavaScript / TypeScript indexer (stdlib-only).

Extracts lightweight metadata from JS/TS sources without external parsers.

Per-file record (example)
-------------------------
{
  "kind": "js.index",
  "path": "web/src/app.tsx",
  "size": 18421,
  "ext": ".tsx",
  "lines": {"total": 512, "code": 391, "comments": 98, "blank": 23},
  "imports": {
    "static": [{"from":"react","count":2}, {"from":"./util","count":1}],
    "dynamic": [{"from":"./lazy","count":1}],
    "require": [{"from":"fs","count":1}]
  },
  "exports": {"default": true, "named": ["Button","useThing"]},
  "symbols": {"functions": 9, "classes": 3, "arrow_functions": 14},
  "packages": ["react","fs","lodash"],
  "hints": ["react","jsx","typescript","node"],
  "todos": 3
}

Summary record
--------------
{
  "kind": "js.index.summary",
  "files": 42,
  "lines": {"total": 8123, "code": 6011, "comments": 1682, "blank": 430},
  "imports_total": {"static": 120, "dynamic": 7, "require": 34},
  "packages_top": [{"package":"react","refs":37}, {"package":"lodash","refs":11}, ...],
  "framework_hints": {"react": 21, "next": 4, "vue": 2, "svelte": 0, "node": 18, "typescript": 26, "jsx": 19},
  "symbols_avg_per_file": {"functions": 6.1, "classes": 1.2, "arrow_functions": 9.4},
  "top_files_by_functions": [{"path":"web/src/app.tsx","functions":17}, ...]
}

Notes
-----
* Uses only the Python standard library (regex + simple state machines).
* Paths returned are repo-relative POSIX. If your pipeline distinguishes
  local vs GitHub path modes, map `path` before appending to the manifest.
* This is a pragmatic scanner — not a full JS/TS parser — but it captures useful signals.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

RepoItem = Tuple[Path, str]  # (local_path, repo_relative_posix)

_MAX_READ_BYTES = 2 * 1024 * 1024  # 2 MiB safety cap
_MAX_LIST = 200                    # cap lists we keep per file


# ──────────────────────────────────────────────────────────────────────────────
# IO helpers
# ──────────────────────────────────────────────────────────────────────────────

def _read_text_limited(p: Path, limit: int = _MAX_READ_BYTES) -> str:
    try:
        with p.open("rb") as f:
            data = f.read(limit + 1)
        if len(data) > limit:
            data = data[:limit]
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""


# ──────────────────────────────────────────────────────────────────────────────
# Comment stripping (string/template aware) for LOC stats
# ──────────────────────────────────────────────────────────────────────────────

def _strip_js_comments(text: str) -> str:
    """
    Remove JS/TS comments while preserving string and template literals.
    Keeps newlines so line counts remain stable.
    """
    out: List[str] = []
    i = 0
    n = len(text)
    in_single = False
    in_double = False
    in_backtick = False
    in_block = False
    while i < n:
        ch = text[i]

        # End of block comment?
        if in_block:
            if ch == "*" and i + 1 < n and text[i + 1] == "/":
                in_block = False
                i += 2
            else:
                # preserve newlines to keep line structure
                out.append("\n" if ch == "\n" else "")
                i += 1
            continue

        # Inside template literal?
        if in_backtick:
            out.append(ch)
            if ch == "\\":
                # escape next char inside template
                if i + 1 < n:
                    out.append(text[i + 1])
                    i += 2
                else:
                    i += 1
                continue
            if ch == "`":
                in_backtick = False
            i += 1
            continue

        # Inside single/double-quoted string?
        if in_single:
            out.append(ch)
            if ch == "\\":
                if i + 1 < n:
                    out.append(text[i + 1])
                    i += 2
                else:
                    i += 1
                continue
            if ch == "'":
                in_single = False
            i += 1
            continue

        if in_double:
            out.append(ch)
            if ch == "\\":
                if i + 1 < n:
                    out.append(text[i + 1])
                    i += 2
                else:
                    i += 1
                continue
            if ch == '"':
                in_double = False
            i += 1
            continue

        # Not inside string/template/comment
        if ch == "/":
            # Line comment?
            if i + 1 < n and text[i + 1] == "/":
                # consume until EOL
                while i < n and text[i] not in ("\n", "\r"):
                    i += 1
                out.append("\n")
                continue
            # Block comment?
            if i + 1 < n and text[i + 1] == "*":
                in_block = True
                i += 2
                continue

        # Enter strings/templates
        if ch == "'":
            in_single = True
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            in_double = True
            out.append(ch)
            i += 1
            continue
        if ch == "`":
            in_backtick = True
            out.append(ch)
            i += 1
            continue

        out.append(ch)
        i += 1

    return "".join(out)


def _line_stats(original: str, code_only: str) -> Dict[str, int]:
    orig_lines = original.splitlines()
    code_lines = code_only.splitlines()
    total = len(orig_lines)
    blank = sum(1 for ln in orig_lines if ln.strip() == "")
    code = sum(1 for ln in code_lines if ln.strip() != "")
    comments = max(0, total - code - blank)
    return {"total": total, "code": code, "comments": comments, "blank": blank}


# ──────────────────────────────────────────────────────────────────────────────
# Regexes for imports/exports/symbols
# ──────────────────────────────────────────────────────────────────────────────

# Static imports:
#   import X from 'mod';
#   import {A,B as C} from "mod";
#   import 'side-effect';
_RE_IMPORT_FROM = re.compile(r"""(?m)^\s*import\s+(?:.+?\s+from\s+)?['"]([^'"]+)['"]""")

# Dynamic imports:
#   const m = await import('mod')
_RE_IMPORT_DYNAMIC = re.compile(r"""(?<!\.)\bimport\s*\(\s*['"]([^'"]+)['"]\s*\)""")

# CommonJS require:
_RE_REQUIRE = re.compile(r"""(?<!\.)\brequire\s*\(\s*['"]([^'"]+)['"]\s*\)""")

# Exports:
_RE_EXPORT_DEFAULT = re.compile(r"""(?m)^\s*export\s+default\b""")
_RE_EXPORT_NAMED_FUNC = re.compile(r"""(?m)^\s*export\s+(?:async\s+)?function\s+([A-Za-z_]\w*)\s*\(""")
_RE_EXPORT_NAMED_CLASS = re.compile(r"""(?m)^\s*export\s+class\s+([A-Za-z_]\w*)\s*[{]""")
_RE_EXPORT_NAMED_CONST = re.compile(r"""(?m)^\s*export\s+(?:const|let|var)\s+([A-Za-z_]\w*)\b""")
_RE_EXPORT_LIST = re.compile(r"""(?m)^\s*export\s*{\s*([^}]+)\s*}""")  # export { A, B as C }

# Symbols (very approximate)
_RE_FUNCTION_DEF = re.compile(r"""(?m)^\s*(?:export\s+)?(?:async\s+)?function\s+[A-Za-z_]\w*\s*\(""")
_RE_CLASS_DEF = re.compile(r"""(?m)^\s*(?:export\s+)?class\s+[A-Za-z_]\w*\s*[{]""")
_RE_ARROW = re.compile(r"""=>""")

# TODO/FIXME/HACK
_RE_TODO = re.compile(r"""(?i)\b(?:TODO|FIXME|HACK|TBD)\b""")


# ──────────────────────────────────────────────────────────────────────────────
# Extraction helpers
# ──────────────────────────────────────────────────────────────────────────────

_JS_EXTS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}

def _is_package(spec: str) -> bool:
    s = spec.strip()
    return not (s.startswith("./") or s.startswith("../") or s.startswith("/") or s.startswith("file:"))

def _collect_named_from_list(spec: str) -> List[str]:
    # From "export { A, B as C }" extract ["A","C"]
    out: List[str] = []
    for part in spec.split(","):
        name = part.strip()
        if not name:
            continue
        # handle "X as Y"
        if " as " in name:
            _, as_name = name.split(" as ", 1)
            as_name = as_name.strip()
            if as_name:
                out.append(as_name)
        else:
            out.append(name)
    # de-dup while preserving order
    seen = set()
    uniq: List[str] = []
    for n in out:
        if n not in seen:
            seen.add(n)
            uniq.append(n)
    return uniq


def _framework_hints(text: str, packages: Set[str], ext: str) -> Set[str]:
    hints: Set[str] = set()
    low = text.lower()

    if "react" in packages:
        hints.add("react")
    if ext in (".jsx", ".tsx"):
        hints.add("jsx")
    if "next" in packages:
        hints.add("next")
    if "vue" in packages or "<template" in low or "vue.createapp" in low:
        hints.add("vue")
    if "svelte" in packages:
        hints.add("svelte")
    if "require(" in low or any(p in packages for p in ("fs", "path", "os", "http", "https", "stream")):
        hints.add("node")
    if ext in (".ts", ".tsx") or re.search(r"(?m)^\s*(?:type|interface)\s+[A-Za-z_]\w*\b", text):
        hints.add("typescript")
    return hints


# ──────────────────────────────────────────────────────────────────────────────
# Per-file analysis
# ──────────────────────────────────────────────────────────────────────────────

def analyze_file(*, local_path: Path, repo_rel_posix: str) -> Dict:
    """
    Analyze a single JS/TS file and return a JSON-ready record.
    """
    text = _read_text_limited(local_path)
    try:
        size = local_path.stat().st_size
    except Exception:
        size = len(text.encode("utf-8", errors="ignore"))

    ext = local_path.suffix.lower()

    # LOC
    code_only = _strip_js_comments(text)
    lines = _line_stats(text, code_only)

    # Imports
    imports_static = Counter(_RE_IMPORT_FROM.findall(text))
    imports_dynamic = Counter(_RE_IMPORT_DYNAMIC.findall(text))
    requires = Counter(_RE_REQUIRE.findall(text))

    # Exports
    has_default = bool(_RE_EXPORT_DEFAULT.search(text))
    named: List[str] = []
    named.extend(_RE_EXPORT_NAMED_FUNC.findall(text))
    named.extend(_RE_EXPORT_NAMED_CLASS.findall(text))
    named.extend(_RE_EXPORT_NAMED_CONST.findall(text))
    for block in _RE_EXPORT_LIST.findall(text):
        named.extend(_collect_named_from_list(block))
    # De-dup while preserving order
    seen = set()
    named_unique: List[str] = []
    for n in named:
        if n not in seen:
            seen.add(n)
            named_unique.append(n)

    # Symbols
    functions = len(_RE_FUNCTION_DEF.findall(text))
    classes = len(_RE_CLASS_DEF.findall(text))
    arrows = len(_RE_ARROW.findall(text))

    # Packages referenced (from static/dynamic/require)
    pkgs: Set[str] = set()
    for spec, cnt in list(imports_static.items()) + list(imports_dynamic.items()) + list(requires.items()):
        if _is_package(spec):
            pkgs.add(spec)

    # TODOs
    todos = len(_RE_TODO.findall(text))

    hints = sorted(list(_framework_hints(text, pkgs, ext)))

    rec = {
        "kind": "js.index",
        "path": repo_rel_posix,
        "size": int(size),
        "ext": ext,
        "lines": lines,
        "imports": {
            "static": [{"from": k, "count": int(v)} for k, v in imports_static.items()][: _MAX_LIST],
            "dynamic": [{"from": k, "count": int(v)} for k, v in imports_dynamic.items()][: _MAX_LIST],
            "require": [{"from": k, "count": int(v)} for k, v in requires.items()][: _MAX_LIST],
        },
        "exports": {"default": has_default, "named": named_unique[: _MAX_LIST]},
        "symbols": {"functions": int(functions), "classes": int(classes), "arrow_functions": int(arrows)},
        "packages": sorted(list(pkgs))[: _MAX_LIST],
        "hints": hints,
        "todos": int(todos),
    }
    return rec


# ──────────────────────────────────────────────────────────────────────────────
# Repository scan with summary
# ──────────────────────────────────────────────────────────────────────────────

def scan(repo_root: Path, discovered: Iterable[RepoItem]) -> List[Dict]:
    """
    Scan discovered files, indexing JS/TS files and returning:
      - One 'js.index' record per file
      - One 'js.index.summary' record at the end
    """
    items = [(lp, rel) for (lp, rel) in discovered if Path(rel).suffix.lower() in _JS_EXTS]
    results: List[Dict] = []

    # Aggregates
    total_lines = total_code = total_comments = total_blank = 0
    imports_total_static = imports_total_dynamic = imports_total_require = 0
    packages_counter: Counter[str] = Counter()
    hints_counter: Counter[str] = Counter()
    sum_functions = sum_classes = sum_arrows = 0
    per_file_funcs: List[Tuple[str, int]] = []

    for local, rel in items:
        rec = analyze_file(local_path=local, repo_rel_posix=rel)
        results.append(rec)

        ln = rec.get("lines", {})
        total_lines += int(ln.get("total", 0))
        total_code += int(ln.get("code", 0))
        total_comments += int(ln.get("comments", 0))
        total_blank += int(ln.get("blank", 0))

        imports = rec.get("imports", {})
        imports_total_static += sum(int(e.get("count", 0)) for e in (imports.get("static") or []))
        imports_total_dynamic += sum(int(e.get("count", 0)) for e in (imports.get("dynamic") or []))
        imports_total_require += sum(int(e.get("count", 0)) for e in (imports.get("require") or []))

        for p in rec.get("packages") or []:
            packages_counter[p] += 1

        for h in rec.get("hints") or []:
            hints_counter[h] += 1

        sym = rec.get("symbols", {})
        f = int(sym.get("functions", 0))
        c = int(sym.get("classes", 0))
        a = int(sym.get("arrow_functions", 0))
        sum_functions += f
        sum_classes += c
        sum_arrows += a
        per_file_funcs.append((rel, f))

    files_n = len(items)
    def _avg(x: int) -> Optional[float]:
        return round(x / float(files_n), 3) if files_n > 0 else None

    top_files_by_funcs = [{"path": p, "functions": f} for (p, f) in sorted(per_file_funcs, key=lambda t: (-t[1], t[0]))[:20]]
    packages_top = [{"package": k, "refs": v} for (k, v) in packages_counter.most_common(20)]

    summary = {
        "kind": "js.index.summary",
        "files": files_n,
        "lines": {"total": total_lines, "code": total_code, "comments": total_comments, "blank": total_blank},
        "imports_total": {
            "static": imports_total_static,
            "dynamic": imports_total_dynamic,
            "require": imports_total_require,
        },
        "packages_top": packages_top,
        "framework_hints": dict(sorted(hints_counter.items(), key=lambda kv: (-kv[1], kv[0]))),
        "symbols_avg_per_file": {
            "functions": _avg(sum_functions) if files_n else None,
            "classes": _avg(sum_classes) if files_n else None,
            "arrow_functions": _avg(sum_arrows) if files_n else None,
        },
        "top_files_by_functions": top_files_by_funcs,
    }
    results.append(summary)
    return results


__all__ = ["scan", "analyze_file"]
