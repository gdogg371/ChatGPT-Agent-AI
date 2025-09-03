# v2/backend/core/utils/code_bundles/code_bundles/execute/parts.py
from __future__ import annotations
import io, json, os, tempfile
from hashlib import sha256
from pathlib import Path
from typing import List, Optional, Tuple
from types import SimpleNamespace as NS

__all__ = [
    "_write_parts_from_jsonl",
    "_append_parts_artifacts_into_manifest",
    "_write_sha256sums_for_parts",
    "_maybe_chunk_manifest_and_update",
]

# --- atomic I/O helpers ------------------------------------------------------

def _write_atomic(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        tmp.write(data)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_name = Path(tmp.name)
    tmp_name.replace(path)

def _iter_lines_binary(fp: io.BufferedReader):
    # Yields (line_bytes_without_trailing_newline, newline_bytes)
    while True:
        line = fp.readline()
        if not line:
            break
        if line.endswith(b"\n"):
            yield line[:-1], b"\n"
        else:
            yield line, b""

# --- naming helpers ----------------------------------------------------------

def _part_name(part_stem: str, dir_idx: int, file_idx: int, part_ext: str) -> str:
    return f"{part_stem}_{dir_idx:02d}_{file_idx:04d}{part_ext}"

def _subdir_for(parts_dir: Path, dir_idx: int, parts_per_dir: int) -> Path:
    # Keep flat when parts_per_dir == 0
    if parts_per_dir <= 0:
        return parts_dir
    return parts_dir / f"{dir_idx:02d}"

def _index_filename(part_stem: str) -> str:
    # REQUIRED: underscore style, e.g. design_manifest_parts_index.json
    return f"{part_stem}_parts_index.json"

def _sums_filename(part_stem: str) -> str:
    # REQUIRED: top-level sums file name used elsewhere
    return f"{part_stem}.SHA256SUMS"

# --- main: split JSONL into parts -------------------------------------------

def _write_parts_from_jsonl(
    jsonl_path: Path,
    parts_dir: Path,
    part_stem: str,
    part_ext: str,
    split_bytes: int,
    parts_per_dir: int,
    preserve_monolith: bool,  # kept for signature parity; not used here
) -> List[Path]:
    parts_dir = Path(parts_dir)
    parts_dir.mkdir(parents=True, exist_ok=True)

    total_bytes = 0
    total_lines = 0
    part_paths: List[Path] = []

    dir_idx = 0
    file_idx = 0
    cur_bytes = 0
    cur_lines = 0
    cur_buf = io.BytesIO()
    cur_files_in_dir = 0

    def _flush_part():
        nonlocal file_idx, dir_idx, cur_bytes, cur_lines, cur_buf, cur_files_in_dir
        if cur_lines == 0:
            return None
        subdir = _subdir_for(parts_dir, dir_idx, parts_per_dir)
        subdir.mkdir(parents=True, exist_ok=True)
        name = _part_name(part_stem, dir_idx, file_idx + 1, part_ext)
        out_path = subdir / name
        _write_atomic(out_path, cur_buf.getvalue())
        part_paths.append(out_path)
        # reset counters
        file_idx += 1
        cur_files_in_dir += 1
        if parts_per_dir > 0 and cur_files_in_dir >= parts_per_dir:
            dir_idx += 1
            cur_files_in_dir = 0
        cur_bytes = 0
        cur_lines = 0
        cur_buf = io.BytesIO()
        return out_path

    with open(jsonl_path, "rb") as f:
        for body, nl in _iter_lines_binary(f):
            line = body + nl  # keep newline as in source
            line_len = len(line)

            # If adding this line would exceed the budget, rotate before writing it
            if cur_lines > 0 and (cur_bytes + line_len) > split_bytes:
                _flush_part()

            cur_buf.write(line)
            cur_bytes += line_len
            cur_lines += 1

            total_lines += 1
            total_bytes += line_len

        # flush final part
        _flush_part()

    # Write parts index JSON (underscore filename)
    index = {
        "format": "jsonl.parts.v1",
        "part_stem": part_stem,
        "part_ext": part_ext,
        "split_bytes": split_bytes,
        "parts_per_dir": parts_per_dir,
        "total_lines": total_lines,
        "total_bytes": total_bytes,
        "parts": [str(p.relative_to(parts_dir).as_posix()) for p in part_paths],
    }
    index_path = parts_dir / _index_filename(part_stem)
    _write_atomic(index_path, json.dumps(index, indent=2).encode("utf-8"))
    return part_paths

# --- checksums ---------------------------------------------------------------

def _write_sha256sums_for_parts(parts_dir: Path, part_stem: str, part_ext: str, sums_path: Path) -> None:
    """
    Write SHA256 sums for the parts index and all part files under parts_dir into sums_path.
    """
    parts_dir = Path(parts_dir)
    sums_path = Path(sums_path)
    lines: List[str] = []

    idx = parts_dir / _index_filename(part_stem)
    if idx.exists():
        dg = sha256(idx.read_bytes()).hexdigest()
        lines.append(f"{dg}  {idx.name}\n")

    # include parts
    for p in sorted(parts_dir.rglob(f"{part_stem}_*_*{part_ext}")):
        if p.is_file():
            dg = sha256(p.read_bytes()).hexdigest()
            lines.append(f"{dg}  {p.name}\n")

    if lines:
        _write_atomic(sums_path, "".join(lines).encode("utf-8"))

# --- manifest augmentation helpers ------------------------------------------

def _append_parts_artifacts_into_manifest(
    manifest: dict,
    parts_written: List[Path],
    parts_dir: Path,
    sums_file: Optional[Path],
    part_stem: str = "design_manifest",
) -> dict:
    """
    Inject transport artifact references into the manifest dict.
    """
    manifest = dict(manifest or {})
    manifest.setdefault("artifacts", {})
    manifest["artifacts"].setdefault("transport", {})

    manifest["artifacts"]["transport"]["parts"] = [
        str(p.relative_to(parts_dir).as_posix()) for p in parts_written
    ]
    manifest["artifacts"]["transport"]["parts_index"] = _index_filename(part_stem)
    if sums_file:
        manifest["artifacts"]["transport"]["sha256sums"] = Path(sums_file).name
    return manifest

# --- orchestration convenience ----------------------------------------------

def _maybe_chunk_manifest_and_update(
    cfg: NS,
    jsonl_path: Path,
    parts_dir: Path,
) -> Tuple[List[Path], Optional[Path]]:
    """
    Split the JSONL at jsonl_path into parts under parts_dir according to cfg.config['transport'].
    Also writes SHA256SUMS beside the parts. Returns (parts_written, sums_path).
    """
    transport_cfg = (getattr(cfg, "config", {}) or {}).get("transport", {}) or {}
    part_stem = str(transport_cfg.get("part_stem", "design_manifest"))
    part_ext = str(transport_cfg.get("part_ext", ".txt"))
    parts_per_dir = int(transport_cfg.get("parts_per_dir", 10))
    split_bytes = int(transport_cfg.get("split_bytes", 150000))
    preserve_monolith = bool(transport_cfg.get("preserve_monolith", False))

    parts_written: List[Path] = []
    sums_path: Optional[Path] = None

    jsonl_path = Path(jsonl_path)
    parts_dir = Path(parts_dir)
    if jsonl_path.exists():
        parts_written = _write_parts_from_jsonl(
            jsonl_path=jsonl_path,
            parts_dir=parts_dir,
            part_stem=part_stem,
            part_ext=part_ext,
            split_bytes=split_bytes,
            parts_per_dir=parts_per_dir,
            preserve_monolith=preserve_monolith,
        )
        sums_path = parts_dir / _sums_filename(part_stem)
        _write_sha256sums_for_parts(
            parts_dir=parts_dir, part_stem=part_stem, part_ext=part_ext, sums_path=sums_path
        )

    return parts_written, sums_path


