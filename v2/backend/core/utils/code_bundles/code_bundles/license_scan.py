# File: v2/backend/core/utils/code_bundles/code_bundles/license_scan.py
"""
Lightweight license scanner (stdlib-only).

Emits JSONL-ready records:

  • license.header
      - Found SPDX headers in source files (from the first N lines)
      - Fields: path, line, spdx_id, snippet

  • license.file
      - Detected top-level license-like files (LICENSE, COPYING, NOTICE, etc.)
      - Fields: path, kind, sha256, size, hints (best-effort ID guess)

  • license.summary
      - Aggregate counts and top examples

Notes
-----
* Standard library only (no external deps).
* Paths are repo-relative POSIX; run_pack remaps if needed (local/github).
* Conservative heuristics to avoid noisy false positives.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

RepoItem = Tuple[Path, str]  # (local_path, repo_relative_posix)

# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────

_HEADER_SCAN_MAX_LINES = 80
_TEXT_MAX_BYTES = 2 * 1024 * 1024  # 2 MiB

LICENSE_FILE_NAMES = {
    "LICENSE", "LICENSE.txt", "LICENSE.md", "LICENSE.rst",
    "COPYING", "COPYING.txt", "COPYING.md", "COPYRIGHT", "COPYRIGHT.txt",
    "NOTICE", "NOTICE.txt", "THIRD_PARTY_NOTICES", "THIRD_PARTY_NOTICES.txt",
}

# Safe character class for SPDX IDs: letters, digits, dot, plus, hyphen
_RE_SPDX = re.compile(r"(?i)\bSPDX-License-Identifier:\s*([A-Za-z0-9.+-]+)\b")

# Very small, best-effort ID clues for full license files
_ID_HINTS: List[Tuple[str, str]] = [
    (r"\bApache\s+License\b.*\bVersion\s+2\.0\b", "Apache-2.0"),
    (r"\bMIT\s+License\b", "MIT"),
    (r"\bBSD\b.*(2\-Clause|3\-Clause)", "BSD-2/3-Clause"),
    (r"\bGNU\s+GENERAL\s+PUBLIC\s+LICENSE\b.*\bVersion\s*3\b", "GPL-3.0"),
    (r"\bGNU\s+GENERAL\s+PUBLIC\s+LICENSE\b.*\bVersion\s*2\b", "GPL-2.0"),
    (r"\bLesser\s+General\s+Public\s+License\b.*\bVersion\s*3\b", "LGPL-3.0"),
    (r"\bMozilla\s+Public\s+License\b.*\b2\.0\b", "MPL-2.0"),
    (r"\bEclipse\s+Public\s+License\b.*\b2\.0\b", "EPL-2.0"),
    (r"\bUnlicense\b", "Unlicense"),
    (r"\bCreative\s+Commons\b.*\bBY\b.*\b[34]\.0\b", "CC-BY"),
]

_TEXT_EXTS = {
    ".txt", ".md", ".rst", ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".kt", ".go",
    ".rs", ".c", ".h", ".cpp", ".hpp", ".cs", ".php", ".rb", ".swift", ".scala",
    ".sh", ".bash", ".zsh", ".ps1", ".ini", ".cfg", ".conf", ".toml", ".yaml", ".yml",
    ".json", ".jsonc",
}

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _sha256_file(path: Path) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None

def _read_text_limited(path: Path, limit: int = _TEXT_MAX_BYTES) -> str:
    try:
        data = path.read_bytes()[: limit + 1]
        if len(data) > limit:
            data = data[:limit]
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""

def _is_text_like(path: Path) -> bool:
    # Heuristic by extension
    ext = path.suffix.lower()
    return ext in _TEXT_EXTS or ext == ""  # allow extension-less files (often LICENSE)

# ──────────────────────────────────────────────────────────────────────────────
# Scanners
# ──────────────────────────────────────────────────────────────────────────────

def _scan_spdx_headers(local: Path, rel: str) -> List[Dict]:
    if not _is_text_like(local):
        return []
    text = _read_text_limited(local, limit=128 * 1024)  # only need the head
    if not text:
        return []
    lines = text.splitlines()
    head = "\n".join(lines[:_HEADER_SCAN_MAX_LINES])
    recs: List[Dict] = []
    for m in _RE_SPDX.finditer(head):
        spdx_id = m.group(1)
        # find 1-based line number of the match
        before = head[: m.start(1)]
        line_no = before.count("\n") + 1
        # capture a tiny snippet for context (the matched line)
        try:
            snippet = lines[line_no - 1][:300]
        except Exception:
            snippet = ""
        recs.append({
            "kind": "license.header",
            "path": rel,
            "line": line_no,
            "spdx_id": spdx_id,
            "snippet": snippet,
        })
    return recs

def _guess_id_from_text(text: str) -> Optional[str]:
    low = text if text is not None else ""
    for pat, spdx in _ID_HINTS:
        try:
            if re.search(pat, low, flags=re.IGNORECASE | re.DOTALL):
                return spdx
        except re.error:
            # Shouldn't happen; patterns are static
            continue
    return None

def _scan_license_files(local: Path, rel: str) -> List[Dict]:
    name = Path(rel).name
    if name not in LICENSE_FILE_NAMES:
        return []
    # Only treat as text if reasonably small or extension-less known names
    if not _is_text_like(local):
        return []
    text = _read_text_limited(local)
    sha = _sha256_file(local)
    try:
        size = int(local.stat().st_size)
    except Exception:
        size = len(text.encode("utf-8", errors="ignore"))
    # Classify file kind by canonical name family
    base = name.upper()
    file_kind = (
        "license" if base.startswith("LICENSE") or base.startswith("COPYING") else
        "notice" if "NOTICE" in base else
        "copyright" if "COPYRIGHT" in base else
        "license"
    )
    rec = {
        "kind": "license.file",
        "path": rel,
        "file_kind": file_kind,
        "sha256": sha,
        "size": size,
        "hints": {
            "spdx_id": _guess_id_from_text(text),
        },
    }
    return [rec]

# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def scan(repo_root: Path, discovered: Iterable[RepoItem]) -> List[Dict]:
    """
    Search for SPDX headers in source files and for top-level license-ish files.
    Returns JSONL-ready records plus a summary.
    """
    records: List[Dict] = []
    header_count = 0
    file_count = 0
    by_spdx: Dict[str, int] = {}

    for local, rel in discovered:
        # SPDX headers in any text-like source/config
        hdrs = _scan_spdx_headers(local, rel)
        if hdrs:
            records.extend(hdrs)
            header_count += len(hdrs)
            for h in hdrs:
                sid = h.get("spdx_id")
                if sid:
                    by_spdx[sid] = by_spdx.get(sid, 0) + 1

        # License/notice files by name
        lfs = _scan_license_files(local, rel)
        if lfs:
            records.extend(lfs)
            file_count += len(lfs)
            sid = lfs[0].get("hints", {}).get("spdx_id")
            if sid:
                by_spdx[sid] = by_spdx.get(sid, 0) + 1

    summary = {
        "kind": "license.summary",
        "headers": header_count,
        "files": file_count,
        "by_spdx": dict(sorted(by_spdx.items(), key=lambda kv: (-kv[1], kv[0]))),
        "examples": {
            "headers": [r["path"] for r in records if r.get("kind") == "license.header"][:10],
            "files": [r["path"] for r in records if r.get("kind") == "license.file"][:10],
        },
    }
    records.append(summary)
    return records


__all__ = ["scan"]

