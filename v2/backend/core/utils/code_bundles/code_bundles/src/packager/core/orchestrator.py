# File: output/patch_code_bundles/backend/core/utils/code_bundles/code_bundles/src/packager/core/orchestrator.py
# Always produce design_manifest.jsonl + design_manifest.SHA256SUMS in all run modes.
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import json

# ----- robust imports: absolute first, then relative fallback -----
try:
    from packager.core.paths import PathOps
    from packager.core.discovery import DiscoveryEngine, DiscoveryConfig
    from packager.io.manifest_writer import BundleWriter
    from packager.io.runspec_writer import RunSpecWriter
    from packager.io.guide_writer import GuideWriter
    from packager.languages.base import discover_language_plugins, LoadedPlugin
except ImportError:
    # allow running without -m or odd sys.path
    from .paths import PathOps
    from .discovery import DiscoveryEngine, DiscoveryConfig
    from ..io.manifest_writer import BundleWriter
    from ..io.runspec_writer import RunSpecWriter
    from ..io.guide_writer import GuideWriter
    from ..languages.base import discover_language_plugins, LoadedPlugin  # type: ignore

# Normalization rules (optional). If missing, we no-op normalize.
try:
    from .normalize import NormalizationRules, apply_normalization as _apply_normalization  # type: ignore
except Exception:  # pragma: no cover
    NormalizationRules = None  # type: ignore

    def _apply_normalization(records: List[Dict[str, Any]], rules=None) -> List[Dict[str, Any]]:  # type: ignore
        return records


def _log(msg: str) -> None:
    print(f"[packager] {msg}", flush=True)


def _read_bytes(p: Path) -> bytes:
    return p.read_bytes()


def _json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=None).encode("utf-8")


def _as_posix_rel(p: Path, root: Path) -> str:
    return PathOps.to_posix_rel(p, root)


def _emitted(rel_posix: str, emitted_prefix: str) -> str:
    return PathOps.emitted_path(rel_posix, emitted_prefix)


@dataclass
class PackagerResult:
    out_bundle: Path
    out_sums: Path
    out_runspec: Path
    out_guide: Path


class FileDiscovery:
    """Deterministic discovery with depth-aware segment excludes and globs."""

    def __init__(self, cfg, plugin_exts: Tuple[str, ...]) -> None:
        self.cfg = cfg
        self.plugin_exts = plugin_exts

    def _effective_case_insensitive(self) -> bool:
        return bool(getattr(self.cfg, "case_insensitive", False))

    def _effective_globs(self, kind: str) -> Tuple[str, ...]:
        """
        Ensure plugin extensions are included if include_globs not specified.
        kind: 'include' | 'exclude'
        """
        globs = tuple(getattr(self.cfg, f"{kind}_globs", ()) or ())
        if not globs and self.plugin_exts:
            return tuple(sorted({f"**/*{ext}" for ext in self.plugin_exts}))
        return globs

    def discover(self) -> List[Path]:
        root = Path(self.cfg.source_root).resolve()
        eng = DiscoveryEngine(
            DiscoveryConfig(
                root=root,
                segment_excludes=tuple(getattr(self.cfg, "segment_excludes", ()) or ()),
                include_globs=self._effective_globs("include"),
                exclude_globs=self._effective_globs("exclude"),
                case_insensitive=self._effective_case_insensitive(),
                follow_symlinks=bool(getattr(self.cfg, "follow_symlinks", False)),
            )
        )
        paths = eng.discover()
        _log(f"Discover: {len(paths)} files under '{root}'")
        return paths


