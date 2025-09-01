# File: v2/backend/core/utils/code_bundles/code_bundles/owners_index.py
"""
CODEOWNERS scanner (stdlib-only).

Finds and parses CODEOWNERS files and assigns owners to discovered files.

Emits JSONL-ready records:

Per rule:
  {
    "kind": "codeowners.rule",
    "source": ".github/CODEOWNERS",
    "lineno": 12,
    "pattern": "/v2/backend/**",
    "owners": ["@team/backend","@you"],
    "index": 7
  }

Per file assignment (last matching rule wins):
  {
    "kind": "codeowners.assignment",
    "path": "v2/backend/core/utils/code_bundles/code_bundles/run_pack.py",
    "owners": ["@team/backend","@you"],
    "rule_index": 7,
    "pattern": "/v2/backend/**",
    "source": ".github/CODEOWNERS"
  }

Summary:
  {
    "kind": "codeowners.summary",
    "sources": [".github/CODEOWNERS","CODEOWNERS"],
    "files": 1234,
    "assigned": 1200,
    "unassigned": 34,
    "owners": {
      "@team/backend": 1100,
      "@you": 950,
      "dev@company.com": 50
    },
    "top_unassigned_dirs": [{"dir":"v2/experimental","count":17}, ...]
  }

Notes
-----
* Matching semantics approximate GitHub's CODEOWNERS behavior using fnmatch:
  - Patterns are POSIX globs, '/'-separated.
  - Leading '/' anchors from repo root; otherwise match anywhere.
  - A pattern ending with '/' matches a directory prefix.
  - If a pattern contains no '/', we also test against the basename.
  - The last matching rule wins (later lines override earlier lines).
* Negation ('!') patterns are ignored (GitHub CODEOWNERS does not support them).
* Paths returned are repo-relative POSIX. If your pipeline distinguishes local
  vs GitHub path modes, map `path` with your existing mapper before appending.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

RepoItem = Tuple[Path, str]  # (local_path, repo_relative_posix)


# ──────────────────────────────────────────────────────────────────────────────
# Parsing
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Rule:
    source: str
    lineno: int
    index: int
    pattern: str
    owners: List[str]


def _strip_inline_comment(line: str) -> str:
    """
    Remove a trailing comment starting with '#' if it's preceded by whitespace
    or at line start. This is a pragmatic approach for CODEOWNERS.
    """
    s = line.strip("\n")
    if not s:
        return ""
    # If the line starts with '#', it's a full comment.
    if s.lstrip().startswith("#"):
        return ""
    # Find a ' #' sequence (space + #) and strip everything after it.
    hash_pos = s.find(" #")
    if hash_pos != -1:
        return s[:hash_pos].rstrip()
    # If there is a '#' but not following space, many CODEOWNERS still treat as start of comment.
    # Be conservative: if there is a '#' and it's not part of an owner (unlikely), cut there.
    if "#" in s:
        before, _hash, _after = s.partition("#")
        return before.rstrip()
    return s


def _parse_codeowners_file(co_path: Path, start_index: int = 0) -> List[Rule]:
    """
    Parse a CODEOWNERS file into Rule entries, assigning sequential indices.
    """
    rules: List[Rule] = []
    idx = start_index
    try:
        text = co_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return rules

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = _strip_inline_comment(raw).strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        pat = parts[0].strip()
        # GitHub CODEOWNERS does not support '!' negation; ignore if present.
        if pat.startswith("!"):
            continue
        owners = [p for p in parts[1:] if p]
        if not owners:
            continue
        idx += 1
        rules.append(Rule(
            source=co_path.as_posix(),
            lineno=lineno,
            index=idx,
            pattern=pat,
            owners=owners,
        ))
    return rules


def _find_codeowners_files(repo_root: Path) -> List[Path]:
    """
    Search for CODEOWNERS in common locations, in precedence order.
    """
    candidates = [
        repo_root / ".github" / "CODEOWNERS",
        repo_root / "docs" / "CODEOWNERS",
        repo_root / "CODEOWNERS",
        repo_root / ".gitlab" / "CODEOWNERS",
    ]
    found: List[Path] = []
    for p in candidates:
        if p.exists() and p.is_file():
            found.append(p)
    # Also pick up any nested CODEOWNERS files (rare, but some repos do it)
    # e.g., "config/CODEOWNERS" or similar
    for p in repo_root.rglob("CODEOWNERS"):
        try:
            # Avoid re-adding the same canonical files already included
            if p.resolve() not in [f.resolve() for f in found]:
                found.append(p)
        except Exception:
            # Best-effort: add it if not obviously a dup
            if p.as_posix() not in [f.as_posix() for f in found]:
                found.append(p)
    # Keep deterministic order: predefined ones first, then sorted extras
    # Ensure uniqueness by path string.
    unique_paths = []
    seen = set()
    for p in candidates + sorted([p for p in found if p not in candidates], key=lambda x: x.as_posix()):
        key = p.as_posix()
        if key not in seen:
            seen.add(key)
            if p.exists() and p.is_file():
                unique_paths.append(p)
    return unique_paths


# ──────────────────────────────────────────────────────────────────────────────
# Matching
# ──────────────────────────────────────────────────────────────────────────────

def _norm_pattern(pat: str) -> str:
    """
    Normalize a CODEOWNERS pattern into a POSIX glob that fnmatch can use.
    We do not add '**/' automatically; we treat pattern as-is except:
      - strip leading '/' (anchor) for matching relative paths
      - keep trailing '/' (handled specially)
    """
    return pat.lstrip("/")

def _matches(pattern: str, rel_path: str) -> bool:
    """
    Approximate CODEOWNERS matching rules using fnmatch.
    """
    pat = _norm_pattern(pattern)
    path = rel_path.lstrip("/")

    # Directory-only match: trailing '/'
    if pat.endswith("/"):
        prefix = pat[:-1]
        if not prefix:
            return True  # '/' -> all
        return path.startswith(prefix if prefix.endswith("/") else prefix + "/")

    # If pattern contains no '/', it should match basenames anywhere
    if "/" not in pat:
        from os.path import basename
        if fnmatch.fnmatch(basename(path), pat):
            return True

    # Otherwise match against the full relative path
    if fnmatch.fnmatch(path, pat):
        return True

    # Some CODEOWNERS examples behave like '**/pat' when not anchored and contains '/'
    # Try a fallback that allows match anywhere:
    if not pattern.startswith("/") and "/" in pat:
        if fnmatch.fnmatch(path, f"**/{pat}"):
            return True

    return False


# ──────────────────────────────────────────────────────────────────────────────
# Scanner
# ──────────────────────────────────────────────────────────────────────────────

def _summarize_unassigned(paths: List[str], top_n: int = 10) -> List[Dict[str, int]]:
    """
    Return top N directories with most unassigned files.
    """
    from collections import Counter
    c = Counter()
    for p in paths:
        # directory part (first segment or two)
        parts = p.split("/")
        if len(parts) >= 2:
            key = f"{parts[0]}/{parts[1]}"
        elif parts:
            key = parts[0]
        else:
            key = ""
        c[key] += 1
    top = c.most_common(top_n)
    return [{"dir": k, "count": v} for k, v in top if k]

def scan(repo_root: Path, discovered: Iterable[RepoItem]) -> List[Dict]:
    """
    Scan CODEOWNERS files and assign owners for discovered files.

    Returns a list of manifest records:
      - One 'codeowners.rule' per parsed line
      - One 'codeowners.assignment' per discovered file (if matched)
      - One 'codeowners.summary' at the end
    """
    repo_root = Path(repo_root)
    files: List[str] = [rel for (_lp, rel) in discovered]

    # Parse rules from all found CODEOWNERS files
    codeowners_paths = _find_codeowners_files(repo_root)
    rules: List[Rule] = []
    next_index = 0
    for p in codeowners_paths:
        parsed = _parse_codeowners_file(p, start_index=next_index)
        if parsed:
            next_index = parsed[-1].index
            rules.extend(parsed)

    records: List[Dict] = []

    # Emit rule records
    for r in rules:
        records.append({
            "kind": "codeowners.rule",
            "source": r.source,
            "lineno": r.lineno,
            "pattern": r.pattern,
            "owners": list(r.owners),
            "index": r.index,
        })

    # Assign files: last matching rule wins
    owner_counts: Dict[str, int] = {}
    assigned = 0
    unassigned_paths: List[str] = []

    # Fast path: if no rules, produce only empty summary
    if not rules:
        unassigned_paths = list(files)
        summary = {
            "kind": "codeowners.summary",
            "sources": [],
            "files": len(files),
            "assigned": 0,
            "unassigned": len(unassigned_paths),
            "owners": {},
            "top_unassigned_dirs": _summarize_unassigned(unassigned_paths),
        }
        records.append(summary)
        return records

    # Evaluate matches
    for rel in files:
        match: Optional[Rule] = None
        for r in rules:
            if _matches(r.pattern, rel):
                match = r  # keep last match
        if match is None:
            unassigned_paths.append(rel)
            continue

        assigned += 1
        # Increment owner tallies
        for ow in match.owners:
            owner_counts[ow] = owner_counts.get(ow, 0) + 1

        # Emit assignment record
        records.append({
            "kind": "codeowners.assignment",
            "path": rel,
            "owners": list(match.owners),
            "rule_index": match.index,
            "pattern": match.pattern,
            "source": match.source,
        })

    # Summary
    summary = {
        "kind": "codeowners.summary",
        "sources": [p.as_posix() for p in codeowners_paths],
        "files": len(files),
        "assigned": assigned,
        "unassigned": len(unassigned_paths),
        "owners": dict(sorted(owner_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "top_unassigned_dirs": _summarize_unassigned(unassigned_paths),
    }
    records.append(summary)
    return records


__all__ = ["scan"]
