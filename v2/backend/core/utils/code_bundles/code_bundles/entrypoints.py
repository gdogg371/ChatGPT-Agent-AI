# File: v2/backend/core/utils/code_bundles/code_bundles/entrypoints.py
"""
Entrypoints indexer (stdlib-only).

Emits JSONL-ready records that describe likely ways to start/run the project:

  • entrypoint.config
      - Parsed entrypoints from:
          - pyproject.toml  ([project.scripts], [project.gui-scripts], [tool.poetry.scripts])
          - setup.cfg       ([options.entry_points] console_scripts, gui_scripts, etc.)
          - package.json    ("bin": string|object)

  • entrypoint.python
      - Python files that contain an executable guard: `if __name__ == "__main__":`
      - Heuristically notes if a `def main(...)` exists

  • entrypoint.shell
      - Shell/batch scripts identified either by extension (.sh/.bash/.cmd/.bat/.ps1)
        or a shebang (`#!` in first line)

  • entrypoints.summary
      - Counts and simple top lists

Notes
-----
* Pure standard library (uses tomllib on Python 3.11+).
* Paths are repo-relative POSIX. The caller may remap them for local/GitHub.
"""

from __future__ import annotations

import json
import os
import re
import sys
import configparser
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

# tomllib is stdlib in Python 3.11+; fall back gracefully if unavailable
try:
    import tomllib  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

RepoItem = Tuple[Path, str]  # (local_path, repo_relative_posix)

# Heuristics / limits
_PY_MAIN_GUARD = re.compile(r'(?m)^\s*if\s+__name__\s*==\s*[\'"]__main__[\'"]\s*:')
_PY_DEF_MAIN = re.compile(r'(?m)^\s*def\s+main\s*\(')
_SHEBANG = re.compile(r"^#!\s*(\S+)")
_MAX_HEADER_BYTES = 128 * 1024  # when scanning text-ish files

_SHELL_EXTS = {".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd"}
_TEXT_LIKE_EXTS = {
    ".py", ".pyi", ".pyw",
    ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd",
    ".toml", ".cfg", ".ini", ".json", ".jsonc", ".md", ".txt",
}

def _read_text_head(path: Path, limit: int = _MAX_HEADER_BYTES) -> str:
    try:
        data = path.read_bytes()[:limit]
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""

def _dotted_module(rel_posix: str) -> Optional[str]:
    """
    Convert a repo-relative path to a dotted module, best-effort.
    E.g., 'pkg/app/cli.py' -> 'pkg.app.cli'
    """
    p = Path(rel_posix)
    if p.suffix.lower() != ".py":
        return None
    parts = list(p.with_suffix("").parts)
    if not parts:
        return None
    return ".".join(parts)

# ──────────────────────────────────────────────────────────────────────────────
# Config parsers
# ──────────────────────────────────────────────────────────────────────────────

def _parse_pyproject(pyproject: Path) -> List[Dict]:
    recs: List[Dict] = []
    if not tomllib:
        return recs
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return recs

    # PEP 621
    proj = data.get("project") or {}
    scripts = proj.get("scripts") or {}
    gui = proj.get("gui-scripts") or {}
    for name, target in (scripts or {}).items():
        recs.append({
            "kind": "entrypoint.config",
            "source": "pyproject.project.scripts",
            "name": str(name),
            "target": str(target),
            "path": pyproject.relative_to(pyproject.parents[len(pyproject.parts)-len(pyproject.parts)] if False else pyproject).as_posix(),
        })
    for name, target in (gui or {}).items():
        recs.append({
            "kind": "entrypoint.config",
            "source": "pyproject.project.gui-scripts",
            "name": str(name),
            "target": str(target),
            "path": pyproject.as_posix(),
        })

    # Poetry
    tool = data.get("tool") or {}
    poetry = tool.get("poetry") or {}
    p_scripts = poetry.get("scripts") or {}
    for name, target in (p_scripts or {}).items():
        recs.append({
            "kind": "entrypoint.config",
            "source": "pyproject.tool.poetry.scripts",
            "name": str(name),
            "target": str(target),
            "path": pyproject.as_posix(),
        })
    return recs

