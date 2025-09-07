from __future__ import annotations
from pathlib import Path
from typing import List, Tuple, Dict, Any
import time

# Import shim: prefer current name, fall back if needed
try:
    from v2.backend.core.utils.code_bundles.code_bundles.src.packager.languages.base import (
        discover_language_plugins as _discover_plugins
    )
except Exception:
    from v2.backend.core.utils.code_bundles.code_bundles.src.packager.languages.base import (
        discover_plugins as _discover_plugins  # type: ignore
    )

from v2.backend.core.utils.code_bundles.code_bundles.src.packager.core.writer import ensure_dir, write_json_atomic

def run_plugins_and_write_artifacts(
    *,
    cfg,
    discovered_repo: List[Tuple[Path, str]],
) -> None:
    if not bool(getattr(getattr(cfg, "plugins", object()), "enabled", True)):
        print("[packager] Plugins: disabled (cfg.plugins.enabled is false)")
        return

    timeout_ms = int(getattr(getattr(cfg, "plugins", object()), "timeout_ms", 120_000))
    max_files  = int(getattr(getattr(cfg, "plugins", object()), "max_files", 10_000))
    max_bytes  = int(getattr(getattr(cfg, "plugins", object()), "max_bytes", 50_000_000))
    # (timeout_ms reserved for external runners; current in-proc Python plugins ignore it)

    art_root = Path(cfg.out_bundle).parent / "analysis"
    ensure_dir(art_root)

    rel_to_local: Dict[str, Path] = {rel: local for (local, rel) in discovered_repo}

    loaded = _discover_plugins() or []
    if not loaded:
        print("[packager] Plugins: none discovered")
        return

    print(f"[packager] Plugins: discovered {len(loaded)} plugin(s)")

    for lp in loaded:
        t0 = time.perf_counter()
        rels = [rel for rel in rel_to_local if any(rel.endswith(ext) for ext in (lp.extensions or []))]
        if not rels:
            print(f"[packager] Plugins: {lp.name} -> no matching files")
            continue

        files_payload: List[Tuple[str, bytes]] = []
        total_bytes = 0
        for rel in rels[:max_files]:
            local = rel_to_local[rel]
            try:
                b = Path(local).read_bytes()
            except Exception as e:
                print(f"[packager] Plugins: skip {rel} ({type(e).__name__}: {e})")
                continue
            total_bytes += len(b)
            if total_bytes > max_bytes:
                print(f"[packager] Plugins: {lp.name} -> reached max_bytes limit ({max_bytes}); truncating input set")
                break
            files_payload.append((rel, b))

        if not files_payload:
            print(f"[packager] Plugins: {lp.name} -> empty payload")
            continue

        try:
            artifacts: Dict[str, Any] = lp.analyze(files_payload) or {}
        except Exception as e:
            print(f"[packager] Plugins: {lp.name} analyze() failed: {type(e).__name__}: {e}")
            continue

        wrote = 0
        for rel_out, obj in artifacts.items():
            rel_out = str(rel_out).lstrip("/")
            if not rel_out.startswith(lp.name + "/"):
                rel_out = f"{lp.name}/{rel_out}"
            out_path = art_root / rel_out
            ensure_dir(out_path.parent)
            try:
                write_json_atomic(out_path, obj)
                wrote += 1
            except Exception as e:
                print(f"[packager] Plugins: failed write {rel_out}: {type(e).__name__}: {e}")

        dt = int((time.perf_counter() - t0) * 1000)
        print(f"[packager] Plugins: {lp.name} -> wrote {wrote} artifact(s) in {dt} ms (files_in={len(files_payload)})")
