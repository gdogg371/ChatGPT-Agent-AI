# File: v2/backend/core/utils/code_bundles/code_bundles/secrets_scan.py
"""
Lightweight secrets scanner (stdlib-only).

Emits JSONL-ready records for:
  - secrets.finding : one record per detected secret (with minimal, redacted context)
  - secrets.summary : one summary record with counts and top paths

Design goals
------------
* Pure-Python standard library; no external dependencies.
* Conservative patterns for common, high-signal credentials (reduced false positives).
* Small, privacy-aware payloads (prefix/suffix only; full values are never emitted).
* Works on any text file. Attempts to skip obvious binaries and huge files.

Notes
-----
* Paths are repo-relative POSIX (use the discovery helper to supply them).
* Callers can remap 'path' for different path modes (local vs GitHub) prior to
  appending to the design manifest.
"""

from __future__ import annotations

import base64
import math
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

RepoItem = Tuple[Path, str]  # (local_path, repo_relative_posix)

# ──────────────────────────────────────────────────────────────────────────────
# Limits & basic config
# ──────────────────────────────────────────────────────────────────────────────

_MAX_FILE_BYTES = 2 * 1024 * 1024  # 2 MiB per file cap
_MAX_FINDINGS_PER_FILE = 100       # safety cap
_CONTEXT_CHARS = 80                # evidence context length
_PREVIEW_PREFIX = 6                # preview preserves first N chars
_PREVIEW_SUFFIX = 2                # preview preserves last N chars

# If any of these substrings exist on a line, we skip findings for that line
_IGNORE_LINE_MARKERS = (
    "secrets:ignore",
    "secret-scan:ignore",
    "secret: ignore",
)

# Treat these extensions as "likely-binary" and skip
_BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico",
    ".pdf", ".zip", ".gz", ".tgz", ".xz", ".7z", ".rar",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".mp3", ".mp4", ".mov", ".avi", ".m4a", ".m4v", ".webm",
    ".jar", ".class", ".so", ".dll", ".dylib", ".bin", ".exe", ".pdb",
}

_TEXT_LIKE_EXT_HINTS = {
    ".env", ".ini", ".cfg", ".conf", ".toml", ".yaml", ".yml",
    ".json", ".jsonc", ".md", ".txt", ".tsv", ".csv",
    ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd",
    ".py", ".pyi", ".pyw",
    ".js", ".jsx", ".ts", ".tsx",
    ".java", ".kt", ".kts", ".scala",
    ".go", ".rs", ".rb", ".php", ".pl", ".swift",
    ".c", ".h", ".cpp", ".cc", ".hpp",
    ".cs",
    ".sql",
    ".dockerfile",  # custom convention
}

# ──────────────────────────────────────────────────────────────────────────────
# Utilities
# ──────────────────────────────────────────────────────────────────────────────

def _is_probably_binary_bytes(b: bytes) -> bool:
    # Heuristic: too many NULs or high-bit bytes
    if not b:
        return False
    if b.count(b"\x00") > 0:
        return True
    # If > 30% are non-text (outside 9,10,13,32..126), regard as binary
    text_whitelist = set(range(32, 127)) | {9, 10, 13}
    weird = sum(1 for x in b if x not in text_whitelist)
    return (weird / max(1, len(b))) > 0.30

def _should_skip_file(path: Path) -> bool:
    ext = path.suffix.lower()
    if ext in _BINARY_EXTS:
        return True
    # quick peek
    try:
        sample = path.read_bytes()[:4096]
        return _is_probably_binary_bytes(sample)
    except Exception:
        # unreadable => skip silently
        return True

def _read_text_limited(path: Path, limit: int = _MAX_FILE_BYTES) -> str:
    try:
        data = path.read_bytes()[: limit + 1]
        if len(data) > limit:
            data = data[:limit]
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""

def _entropy_shannon(s: str) -> float:
    if not s:
        return 0.0
    freq = Counter(s)
    n = float(len(s))
    return -sum((c / n) * math.log2(c / n) for c in freq.values())

def _redact(value: str) -> str:
    if not value:
        return ""
    if len(value) <= _PREVIEW_PREFIX + _PREVIEW_SUFFIX:
        return value[0:1] + "…"  # tiny
    return f"{value[:_PREVIEW_PREFIX]}…{value[-_PREVIEW_SUFFIX:]}"

