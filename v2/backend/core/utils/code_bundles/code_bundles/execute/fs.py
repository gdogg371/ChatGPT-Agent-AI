from __future__ import annotations
import re
import json
from pathlib import Path
from hashlib import sha256
from typing import Tuple, List, Dict, Any

def write_parts_from_jsonl(
    *,
    src_manifest: Path,
    dest_dir: Path,
    part_stem: str,
    part_ext: str,
    split_bytes: int,
    group_dirs: bool,
    dir_suffix_width: int,
    parts_per_dir: int,
) -> Tuple[List[Path], Dict[str, Any]]:
    dest_dir.mkdir(parents=True, exist_ok=True)

    if not src_manifest.exists():
        return [], {"record_type": "parts_index", "total_parts": 0, "split_bytes": split_bytes, "parts": []}

    text = src_manifest.read_text(encoding="utf-8", errors="replace")
    lines = [ln if ln.endswith("\n") else (ln + "\n") for ln in text.splitlines()]

    parts: List[Path] = []
    parts_meta: List[Dict[str, Any]] = []

    buf: List[str] = []
    buf_bytes = 0
    part_idx = 0

    def make_name(i: int) -> str:
        serial = f"{i+1:04d}"
        if group_dirs:
            group = (i // max(1, parts_per_dir))
            g = f"{group:0{dir_suffix_width}d}"
            return f"{part_stem}_{g}_{serial}{part_ext}"
        return f"{part_stem}_{serial}{part_ext}"

    def flush():
        nonlocal buf, buf_bytes, part_idx
        if not buf:
            return
        name = make_name(part_idx)
        p = dest_dir / name
        p.write_text("".join(buf), encoding="utf-8")
        parts.append(p)
        parts_meta.append({"name": p.name, "size": int(p.stat().st_size), "lines": len(buf)})
        part_idx += 1
        buf = []
        buf_bytes = 0

    for s in lines:
        s_len = len(s.encode("utf-8"))
        if buf and (buf_bytes + s_len) > split_bytes:
            flush()
        buf.append(s)
        buf_bytes += s_len
    flush()

    index = {
        "record_type": "parts_index",
        "total_parts": len(parts_meta),
        "split_bytes": int(split_bytes),
        "parts": parts_meta,
        "source": src_manifest.name,
    }
    return parts, index


def write_sha256sums_for_parts(*, parts_dir: Path, parts_index_name: str, part_stem: str, part_ext: str, out_sums_path: Path) -> int:
    """
    Write SHA256SUMS for chunked manifest parts and the parts index.
    Returns number of files hashed.
    """
    parts_dir = Path(parts_dir)
    out_sums_path = Path(out_sums_path)
    out_sums_path.parent.mkdir(parents=True, exist_ok=True)

    # Prefer the explicit parts index if present
    index_path = parts_dir / parts_index_name
    part_files: list[Path] = []
    if index_path.exists():
        try:
            data = json.loads(index_path.read_text(encoding="utf-8"))
            seq = data.get("parts") or data.get("files") or []
            for p in seq:
                if isinstance(p, str):
                    part_files.append(parts_dir / p)
                elif isinstance(p, dict):
                    name = p.get("path") or p.get("name")
                    if name:
                        part_files.append(parts_dir / str(name))
        except Exception:
            part_files = []

    # Fallback: discover by pattern
    if not part_files:
        pat = re.compile(rf"^{re.escape(part_stem)}_\d+_\d+{re.escape(part_ext)}$")
        part_files = [p for p in sorted(parts_dir.iterdir()) if p.is_file() and pat.match(p.name)]

    # Include the index file itself if present
    files_to_hash = [p for p in part_files if p.exists()]
    if index_path.exists():
        files_to_hash.insert(0, index_path)

    if not files_to_hash:
        return 0

    lines = []
    for fp in files_to_hash:
        digest = sha256(fp.read_bytes()).hexdigest()
        lines.append(f"{digest}  {fp.name}\n")

    out_sums_path.write_text("".join(lines), encoding="utf-8")
    return len(files_to_hash)