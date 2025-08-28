# File: v2/backend/core/utils/code_bundles/code_bundles/bundle_io.py
"""
Utilities for reading/writing the design manifest (JSONL) and related helpers.

This module augments the packager's output by:
- Ensuring a manifest header record exists (optionally front-inserting it).
- Appending arbitrary records (newline-delimited JSON).
- Emitting artifact records for standard outputs and transport parts.
- Rewriting record paths between local-snapshot and repo-root forms.
- Regenerating a simple SHA256SUMS file for a manifest file.
- In-memory variants for rewrites and sha256 over bytes (no disk writes).

It is careful to avoid logging sensitive data and writes atomically where needed.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

# Local contracts (builders for record dicts)
from v2.backend.core.utils.code_bundles.code_bundles.contracts import (
    build_artifact,
)


def _to_record_dict(obj: Any) -> Dict[str, Any]:
    """Return a plain dict suitable for JSON serialization."""
    if isinstance(obj, dict):
        return obj
    if is_dataclass(obj):
        return asdict(obj)
    raise TypeError(f"Unsupported record type: {type(obj)!r}")


def _json_dumps(rec: Dict[str, Any]) -> str:
    """Serialize a record to a compact JSON string with stable key order."""
    return json.dumps(rec, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class ManifestAppender:
    """
    Append-only JSONL writer with a helper to *front-insert* a header if missing.

    Usage:
        ma = ManifestAppender(path)
        ma.ensure_header(header_dict)  # creates or front-inserts if needed
        ma.append_record({...})
        ma.append_many([ {...}, {...} ])
    """

    def __init__(self, manifest_path: Path):
        self.manifest_path = Path(manifest_path)

    # ──────────────────────────────────────────────────────────────────────
    # Header handling
    # ──────────────────────────────────────────────────────────────────────

    def ensure_header(self, header_record: Dict[str, Any]) -> None:
        """
        Ensure the first line in the JSONL file is the given header (kind=manifest.header).
        If the file doesn't exist: create it with just the header line.
        If it exists and the first line is already a 'manifest.header': no-op.
        Otherwise: front-insert the header line (atomic rewrite).
        """
        header_line = _json_dumps(header_record) + "\n"

        if not self.manifest_path.exists():
            self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
            self.manifest_path.write_text(header_line, encoding="utf-8")
            return

        # Read the first line (if any)
        first_line: Optional[str] = None
        with self.manifest_path.open("r", encoding="utf-8", errors="replace") as f:
            first_line = f.readline()

        if first_line:
            try:
                parsed = json.loads(first_line)
                if isinstance(parsed, dict) and parsed.get("kind") == "manifest.header":
                    # Already has a header; nothing to do.
                    return
            except Exception:
                # Corrupt/garbled first line: we'll still front-insert a correct header.
                pass

        # Front-insert header by rewriting the file atomically
        tmp_dir = Path(tempfile.mkdtemp(prefix="manifest_insert_"))
        tmp_path = tmp_dir / (self.manifest_path.name + ".tmp")
        try:
            with self.manifest_path.open("rb") as src, tmp_path.open("wb") as dst:
                dst.write(header_line.encode("utf-8"))
                shutil.copyfileobj(src, dst)
            # Atomic replace
            os.replace(tmp_path, self.manifest_path)
        finally:
            try:
                if tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)  # type: ignore[arg-type]
            except Exception:
                pass
            try:
                tmp_dir.rmdir()
            except Exception:
                pass

    # ──────────────────────────────────────────────────────────────────────
    # Append helpers
    # ──────────────────────────────────────────────────────────────────────

    def append_record(self, record: Dict[str, Any]) -> None:
        """Append a single JSON record as one line."""
        rec = _to_record_dict(record)
        line = _json_dumps(rec) + "\n"
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with self.manifest_path.open("a", encoding="utf-8") as f:
            f.write(line)

    def append_many(self, records: Iterable[Dict[str, Any]]) -> int:
        """Append many JSON records; returns count written."""
        count = 0
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with self.manifest_path.open("a", encoding="utf-8") as f:
            for rec in records:
                line = _json_dumps(_to_record_dict(rec)) + "\n"
                f.write(line)
                count += 1
        return count


# ──────────────────────────────────────────────────────────────────────────────
# Artifact enumeration helpers
# ──────────────────────────────────────────────────────────────────────────────

def emit_standard_artifacts(
    *,
    appender: ManifestAppender,
    out_bundle: Path,
    out_sums: Path,
    out_runspec: Optional[Path],
    out_guide: Optional[Path],
) -> int:
    """
    Append 'artifact' records for the four standard outputs, if they exist.
    Returns the number of artifacts written.
    """
    n = 0
    if out_bundle and Path(out_bundle).exists():
        appender.append_record(build_artifact(
            name="design_manifest.jsonl",
            path=str(out_bundle),
            kind_hint="manifest",
        ))
        n += 1
    if out_sums and Path(out_sums).exists():
        appender.append_record(build_artifact(
            name="design_manifest.SHA256SUMS",
            path=str(out_sums),
            kind_hint="sums",
        ))
        n += 1
    if out_runspec and Path(out_runspec).exists():
        appender.append_record(build_artifact(
            name="superbundle.run.json",
            path=str(out_runspec),
            kind_hint="run_spec",
        ))
        n += 1
    if out_guide and Path(out_guide).exists():
        appender.append_record(build_artifact(
            name="assistant_handoff.v1.json",
            path=str(out_guide),
            kind_hint="guide",
        ))
        n += 1
    return n


def emit_transport_parts(
    *,
    appender: ManifestAppender,
    parts_dir: Path,
    part_stem: str,
    part_ext: str,
    parts_index_name: str,
) -> int:
    """
    Append 'artifact' records for transport split parts and index, if present.
    Returns the number of artifacts written.
    """
    n = 0
    parts_dir = Path(parts_dir or ".")
    if not parts_dir.exists():
        return 0

    for pf in sorted(parts_dir.glob(f"{part_stem}*{part_ext}")):
        appender.append_record(build_artifact(
            name=pf.name,
            path=str(pf),
            kind_hint="transport_part",
        ))
        n += 1

    idx = parts_dir / parts_index_name
    if idx.exists():
        appender.append_record(build_artifact(
            name=idx.name,
            path=str(idx),
            kind_hint="transport_index",
        ))
        n += 1

    return n


# ──────────────────────────────────────────────────────────────────────────────
# Path rewrite & sums helpers (disk + in-memory)
# ──────────────────────────────────────────────────────────────────────────────

def rewrite_manifest_paths(
    *,
    manifest_in: Path,
    manifest_out: Path,
    emitted_prefix: str,
    to_mode: str,  # "github" | "local"
) -> None:
    """
    Read JSONL from manifest_in, rewrite 'path' fields between local and github modes,
    and write to manifest_out atomically.

    Rewrites the following record fields:
      - kind=="file": rec["path"]
      - kind=="python.module": rec["path"]
      - kind=="quality.metric": rec["path"]
      - kind=="graph.edge": rec["src_path"]

    'local' mode ensures the path is prefixed with emitted_prefix (once).
    'github' mode strips the leading emitted_prefix/ if present.

    Artifact records are left unchanged.
    """
    emitted_prefix = emitted_prefix.strip("/")

    def to_local(p: str) -> str:
        p = p.lstrip("/")
        if emitted_prefix and not p.startswith(emitted_prefix + "/"):
            return f"{emitted_prefix}/{p}"
        return p

    def to_github(p: str) -> str:
        p = p.lstrip("/")
        if emitted_prefix and p.startswith(emitted_prefix + "/"):
            return p[len(emitted_prefix) + 1 :]
        return p

    mapper = to_github if to_mode == "github" else to_local

    tmp_dir = Path(tempfile.mkdtemp(prefix="manifest_rewrite_"))
    tmp_path = tmp_dir / (Path(manifest_out).name + ".tmp")
    try:
        with Path(manifest_in).open("r", encoding="utf-8") as src, tmp_path.open("w", encoding="utf-8") as dst:
            for line in src:
                try:
                    rec = json.loads(line)
                except Exception:
                    dst.write(line)
                    continue

                k = rec.get("kind")
                if k == "file":
                    if isinstance(rec.get("path"), str):
                        rec["path"] = mapper(rec["path"])
                        line = _json_dumps(rec) + "\n"
                elif k == "python.module":
                    if isinstance(rec.get("path"), str):
                        rec["path"] = mapper(rec["path"])
                        line = _json_dumps(rec) + "\n"
                elif k == "quality.metric":
                    if isinstance(rec.get("path"), str):
                        rec["path"] = mapper(rec["path"])
                        line = _json_dumps(rec) + "\n"
                elif k == "graph.edge":
                    if isinstance(rec.get("src_path"), str):
                        rec["src_path"] = mapper(rec["src_path"])
                        line = _json_dumps(rec) + "\n"

                dst.write(line)

        manifest_out.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp_path, manifest_out)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)  # type: ignore[arg-type]
        except Exception:
            pass
        try:
            tmp_dir.rmdir()
        except Exception:
            pass


def rewrite_manifest_paths_to_bytes(
    *,
    manifest_in: Path,
    emitted_prefix: str,
    to_mode: str,  # "github" | "local"
) -> bytes:
    """
    In-memory variant of rewrite_manifest_paths: returns the rewritten JSONL as bytes.
    """
    emitted_prefix = emitted_prefix.strip("/")

    def to_local(p: str) -> str:
        p = p.lstrip("/")
        if emitted_prefix and not p.startswith(emitted_prefix + "/"):
            return f"{emitted_prefix}/{p}"
        return p

    def to_github(p: str) -> str:
        p = p.lstrip("/")
        if emitted_prefix and p.startswith(emitted_prefix + "/"):
            return p[len(emitted_prefix) + 1 :]
        return p

    mapper = to_github if to_mode == "github" else to_local

    out_lines: list[str] = []
    with Path(manifest_in).open("r", encoding="utf-8") as src:
        for line in src:
            try:
                rec = json.loads(line)
            except Exception:
                out_lines.append(line.rstrip("\n"))
                continue

            k = rec.get("kind")
            if k == "file":
                if isinstance(rec.get("path"), str):
                    rec["path"] = mapper(rec["path"])
                    line = _json_dumps(rec)
            elif k == "python.module":
                if isinstance(rec.get("path"), str):
                    rec["path"] = mapper(rec["path"])
                    line = _json_dumps(rec)
            elif k == "quality.metric":
                if isinstance(rec.get("path"), str):
                    rec["path"] = mapper(rec["path"])
                    line = _json_dumps(rec)
            elif k == "graph.edge":
                if isinstance(rec.get("src_path"), str):
                    rec["src_path"] = mapper(rec["src_path"])
                    line = _json_dumps(rec)
            out_lines.append(line.rstrip("\n"))
    return ("\n".join(out_lines) + "\n").encode("utf-8")


def sha256_hex(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_hex_bytes(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


def write_sha256sums_for_file(*, target_file: Path, out_sums_path: Path) -> None:
    """
    Write a simple sha256sum line for the given file to out_sums_path:
      <hex>  <basename>
    """
    out_sums_path.parent.mkdir(parents=True, exist_ok=True)
    digest = sha256_hex(target_file)
    line = f"{digest}  {Path(target_file).name}\n"
    with Path(out_sums_path).open("w", encoding="utf-8") as f:
        f.write(line)