def _line_col_from_offset(text: str, offset: int) -> Tuple[int, int]:
    # 1-based line & column for UX
    before = text[:offset]
    line = before.count("\n") + 1
    col = len(before.split("\n")[-1]) + 1
    return line, col

def _line_has_ignore_marker(line: str) -> bool:
    low = line.lower()
    return any(m in low for m in _IGNORE_LINE_MARKERS)

# ──────────────────────────────────────────────────────────────────────────────
# Patterns (ordered: high signal first)
# Each entry: (id, compiled_regex, severity, value_group, min_entropy, note)
# value_group: which capture to treat as the secret value (0 => whole match)
# min_entropy: if not None, require entropy >= threshold to accept finding
# ──────────────────────────────────────────────────────────────────────────────

_PATTERNS: List[Tuple[str, re.Pattern, str, int, Optional[float], str]] = [
    # Private keys (multi-line, but we detect the BEGIN line)
    ("private_key", re.compile(r"-----BEGIN (?:(?:RSA|DSA|EC|OPENSSH)\s+)?PRIVATE KEY-----"), "high", 0, None, "PEM private key"),
    # GitHub personal / fine-grained access tokens
    ("github.token", re.compile(r"\b(?:gh[pousr]|ghe)_[A-Za-z0-9]{36,}\b"), "high", 0, 3.3, "GitHub token"),
    ("github.pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{10,}_[A-Za-z0-9]{20,}\b"), "high", 0, 3.3, "GitHub fine-grained PAT"),
    # AWS
    ("aws.access_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "high", 0, None, "AWS Access Key ID"),
    # Google API
    ("google.api_key", re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b"), "high", 0, 3.0, "Google API key"),
    # Slack tokens
    ("slack.token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,48}\b"), "high", 0, 3.0, "Slack token"),
    # Stripe
    ("stripe.secret_key", re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{16,64}\b"), "high", 0, 3.2, "Stripe secret key"),
    ("stripe.webhook_secret", re.compile(r"\bwhsec_[A-Za-z0-9]{16,64}\b"), "high", 0, 3.2, "Stripe webhook secret"),
    # Twilio
    ("twilio.account_sid", re.compile(r"\bAC[0-9a-fA-F]{32}\b"), "medium", 0, None, "Twilio Account SID"),
    # JWT (starts with 'eyJ' due to '{"' base64, three parts)
    ("jwt", re.compile(r"\beyJ[0-9A-Za-z_\-]+?\.[0-9A-Za-z_\-]+(?:\.[0-9A-Za-z_\-]+)?\b"), "medium", 0, 3.0, "Likely JWT"),
    # Azure Storage connection string (AccountKey=...)
    ("azure.storage_key", re.compile(r"\bAccountKey=([A-Za-z0-9+/=]{20,})\b"), "high", 1, 3.2, "Azure Storage key"),
    # Generic API key assignment (guard with entropy)
    ("generic.secret_assign", re.compile(
        r"""(?ix)
        \b
        (?:api[_-]?key|token|secret|passwd|password|bearer[_-]?token)
        \b
        [\s:=]+
        (?:
            ["']?([A-Za-z0-9._\-=/]{12,})["']?
        )
        """), "medium", 1, 3.3, "Generic secret assignment"),
    # Bearer token header
    ("http.bearer", re.compile(r"\bBearer\s+([A-Za-z0-9_\-\.=]{20,})"), "medium", 1, 3.3, "HTTP Bearer token"),
]

# ──────────────────────────────────────────────────────────────────────────────
# Core scanning
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Finding:
    kind: str
    path: str
    line: int
    span: Tuple[int, int]
    id: str
    severity: str
    note: str
    value_preview: str
    entropy: Optional[float]
    evidence: str
    pattern: str

def _gather_findings(text: str, path_rel: str) -> List[Finding]:
    findings: List[Finding] = []
    if not text:
        return findings

    # Fast path to ignore entire files, e.g., huge minified bundles: if a single "line" is extremely long
    if any(len(l) > 20000 for l in text.splitlines()[:100]):
        return findings

    # Iterate patterns
    for pid, rx, severity, grp, min_ent, note in _PATTERNS:
        for m in rx.finditer(text):
            # Skip lines with explicit ignore
            line_no, col = _line_col_from_offset(text, m.start(grp if grp else 0))
            try:
                line_text = text.splitlines()[line_no - 1]
            except Exception:
                line_text = ""
            if _line_has_ignore_marker(line_text):
                continue

            # Extract the matched value (group or whole match)
            v = m.group(grp) if grp else m.group(0)
            v = v or ""
            ent = _entropy_shannon(v) if v else None
            if min_ent is not None and ent is not None and ent < min_ent:
                # Entropy too low → likely not a secret
                continue

            # Build short evidence around the match (same line window)
            start = max(0, m.start() - _CONTEXT_CHARS // 2)
            end = min(len(text), m.end() + _CONTEXT_CHARS // 2)
            snippet = text[start:end].replace("\n", "\\n")

            findings.append(Finding(
                kind="secrets.finding",
                path=path_rel,
                line=line_no,
                span=(col, col + (len(v) if v else (m.end() - m.start())) - 1),
                id=pid,
                severity=severity,
                note=note,
                value_preview=_redact(v),
                entropy=round(ent, 3) if ent is not None else None,
                evidence=snippet[:_CONTEXT_CHARS],
                pattern=rx.pattern,
            ))
            if len(findings) >= _MAX_FINDINGS_PER_FILE:
                return findings
    return findings

def _scan_file(local: Path, repo_rel: str) -> List[Finding]:
    # Skip obvious binaries
    # Allow text-like extensions even if binary heuristic flags, but still sample
    ext = local.suffix.lower()
    force_text = (ext in _TEXT_LIKE_EXT_HINTS) or (local.name.lower() in (".env", "env"))
    if not force_text and _should_skip_file(local):
        return []

    text = _read_text_limited(local, _MAX_FILE_BYTES)
    if not text:
        return []

    return _gather_findings(text, repo_rel)

# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def scan(repo_root: Path, discovered: Iterable[RepoItem]) -> List[Dict]:
    """
    Scan discovered repository items for secrets.

    Returns:
      - One 'secrets.finding' record per detected secret (redacted)
      - One 'secrets.summary' record at the end
    """
    results: List[Dict] = []
    counts_by_id: Counter[str] = Counter()
    counts_by_severity: Counter[str] = Counter()
    files_with_findings: Counter[str] = Counter()

    total_files = 0
    scanned_files = 0
    skipped_binary = 0

    for local, rel in discovered:
        total_files += 1
        # quick binary skip reuses the same logic as per-file scan
        ext = Path(rel).suffix.lower()
        force_text = (ext in _TEXT_LIKE_EXT_HINTS) or (Path(rel).name.lower() in (".env", "env"))
        if not force_text and (ext in _BINARY_EXTS):
            skipped_binary += 1
            continue

        findings = _scan_file(local, rel)
        if not findings:
            continue

        scanned_files += 1
        files_with_findings[rel] += 1

        for f in findings:
            rec = {
                "kind": f.kind,
                "path": f.path,
                "line": f.line,
                "span": list(f.span),
                "id": f.id,
                "severity": f.severity,
                "note": f.note,
                "value_preview": f.value_preview,
                "entropy": f.entropy,
                "evidence": f.evidence,
            }
            results.append(rec)
            counts_by_id[f.id] += 1
            counts_by_severity[f.severity] += 1

    top_files = [{"path": p, "findings": c} for (p, c) in files_with_findings.most_common(20)]

    summary = {
        "kind": "secrets.summary",
        "files_total": total_files,
        "files_with_findings": sum(1 for _ in files_with_findings),
        "skipped_binary": skipped_binary,
        "findings_total": sum(counts_by_id.values()),
        "by_id": dict(counts_by_id),
        "by_severity": dict(counts_by_severity),
        "top_files": top_files,
        "hints": {
            "ignore_marker": _IGNORE_LINE_MARKERS[0],
            "redaction": f"prefix {_PREVIEW_PREFIX} chars + suffix {_PREVIEW_SUFFIX} chars",
        },
    }
    results.append(summary)
    return results


__all__ = ["scan"]
