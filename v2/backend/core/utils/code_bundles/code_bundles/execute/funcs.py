from __future__ import annotations
import os
import yaml
import fnmatch
import inspect
from pathlib import Path
from typing import Dict, Any, List, Tuple

from v2.backend.core.configuration.loader import (
    ConfigPaths,
)
import v2.backend.core.utils.code_bundles.code_bundles.src.packager.core.orchestrator as orch_mod


# ──────────────────────────────────────────────────────────────────────────────
# Discovery helpers
# ──────────────────────────────────────────────────────────────────────────────
def match_any(rel_posix: str, globs: List[str], case_insensitive: bool = False) -> bool:
    if not globs:
        return False
    rp = rel_posix.casefold() if case_insensitive else rel_posix
    for g in globs:
        pat = g.replace("\\", "/")
        pat = pat.casefold() if case_insensitive else pat
        if fnmatch.fnmatch(rp, pat):
            return True
    return False


def seg_excluded(parts: Tuple[str, ...], segment_excludes: List[str], case_insensitive: bool = False) -> bool:
    if not segment_excludes:
        return False
    segs = set((s.casefold() if case_insensitive else s) for s in segment_excludes)
    for seg in parts[:-1]:
        s = seg.casefold() if case_insensitive else seg
        if s in segs:
            return True
    return False

# ──────────────────────────────────────────────────────────────────────────────
# Local snapshot utilities
# ──────────────────────────────────────────────────────────────────────────────
def clear_dir_contents(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for p in sorted(root.rglob("*"), reverse=True):
        try:
            if p.is_file() or p.is_symlink():
                p.unlink()
            elif p.is_dir():
                try:
                    p.rmdir()
                except OSError:
                    pass
        except Exception:
            pass


def copy_snapshot(items: List[Tuple[Path, str]], dest_root: Path) -> int:
    count = 0
    for local, rel in items:
        dst = dest_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            dst.write_bytes(local.read_bytes())
            count += 1
        except Exception as e:
            print(f"[packager] WARN: copy failed {rel}: {type(e).__name__}: {e}")
    return count

# ──────────────────────────────────────────────────────────────────────────────
# Delta pruning (code + artifacts)
# ──────────────────────────────────────────────────────────────────────────────
def is_managed_path(
    rel_posix: str,
    include_globs: List[str],
    exclude_globs: List[str],
    segment_excludes: List[str],
    case_insensitive: bool,
) -> bool:
    parts = Path(rel_posix).parts
    if seg_excluded(parts, segment_excludes, case_insensitive):
        return False
    if include_globs and not match_any(rel_posix, include_globs, case_insensitive):
        return False
    if exclude_globs and match_any(rel_posix, exclude_globs, case_insensitive):
        return False
    return True

# ──────────────────────────────────────────────────────────────────────────────
# Manifest enrichment
# ──────────────────────────────────────────────────────────────────────────────
def tool_versions() -> Dict[str, Any]:
    try:
        orch_path = Path(inspect.getsourcefile(orch_mod) or "")
        return {"packager.orchestrator": orch_path.as_posix() if orch_path else "?", "run_pack": Path(__file__).as_posix()}
    except Exception:
        return {"run_pack": Path(__file__).as_posix()}


def map_record_paths_inplace(rec: Dict[str, Any], map_path_fn) -> None:
    # Standard path keys
    for key in ("path", "src_path", "dst_path", "caller_path", "callee_path"):
        if key in rec and isinstance(rec[key], str):
            rec[key] = map_path_fn(rec[key])
    # Examples array-of-paths
    examples = rec.get("examples")
    if isinstance(examples, dict):
        for k, v in list(examples.items()):
            if isinstance(v, list):
                examples[k] = [map_path_fn(x) if isinstance(x, str) else x for x in v]

def discover_repo_paths(
    *,
    src_root: Path,
    include_globs: List[str],
    exclude_globs: List[str],
    segment_excludes: List[str],
    case_insensitive: bool = False,
    follow_symlinks: bool = False,
) -> List[Tuple[Path, str]]:
    out: List[Tuple[Path, str]] = []
    for cur, dirs, files in os.walk(src_root, followlinks=follow_symlinks):
        pruned_dirs = []
        for d in dirs:
            try:
                parts = (Path(cur) / d).relative_to(src_root).parts
            except Exception:
                pruned_dirs.append(d)
                continue
            if seg_excluded(parts, segment_excludes, case_insensitive):
                continue
            pruned_dirs.append(d)
        dirs[:] = pruned_dirs

        for fn in sorted(files):
            p = Path(cur) / fn
            if not p.is_file():
                continue
            rel_posix = p.relative_to(src_root).as_posix()
            if include_globs and not match_any(rel_posix, include_globs, case_insensitive):
                continue
            if exclude_globs and match_any(rel_posix, exclude_globs, case_insensitive):
                continue
            out.append((p, rel_posix))
    out.sort(key=lambda t: t[1])
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Config readers (+ root-level flags)
# ──────────────────────────────────────────────────────────────────────────────
def read_root_publish_analysis() -> bool:
    """
    Read config/packager.yml directly to respect ROOT-LEVEL 'publish_analysis'.
    Do NOT infer from publish.*. Only return the root-level boolean.
    """
    try:
        paths = ConfigPaths.detect()
        cfg_path = paths.repo_root / "config" / "packager.yml"
        if not cfg_path.exists():
            return False
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        return bool(data.get("publish_analysis", False))
    except Exception as e:
        print(f"[packager] WARN: publish_analysis read failed: {type(e).__name__}: {e}")
        return False


def read_root_emit_ast() -> bool:
    """
    Read config/packager.yml directly to respect ROOT-LEVEL 'emit_ast'.
    This only controls whether we append AST records if produced by the indexer.
    """
    try:
        paths = ConfigPaths.detect()
        cfg_path = paths.repo_root / "config" / "packager.yml"
        if not cfg_path.exists():
            return False
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        return bool(data.get("emit_ast", False))
    except Exception as e:
        print(f"[packager] WARN: emit_ast read failed: {type(e).__name__}: {e}")
        return False