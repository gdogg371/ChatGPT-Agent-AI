# v2/backend/core/utils/code_bundles/code_bundles/bundle_io.py

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Dict, Any, List, Tuple, Optional
import hashlib
import json
import os
import re

# --------------------------------------------------------------------------------------
# Manifest appender
# --------------------------------------------------------------------------------------

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

        # Find the first non-empty line
        first_idx = None
        for i, ln in enumerate(lines):
            if ln.strip():
                first_idx = i
                break

        def _is_header(s: str) -> bool:
            try:
                obj = json.loads(s)
                return isinstance(obj, dict) and obj.get("kind") == "manifest.header"
            except Exception:
                return False

        # If no content, just write the header as first line
        if first_idx is None:
            self._write_lines([json.dumps(header_record, ensure_ascii=False, sort_keys=True)])
            return

        # If the first non-empty line is already a header, keep as-is
        if _is_header(lines[first_idx]):
            return

        # Otherwise, insert header before the first non-empty line
        new_lines = []
        # Keep any leading empty lines to preserve formatting
        new_lines.extend(lines[:first_idx])
        new_lines.append(json.dumps(header_record, ensure_ascii=False, sort_keys=True))
        new_lines.extend(lines[first_idx:])
        self._write_lines(new_lines)

    def append_record(self, record: dict) -> None:
        """
        Append a single JSON object as one line to the manifest.
        """
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        with self.manifest_path.open("ab") as f:
            f.write(line.encode("utf-8"))

    def append_many(self, records: Iterable[Dict[str, Any]]) -> int:
        """
        Append many JSON objects; returns count appended.
        """
        n = 0
        with self.manifest_path.open("ab") as f:
            for rec in records:
                line = json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n"
                f.write(line.encode("utf-8"))
                n += 1
        return n


# --------------------------------------------------------------------------------------
# Artifact writers/helpers used by run_pack.py
# --------------------------------------------------------------------------------------

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
    out_runspec: Optional[Path] = None,
    out_guide: Optional[Path] = None,
) -> int:
    """
    Append artifact records for the standard outputs that may have been written earlier:
      - design_manifest.jsonl
      - design_manifest.SHA256SUMS
      - superbundle.run.json (optional)
      - assistant_handoff.v1.json (optional)

    Returns:
        int: number of artifact records appended.
    """
    count = 0

    if out_bundle and Path(out_bundle).exists():
        appender.append_record(_artifact_record("manifest.bundle", Path(out_bundle)))
        count += 1

    if out_sums and Path(out_sums).exists():
        appender.append_record(_artifact_record("manifest.sums", Path(out_sums)))
        count += 1

    if out_runspec:
        p = Path(out_runspec)
        if p.exists():
            appender.append_record(_artifact_record("run.spec", p))
            count += 1

    if out_guide:
        p = Path(out_guide)
        if p.exists():
            appender.append_record(_artifact_record("guide.handoff", p))
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

    # Parts (e.g., design_manifest_XX_YYYY.txt)
    part_pat = re.compile(rf"^{re.escape(part_stem)}_\d+_\d+{re.escape(part_ext)}$")
    for p in sorted(parts_dir.iterdir()):
        if p.is_file() and part_pat.match(p.name):
            appender.append_record(_artifact_record("manifest.part", p))
            count += 1

    # Index
    idx = parts_dir / parts_index_name
    if idx.exists() and idx.is_file():
        appender.append_record(_artifact_record("manifest.parts_index", idx))
        count += 1

    return count


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

    def _map(rel: str) -> str:
        rel = rel.strip().lstrip("/")
        if to_mode == "github":
            # GitHub mode wants repo-relative paths (no emitted_prefix)
            return rel
        # Local mode: prefix with emitted_prefix
        return f"{emitted_prefix}/{rel}" if emitted_prefix else rel

    manifest_in = Path(manifest_in)
    manifest_out = Path(manifest_out)
    manifest_out.parent.mkdir(parents=True, exist_ok=True)

    with manifest_in.open("r", encoding="utf-8", errors="replace") as fin, \
         manifest_out.open("w", encoding="utf-8") as fout:
        for line in fin:
            s = line.strip()
            if not s:
                fout.write("\n")
                continue
            try:
                obj = json.loads(s)
            except Exception:
                fout.write(line)
                continue

            if isinstance(obj, dict):
                # Common path fields
                if "path" in obj and isinstance(obj["path"], str):
                    obj["path"] = _map(obj["path"])
                if "src_path" in obj and isinstance(obj["src_path"], str):
                    obj["src_path"] = _map(obj["src_path"])
                fout.write(json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n")
            else:
                fout.write(line)


# --------------------------------------------------------------------------------------
# Legacy single-file checksum (kept for backwards-compat)
# --------------------------------------------------------------------------------------

def write_sha256sums_for_file(target_file: Path, out_sums_path: Path) -> None:
    """
    Compute sha256 of `target_file` (raw bytes) and write a single-line SHA256SUMS file:

        <sha256(hex)>␠␠<target_file.name>

    Behavior:
        - When environment variable PACKAGER_DISABLE_LEGACY_SUMS == "1",
          this function is a NO-OP (the unified emitter produces the canonical sums).
        - If the target file does not exist (e.g., preserve_monolith=false), NO-OP.
    """
    if os.getenv("PACKAGER_DISABLE_LEGACY_SUMS") == "1":
        return

    p = Path(target_file)
    if not p.exists():
        return  # target missing is a valid scenario with chunk-only transport

    data = p.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    line = f"{digest}  {p.name}\n"
    out_sums_path = Path(out_sums_path)
    out_sums_path.parent.mkdir(parents=True, exist_ok=True)
    out_sums_path.write_text(line, encoding="utf-8")


__all__ = [
    "ManifestAppender",
    "emit_standard_artifacts",
    "emit_transport_parts",
    "rewrite_manifest_paths",
    "write_sha256sums_for_file",
]