class Packager:
    """
    Build the design manifest + sums, always (independent of publish mode).
    Optionally writes a run-spec and a guide. Language plugins may add analysis artifacts.
    """

    def __init__(self, cfg, rules=None) -> None:
        self.cfg = cfg
        self.rules = rules  # optional NormalizationRules

    def _load_plugins(self) -> List[LoadedPlugin]:
        plugs = discover_language_plugins()
        if not plugs:
            _log("Plugins: none discovered")
        else:
            _log(f"Plugins: discovered {[p.name for p in plugs]}")
        return plugs

    def _collect_sources(self, files: List[Path]) -> List[Tuple[str, bytes]]:
        """
        Returns list of (rel_posix, data) for codebase files relative to cfg.source_root.
        """
        root = Path(self.cfg.source_root).resolve()
        out: List[Tuple[str, bytes]] = []
        for p in files:
            if not p.is_file():
                continue
            rel_posix = _as_posix_rel(p, root)
            try:
                data = _read_bytes(p)
            except Exception:
                # Skip unreadable files; do not fail manifest production
                continue
            out.append((rel_posix, data))
        return out

    def _plugin_artifacts(
        self, plugins: List[LoadedPlugin], src_files: List[Tuple[str, bytes]]
    ) -> List[Tuple[str, bytes]]:
        """
        Run language plugins; return additional (rel_posix, data) items to include.
        We serialize non-bytes outputs as JSON bytes.
        """
        artifacts: List[Tuple[str, bytes]] = []
        if not plugins or not src_files:
            return artifacts

        for plug in plugins:
            try:
                result = plug.analyze(src_files)  # expected dict[str, Any] or dict[str, bytes]
            except Exception as e:
                _log(f"Plugin '{plug.name}': analyze failed: {type(e).__name__}: {e}")
                continue

            if isinstance(result, dict):
                for rel, val in result.items():
                    if isinstance(val, (bytes, bytearray)):
                        data = bytes(val)
                    else:
                        data = _json_bytes(val)
                    # Ensure analysis lives under 'analysis/' for neatness
                    rel_norm = rel.lstrip("/")
                    if not rel_norm.startswith("analysis/"):
                        rel_norm = f"analysis/{rel_norm}"
                    artifacts.append((rel_norm, data))
        _log(f"Plugins: produced {len(artifacts)} artifacts")
        return artifacts

    def _build_records(
        self, emitted: List[Tuple[str, bytes]]
    ) -> Tuple[List[Dict[str, Any]], List[Tuple[str, bytes]]]:
        """
        Turn (rel_posix, data) pairs into manifest records and sums input.
        Records schema is intentionally simple: {kind, path, sha256}.
        """
        records: List[Dict[str, Any]] = []
        sums_in: List[Tuple[str, bytes]] = []

        for rel_posix, data in emitted:
            path = _emitted(rel_posix, self.cfg.emitted_prefix)
            rec = {"kind": "file", "path": path}
            # We compute SHA256 here so both records and SHA256SUMS agree.
            try:
                from packager.core.integrity import Integrity  # type: ignore
            except Exception:
                from .integrity import Integrity  # type: ignore
            rec["sha256"] = Integrity.sha256_bytes(data)

            records.append(rec)
            sums_in.append((path, data))
        return records, sums_in

    def _maybe_normalize(self, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        rules = self.rules
        try:
            return _apply_normalization(records, rules) if rules else records
        except Exception as e:  # non-fatal
            _log(f"Normalize: skipped ({type(e).__name__}: {e})")
            return records

    def run(self, external_source: Optional[Path] = None) -> PackagerResult:
        """
        Always writes:
          - cfg.out_bundle (JSONL design manifest)
          - cfg.out_sums (SHA256SUMS)
        Also writes:
          - cfg.out_runspec (run snapshot JSON)
          - cfg.out_guide (assistant handoff JSON)
        """
        cfg = self.cfg

        # 1) plugins
        plugins = self._load_plugins()
        plugin_exts: Tuple[str, ...] = tuple(sorted({ext for p in plugins for ext in getattr(p, "extensions", ())}))

        # 2) Direct-source mode: no external ingestion (external_source is ignored here)
        #    We deliberately scan cfg.source_root directly.

        # 3) discover files under source_root
        files = FileDiscovery(cfg, plugin_exts).discover()

        # 4) read contents (robust)
        src_pairs = self._collect_sources(files)

        # 5) plugin artifacts
        extras = self._plugin_artifacts(plugins, src_pairs)

        # 6) combine
        combined: List[Tuple[str, bytes]] = src_pairs + extras

        # 7) build manifest records (+ sums input)
        records, sums_in = self._build_records(combined)

        # 8) optional normalization (non-fatal)
        records = self._maybe_normalize(records)

        # 9) write manifest ALWAYS
        writer = BundleWriter(Path(cfg.out_bundle))
        writer.write(records)

        # 10) write sums ALWAYS
        writer.write_sums(Path(cfg.out_sums), sums_in)

        # 11) write runspec + guide (do not gate manifest/sums)
        rsw = RunSpecWriter(Path(cfg.out_runspec))
        runspec = rsw.build_snapshot(cfg, {"source_root": str(cfg.source_root), "emitted_prefix": str(cfg.emitted_prefix)})
        rsw.write(runspec)

        gw = GuideWriter(Path(cfg.out_guide))
        gw.write(cfg, {"purpose": "assistant-handoff"}, {})

        _log(f"Wrote: {cfg.out_bundle.name}, {cfg.out_sums.name}, {cfg.out_runspec.name}, {cfg.out_guide.name}")
        return PackagerResult(out_bundle=Path(cfg.out_bundle), out_sums=Path(cfg.out_sums),
                              out_runspec=Path(cfg.out_runspec), out_guide=Path(cfg.out_guide))

