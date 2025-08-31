from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from packager.core.loader import load_packager_config  # uses config/packager.yml
from packager.manifest.reader import ManifestReader
from packager.emitters.backfill import emit_analysis_sidecars
from packager.core.writer import write_json_atomic


def _pfx(msg: str) -> str:
    return f"[analysis] {msg}"


def _analysis_dir_from_cfg(cfg) -> Path:
    """
    Sidecars live under the same design_manifest/ folder your run_pack writes to.
    """
    art_dir = Path(cfg.out_bundle).parent
    out = art_dir / "analysis"
    out.mkdir(parents=True, exist_ok=True)
    return out


def emit_all(*, repo_root: Path, cfg) -> Dict[str, Any]:
    """
    Entrypoint used by run_pack._load_analysis_emitter(...):
      emit_all(repo_root=<Path>, cfg=<NS from run_pack.build_cfg>)
    """
    conf = load_packager_config(repo_root)

    # Respect publish.analysis.enabled, fallback to legacy root flag
    publish_analysis_enabled = bool(conf.publish.get("analysis", {}).get("enabled", False))
    if not publish_analysis_enabled and not bool(getattr(cfg, "publish_analysis", False)):
        print(_pfx("disabled (publish.analysis.enabled=false and root publish_analysis=false)"))
        return {"enabled": False, "families": {}}

    # Gate selection
    policy = conf.emitter_policy  # {"mode":"all"} | {"mode":"set","families":[...]}
    if policy.get("mode") == "all":
        gate = sorted(conf.canonical_families)  # metadata_emission != "none"
    else:
        gate = sorted(set(policy.get("families", [])))

    if not gate:
        print(_pfx("no families selected by emitter policy; nothing to write"))
        return {"enabled": True, "families": {}, "note": "empty gate"}

    # Build manifest reader from the design_manifest parts
    manifest_dir = Path(cfg.out_bundle).parent
    parts_index_name = str(conf.transport.get("parts_index_name", "design_manifest_parts_index.json"))
    parts_index = manifest_dir / parts_index_name

    reader = ManifestReader(
        repo_root=repo_root,
        manifest_dir=manifest_dir,
        parts_index=parts_index,
        transport=conf.transport,
        family_aliases=conf.family_aliases,
    )

    out_dir = _analysis_dir_from_cfg(cfg)

    # Controls
    controls = conf.controls or {}
    strategy = str(controls.get("analysis_strategy", "backfill")).lower()
    synth_empty = bool(controls.get("synthesize_empty_summaries", True))
    forbid_raw_secrets = bool(controls.get("forbid_raw_secrets", True))

    print(_pfx(f"strategy={strategy} gate={len(gate)} out={out_dir}"))

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

    write_json_atomic(out_dir / "_index.json", index)

    fams = index.get("families", {})
    emitted = sum(1 for v in fams.values() if v.get("path"))
    total = len(fams)
    print(_pfx(f"wrote {emitted}/{total} families  "
               f"(nonzero: {sum(1 for v in fams.values() if v.get('count', 0) > 0)})"))

    return index


