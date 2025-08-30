# File: v2/backend/core/utils/code_bundles/code_bundles/bundle_io.py
"""
Utilities for reading/writing the design manifest and related artifacts.

Exports
-------
ManifestAppender
emit_standard_artifacts
emit_transport_parts
rewrite_manifest_paths
write_sha256sums_for_file
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, List, Optional


# ──────────────────────────────────────────────────────────────────────────────
# Manifest appender
# ──────────────────────────────────────────────────────────────────────────────

class ManifestAppender:
    """
    Simple JSONL appender with a helper to ensure a single manifest.header
    record appears at the top of the file.
    """

    def __init__(self, manifest_path: Path) -> None:
        self.manifest_path = Path(manifest_path)
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.manifest_path.exists():
            # Start an empty file so ensure_header/append_record work uniformly.
            self.manifest_path.write_text("", encoding="utf-8")

    # Internal: read all lines (no newline suffix)
    def _read_lines(self) -> List[str]:
        if not self.manifest_path.exists():
            return []
        raw = self.manifest_path.read_text(encoding="utf-8", errors="replace")
        if not raw:
            return []
        return [ln.rstrip("\n") for ln in raw.splitlines()]

    def _write_lines(self, lines: Iterable[str]) -> None:
        text = "\n".join([ln.rstrip("\n") for ln in lines]) + "\n"
        self.manifest_path.write_text(text, encoding="utf-8")

    def ensure_header(self, header_record: dict) -> None:
        """
        If the first non-empty line is not a {"kind":"manifest.header"} record,
        insert one at the top. If an existing header is present, do nothing.
        """
        lines = self._read_lines()

        # Find first non-empty, parse if possible
        first_idx = None
        first_obj: Optional[dict] = None
        for i, ln in enumerate(lines):
            if ln.strip() == "":
                continue
            first_idx = i
            try:
                obj = json.loads(ln)
                if isinstance(obj, dict):
                    first_obj = obj
            except Exception:
                first_obj = None
            break

        if first_obj and first_obj.get("kind") == "manifest.header":
            # Already has a header
            return

        # Insert header at top (before any existing content)
        header_line = json.dumps(header_record, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        if first_idx is None:
            # Empty file
            self._write_lines([header_line])
            return

        new_lines = lines[:]
        new_lines.insert(0, header_line)
        self._write_lines(new_lines)

    def append_record(self, record: dict) -> None:
        """
        Append a JSON record as a single JSONL line.
        """
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        with self.manifest_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


# ──────────────────────────────────────────────────────────────────────────────
# Artifact emitters
# ──────────────────────────────────────────────────────────────────────────────

def _artifact_record(kind: str, path: Path, extra: Optional[dict] = None) -> dict:
    rec = {
        "kind": "artifact",
        "artifact_kind": kind,
        "path": Path(path).as_posix(),
        "size": int(Path(path).stat().st_size) if Path(path).exists() else 0,
    }
    if extra:
        rec.update(extra)
    return rec


def emit_standard_artifacts(
    *,
    appender: ManifestAppender,
    out_bundle: Path,
    out_sums: Path,
    out_runspec: Optional[Path],
    out_guide: Optional[Path],
) -> int:
    """
    Emit artifact records for the standard pack outputs that exist:
      - design_manifest.jsonl
      - design_manifest.SHA256SUMS
      - superbundle.run.json (optional)
      - assistant_handoff.v1.json (optional)
    Returns the number of records emitted.
    """
    count = 0
    if Path(out_bundle).exists():
        appender.append_record(_artifact_record("manifest.jsonl", out_bundle))
        count += 1
    if Path(out_sums).exists():
        appender.append_record(_artifact_record("manifest.sha256sums", out_sums))
        count += 1
    if out_runspec and Path(out_runspec).exists():
        appender.append_record(_artifact_record("superbundle.run.json", out_runspec))
        count += 1
    if out_guide and Path(out_guide).exists():
        appender.append_record(_artifact_record("assistant_handoff.v1.json", out_guide))
        count += 1
    return count


def emit_transport_parts(
    *,
    appender: ManifestAppender,
    parts_dir: Path,
    part_stem: str,
    part_ext: str,
    parts_index_name: str,
) -> int:
    """
    Emit artifact records for split transport parts (e.g., design_manifest_0001.txt)
    and the parts index file if present. Returns the number of records emitted.
    """
    count = 0
    parts_dir = Path(parts_dir)
    if not parts_dir.exists():
        return 0

    # Parts (e.g., design_manifest_XX.txt)
    for p in sorted(parts_dir.glob(f"{part_stem}*{part_ext}")):
        if p.is_file():
            appender.append_record(_artifact_record("manifest.part", p))
            count += 1

    # Index
    idx = parts_dir / parts_index_name
    if idx.exists() and idx.is_file():
        appender.append_record(_artifact_record("manifest.parts_index", idx))
        count += 1

    return count


# ──────────────────────────────────────────────────────────────────────────────
# Path-rewrite & checksums
# ──────────────────────────────────────────────────────────────────────────────

def rewrite_manifest_paths(
    *,
    manifest_in: Path,
    manifest_out: Path,
    emitted_prefix: str,
    to_mode: str,  # "github" | "local"
) -> None:
    """
    Rewrite path-like fields in a manifest JSONL from one path mode to another.
    We touch:
      - records with "path" (kinds: file, python.module, quality.metric, artifact)
      - records with "src_path" (kind: graph.edge)
    """
    emitted_prefix = (emitted_prefix or "").strip("/")

    def to_local(p: str) -> str:
        p = (p or "").lstrip("/")
        if emitted_prefix and not p.startswith(emitted_prefix + "/"):
            return f"{emitted_prefix}/{p}"
        return p

    def to_github(p: str) -> str:
        p = (p or "").lstrip("/")
        if emitted_prefix and p.startswith(emitted_prefix + "/"):
            return p[len(emitted_prefix) + 1:]
        return p

    mapper = to_github if to_mode == "github" else to_local

    src_path = Path(manifest_in)
    dst_path = Path(manifest_out)
    dst_path.parent.mkdir(parents=True, exist_ok=True)

    out_lines: List[str] = []
    text = src_path.read_text(encoding="utf-8", errors="replace") if src_path.exists() else ""
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        try:
            obj = json.loads(line)
        except Exception:
            out_lines.append(line)
            continue

        kind = obj.get("kind")
        if "path" in obj and isinstance(obj["path"], str):
            obj["path"] = mapper(obj["path"])
            line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        if kind == "graph.edge" and isinstance(obj.get("src_path"), str):
            obj["src_path"] = mapper(obj["src_path"])
            line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        out_lines.append(line)

    dst_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")


def write_sha256sums_for_file(*, target_file: Path, out_sums_path: Path) -> None:
    """
    Compute sha256 of target_file and write a single-line SHA256SUMS file:
       <hex>  <basename>
    """
    target_file = Path(target_file)
    out_sums_path = Path(out_sums_path)
    if not target_file.exists():
        # Ensure sums file is absent if the target is missing.
        if out_sums_path.exists():
            out_sums_path.unlink(missing_ok=True)  # type: ignore[arg-type]
        return

    h = hashlib.sha256()
    with target_file.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    digest = h.hexdigest()
    line = f"{digest}  {target_file.name}\n"
    out_sums_path.parent.mkdir(parents=True, exist_ok=True)
    out_sums_path.write_text(line, encoding="utf-8")


__all__ = [
    "ManifestAppender",
    "emit_standard_artifacts",
    "emit_transport_parts",
    "rewrite_manifest_paths",
    "write_sha256sums_for_file",
]