def _parse_setup_cfg(setup_cfg: Path) -> List[Dict]:
    recs: List[Dict] = []
    cp = configparser.ConfigParser()
    try:
        cp.read(setup_cfg, encoding="utf-8")
    except Exception:
        return recs

    sec = "options.entry_points"
    if not cp.has_section(sec):
        return recs

    # Collect each group: console_scripts, gui_scripts, etc.
    for key, raw in cp.items(sec):
        # 'raw' may be a multi-line INI value with entries like "name = pkg.mod:func"
        lines = []
        for line in (raw or "").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or s.startswith(";"):
                continue
            lines.append(s)
        if not lines:
            continue
        for line in lines:
            # Split on '=' or ' = '
            if "=" in line:
                name, target = [x.strip() for x in line.split("=", 1)]
            else:
                # e.g. "name:pkg.mod:func" (very rare); keep full line
                name, target = line, line
            recs.append({
                "kind": "entrypoint.config",
                "source": f"setup.cfg[{key}]",
                "name": name,
                "target": target,
                "path": setup_cfg.as_posix(),
            })
    return recs

def _parse_package_json(pkg_json: Path) -> List[Dict]:
    recs: List[Dict] = []
    try:
        data = json.loads(pkg_json.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return recs

    # "bin" can be a string (single command) or an object map
    if "bin" in data:
        bin_field = data["bin"]
        if isinstance(bin_field, str):
            recs.append({
                "kind": "entrypoint.config",
                "source": "package.json.bin",
                "name": data.get("name") or "<bin>",
                "target": bin_field,
                "path": pkg_json.as_posix(),
            })
        elif isinstance(bin_field, dict):
            for name, target in bin_field.items():
                recs.append({
                    "kind": "entrypoint.config",
                    "source": "package.json.bin",
                    "name": str(name),
                    "target": str(target),
                    "path": pkg_json.as_posix(),
                })
    return recs

# ──────────────────────────────────────────────────────────────────────────────
# File heuristics
# ──────────────────────────────────────────────────────────────────────────────

def _is_shell_script(path: Path, head: Optional[str]) -> bool:
    if path.suffix.lower() in _SHELL_EXTS:
        return True
    first = (head.splitlines()[0] if head else "") if head is not None else ""
    return bool(_SHEBANG.search(first))

def _python_entrypoint_record(rel: str, head: str) -> Optional[Dict]:
    if not head or "__main__" not in head:
        return None
    if not _PY_MAIN_GUARD.search(head):
        return None
    dotted = _dotted_module(rel)
    has_main_fn = bool(_PY_DEF_MAIN.search(head))
    return {
        "kind": "entrypoint.python",
        "path": rel,
        "module": dotted,
        "has_main_fn": has_main_fn,
    }

def _shell_entrypoint_record(rel: str, head: str) -> Optional[Dict]:
    if not _is_shell_script(Path(rel), head):
        return None
    # Note interpreter if shebang present
    first = head.splitlines()[0] if head else ""
    m = _SHEBANG.search(first)
    interp = m.group(1) if m else None
    return {
        "kind": "entrypoint.shell",
        "path": rel,
        "interpreter": interp,
    }

# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def scan(repo_root: Path, discovered: Iterable[RepoItem]) -> List[Dict]:
    """
    Scan for common entrypoint declarations & files.

    Returns a list of dict records suitable for appending to the design manifest.
    """
    records: List[Dict] = []
    repo_root = Path(repo_root)
    items = list(discovered)

    # Config-based entrypoints
    # Prefer scanning via explicit filenames; use discovered list to find them quickly
    paths_map = {rel: local for (local, rel) in items}
    # pyproject.toml at repo root (or nested — take all)
    for (local, rel) in items:
        name = Path(rel).name.lower()
        if name == "pyproject.toml":
            records.extend(_parse_pyproject(local))
        elif name == "setup.cfg":
            records.extend(_parse_setup_cfg(local))
        elif name == "package.json":
            records.extend(_parse_package_json(local))

    # File heuristics (python/shell)
    for local, rel in items:
        # Quick skip for obviously non-text (use extension hints)
        ext = Path(rel).suffix.lower()
        if ext and (ext not in _TEXT_LIKE_EXTS):
            continue

        head = _read_text_head(local)

        py_rec = _python_entrypoint_record(rel, head)
        if py_rec:
            records.append(py_rec)

        sh_rec = _shell_entrypoint_record(rel, head)
        if sh_rec:
            records.append(sh_rec)

    # Summary
    counts: Dict[str, int] = {}
    for r in records:
        k = r.get("kind")
        if not k:
            continue
        counts[k] = counts.get(k, 0) + 1

    summary = {
        "kind": "entrypoints.summary",
        "counts": counts,
        "examples": {
            "config": [r["name"] for r in records if r.get("kind") == "entrypoint.config"][:10],
            "python": [r["path"] for r in records if r.get("kind") == "entrypoint.python"][:10],
            "shell": [r["path"] for r in records if r.get("kind") == "entrypoint.shell"][:10],
        },
    }
    records.append(summary)
    return records


__all__ = ["scan"]

