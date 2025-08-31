from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

# The SRC dir is already injected by run_pack; imports below assume we are on sys.path
from packager.core.loader import load_packager_config  # uses config/packager.yml
from packager.manifest.reader import ManifestReader
from packager.emitters.backfill import emit_analysis_sidecars
from packager.core.writer import write_json_atomic


def _pfx(msg: str) -> str:
    return f"[analysis] {msg}"


def _analysis_dir_from_cfg(cfg) -> Path:
    """
    Your run_pack publishes artifacts from <cfg.out_bundle>.parent,
    and expects sidecars under 'analysis/' inside that folder.
    """
    art_dir = Path(cfg.out_bundle).parent
    out = art_dir / "analysis"
    out.mkdir(parents=True, exist_ok=True)
    return out


def emit_all(*, repo_root: Path, cfg) -> Dict[str, Any]:
    """
    Entrypoint used by run_pack._load_analysis_emitter(...):
      emit_all(repo_root=<Path>, cfg=<NS from run_pack.build_cfg>)

    Returns an index dict for logging/debugging.
    """
    # Load full packager config (families, filenames, aliases, controls, emitters policy)
    conf = load_packager_config(repo_root)

    # Decide whether to emit at all:
    # - Prefer publish.analysis.enabled; fallback to root-level cfg.publish_analysis (legacy)
    publish_analysis_enabled = bool(conf.publish.get("analysis", {}).get("enabled", False))
    if not publish_analysis_enabled and not bool(getattr(cfg, "publish_analysis", False)):
        print(_pfx("disabled (both publish.analysis.enabled and root publish_analysis are false)"))
        return {"enabled": False, "families": {}}

    # Gate selection
    policy = conf.emitter_policy  # {"mode":"all"} | {"mode":"set","families":[...]}
    if policy.get("mode") == "all":
        gate = sorted(conf.canonical_families)  # all families where metadata_emission != "none"
    else:
        gate = sorted(set(policy.get("families", [])))

    if not gate:
        print(_pfx("no families selected by emitter policy; nothing to write"))
        return {"enabled": True, "families": {}, "note": "empty gate"}

    # Build manifest reader from the design_manifest parts your run already wrote
    manifest_dir = Path(cfg.out_bundle).parent
    parts_index = manifest_dir / str(conf.transport.get("parts_index", "design_manifest_parts_index.json"))
    if not parts_index.exists():
        # Try the canonical name from config even if transport dict omitted 'parts_index'
        parts_index = manifest_dir / "design_manifest_parts_index.json"

    reader = ManifestReader(
        repo_root=repo_root,
        manifest_dir=manifest_dir,
        parts_index=parts_index,
        transport=conf.transport,
        family_aliases=conf.family_aliases,
    )

    # Output dir is design_manifest/analysis
    out_dir = _analysis_dir_from_cfg(cfg)

    # Controls
    controls = conf.controls or {}
    strategy = str(controls.get("analysis_strategy", "backfill")).lower()
    synth_empty = bool(controls.get("synthesize_empty_summaries", True))
    forbid_raw_secrets = bool(controls.get("forbid_raw_secrets", True))

    print(_pfx(f"strategy={strategy} gate={len(gate)} out={out_dir}"))

    # Emit sidecars
    index = emit_analysis_sidecars(
        manifest_iter=reader.iter_rows(),
        gate=gate,
        filenames=conf.analysis_filenames,
        emission_modes=conf.metadata_emission,
        out_dir=out_dir,
        strategy=strategy,
        synth_empty=synth_empty,
        forbid_raw_secrets=forbid_raw_secrets,
    )

    # Write a compact _index.json for quick inspection
    write_json_atomic(out_dir / "_index.json", index)

    # Friendly per-family log
    fams = index.get("families", {})
    emitted = sum(1 for v in fams.values() if v.get("path"))
    total = len(fams)
    print(_pfx(f"wrote {emitted}/{total} families  "
               f"(nonzero: {sum(1 for v in fams.values() if v.get('count', 0) > 0)})"))

    return index

