from __future__ import annotations
import json
from pathlib import Path
from hashlib import sha256
from types import SimpleNamespace as NS
from typing import Dict, List, Any, Tuple


from v2.backend.core.utils.code_bundles.code_bundles.bundle_io import (
    ManifestAppender,
    emit_transport_parts,
    write_sha256sums_for_file,
)
from v2.backend.core.utils.code_bundles.code_bundles.execute.fs import (
    write_parts_from_jsonl,
    write_sha256sums_for_parts
)
from v2.backend.core.utils.code_bundles.code_bundles.execute.config import (
    read_code_bundle_params
)


class Transport(NS):
    pass
# ──────────────────────────────────────────────────────────────────────────────
# Transport chunking (manifest -> parts)
# ──────────────────────────────────────────────────────────────────────────────
def should_chunk(kind: str, size_bytes: int, split_bytes: int) -> bool:
    if kind == "always":
        return True
    if kind == "never":
        return False
    return size_bytes > max(1, int(split_bytes))

def append_parts_artifacts_into_manifest(
    *,
    manifest_path: Path,
    parts_dir: Path,
    part_stem: str,
    part_ext: str,
    parts_index_name: str,
) -> int:
    app = ManifestAppender(manifest_path)
    count = emit_transport_parts(
        appender=app,
        parts_dir=parts_dir,
        part_stem=part_stem,
        part_ext=part_ext,
        parts_index_name=parts_index_name,
    )
    return count


def maybe_chunk_manifest_and_update(
    *,
    cfg: NS,
    which: str,  # "local" | "github"
) -> Dict[str, Any]:
    params = read_code_bundle_params()
    mode = params.get("chunk_manifest", "auto")
    split_bytes = int(params.get("split_bytes", 300000) or 300000)
    group_dirs = bool(params.get("group_dirs", True))

    manifest_path = Path(cfg.out_bundle)
    parts_dir = manifest_path.parent
    part_stem = str(cfg.transport.part_stem)
    part_ext = str(cfg.transport.part_ext)
    index_name = str(cfg.transport.parts_index_name)

    report = {
        "kind": which,
        "decision": "skipped",
        "parts": 0,
        "bytes": int(manifest_path.stat().st_size) if manifest_path.exists() else 0,
        "split_bytes": split_bytes,
    }

    if not manifest_path.exists():
        print(f"[packager] chunk({which}): manifest missing; nothing to do")
        return report

    size = int(manifest_path.stat().st_size)
    if not should_chunk(mode, size, split_bytes):
        if Path(manifest_path).exists():
            write_sha256sums_for_file(target_file=manifest_path, out_sums_path=Path(cfg.out_sums))
            report["decision"] = "no-chunk"
            return report

    parts, index = write_parts_from_jsonl(
        src_manifest=manifest_path,
        dest_dir=parts_dir,
        part_stem=part_stem,
        part_ext=part_ext,
        split_bytes=split_bytes,
        group_dirs=group_dirs,
        dir_suffix_width=int(getattr(cfg.transport, "dir_suffix_width", 2)),
        parts_per_dir=int(getattr(cfg.transport, "parts_per_dir", 10)),
    )
    (parts_dir / index_name).write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    # --- CHECKSUMS unified into design_manifest*.SHA256SUMS ---
    try:
        sums_file = Path(cfg.out_sums)
        _ = write_sha256sums_for_parts(
            parts_dir=Path(cfg.out_bundle).parent,
            parts_index_name=str(cfg.transport.parts_index_name),
            part_stem=part_stem,
            part_ext=part_ext,
            out_sums_path=sums_file,
        )
        # Append monolith checksum line if present
        mono = Path(cfg.out_bundle).parent / f"{part_stem}.jsonl"
        if mono.exists():
            dg = sha256(mono.read_bytes()).hexdigest()
            with open(sums_file, "a", encoding="utf-8") as _f:
                _f.write(f"{dg}  {mono.name}\n")
    except Exception as e:
        print("[packager] WARN: checksums:", type(e).__name__, e)

    added = append_parts_artifacts_into_manifest(
        manifest_path=manifest_path,
        parts_dir=parts_dir,
        part_stem=part_stem,
        part_ext=part_ext,
        parts_index_name=index_name,
    )
    print(f"[packager] chunk({which}): wrote {len(parts)} parts; appended {added} artifact records")

    if not bool(getattr(cfg.transport, "preserve_monolith", False)):
        try:
            manifest_path.unlink(missing_ok=True)
        except TypeError:
            if manifest_path.exists():
                manifest_path.unlink()
        if Path(manifest_path).exists():
            write_sha256sums_for_file(target_file=manifest_path, out_sums_path=Path(cfg.out_sums))
    else:
        if Path(manifest_path).exists():
            write_sha256sums_for_file(target_file=manifest_path, out_sums_path=Path(cfg.out_sums))

    report.update({"decision": "chunked", "parts": len(parts)})
    return report