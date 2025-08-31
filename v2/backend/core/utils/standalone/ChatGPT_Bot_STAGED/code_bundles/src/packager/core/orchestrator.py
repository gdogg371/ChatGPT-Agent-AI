# File: v2/backend/core/utils/code_bundles/code_bundles/src/packager/core/orchestrator.py
# Orchestrator: discover code, run language plugins, build manifest + sums.
# If cfg.publish.publish_analysis == True, also write plugin artifacts to
# output/design_manifest/analysis/** (to be picked up by the publisher).

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
import json

# ----- imports within embedded packager tree (robust absolute + fallback) -----
try:
    from packager.core.paths import PathOps
    from packager.core.discovery import DiscoveryEngine, DiscoveryConfig
    from packager.core.integrity import Integrity
    from packager.io.manifest_writer import BundleWriter
    from packager.io.runspec_writer import RunSpecWriter
    from packager.io.guide_writer import GuideWriter
    from packager.languages.base import discover_language_plugins, LoadedPlugin
except ImportError:  # fallback if sys.path differs
    from .paths import PathOps  # type: ignore
    from .discovery import DiscoveryEngine, DiscoveryConfig  # type: ignore
    from .integrity import Integrity  # type: ignore
    from ..io.manifest_writer import BundleWriter  # type: ignore
    from ..io.runspec_writer import RunSpecWriter  # type: ignore
    from ..io.guide_writer import GuideWriter  # type: ignore
    from ..languages.base import discover_language_plugins, LoadedPlugin  # type: ignore

# Optional normalization rules (no-op if module not present)
try:
    from .normalize import NormalizationRules, apply_normalization as _apply_normalization  # type: ignore
except Exception:  # pragma: no cover
    NormalizationRules = None  # type: ignore

    def _apply_normalization(records: List[Dict[str, Any]], rules=None) -> List[Dict[str, Any]]:  # type: ignore
        return records


def _log(msg: str) -> None:
    print(f"[packager] {msg}", flush=True)


def _json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


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
    """Deterministic discovery honoring include/exclude globs and segment excludes."""

    def __init__(self, cfg, plugin_exts: Tuple[str, ...]) -> None:
        self.cfg = cfg
        self.plugin_exts = plugin_exts

    def _effective_globs(self, kind: str) -> Tuple[str, ...]:
        globs = tuple(getattr(self.cfg, f"{kind}_globs", ()) or ())
        if not globs and self.plugin_exts:
            # If includes not provided, allow all plugin extensions by default
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
                case_insensitive=bool(getattr(self.cfg, "case_insensitive", False)),
                follow_symlinks=bool(getattr(self.cfg, "follow_symlinks", False)),
            )
        )
        paths = eng.discover()
        _log(f"Discover: {len(paths)} files under '{root}'")
        return paths


class Packager:
    """
    Build the design manifest + SHA256 sums, always.
    Run language plugins; if configured, persist their artifacts for publishing.
    """

    def __init__(self, cfg, rules=None) -> None:
        self.cfg = cfg
        self.rules = rules

    def _load_plugins(self) -> List[LoadedPlugin]:
        plugs = discover_language_plugins()
        if not plugs:
            _log("Plugins: none discovered")
        else:
            _log(f"Plugins: discovered {[p.name for p in plugs]}")
        return plugs

    def _collect_sources(self, files: List[Path]) -> List[Tuple[str, bytes]]:
        root = Path(self.cfg.source_root).resolve()
        out: List[Tuple[str, bytes]] = []
        for p in files:
            try:
                if not p.is_file():
                    continue
                rel_posix = _as_posix_rel(p, root)
                data = p.read_bytes()
                out.append((rel_posix, data))
            except Exception as e:
                _log(f"Read skip: {p} ({type(e).__name__}: {e})")
        return out

    def _plugin_artifacts(
        self,
        plugins: List[LoadedPlugin],
        src_files: List[Tuple[str, bytes]],
    ) -> List[Tuple[str, bytes]]:
        """
        Return list of (rel_posix, data) artifacts. rel_posix should live under analysis/.
        """
        artifacts: List[Tuple[str, bytes]] = []
        if not plugins or not src_files:
            return artifacts

        for plug in plugins:
            try:
                result = plug.analyze(src_files)  # Dict[str, Any] | Dict[str, bytes]
            except Exception as e:
                _log(f"Plugin '{plug.name}': analyze failed: {type(e).__name__}: {e}")
                continue
            if not isinstance(result, dict):
                continue
            for rel, val in result.items():
                rel_norm = str(rel).lstrip("/")
                if not rel_norm.startswith("analysis/"):
                    rel_norm = f"analysis/{rel_norm}"
                data = val if isinstance(val, (bytes, bytearray)) else _json_bytes(val)
                artifacts.append((rel_norm, bytes(data)))
        _log(f"Plugins: produced {len(artifacts)} artifacts")
        return artifacts

    @staticmethod
    def _pub_flag(cfg, name: str) -> bool:
        pub = getattr(cfg, "publish", None)
        if isinstance(pub, dict):
            return bool(pub.get(name))
        return bool(getattr(pub, name, False))

    def _write_analysis_files(self, extras: List[Tuple[str, bytes]]) -> None:
        """
        Persist plugin artifacts under output/design_manifest/analysis/** when enabled.
        Reads only cfg.publish.publish_analysis (which is set from the root-level flag by the runner).
        """
        if not extras:
            return
        if not self._pub_flag(self.cfg, "publish_analysis"):
            return

        dest_root = Path(self.cfg.out_bundle).parent / "analysis"
        wrote = 0
        for rel, data in extras:
            # rel is "analysis/..." — write under dest_root preserving trailing path
            trailing = rel.split("/", 1)[1] if rel.startswith("analysis/") else rel
            out_path = dest_root / trailing
            try:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(data)
                wrote += 1
            except Exception as e:
                _log(f"analysis: write failed {out_path} ({type(e).__name__}: {e})")
        _log(f"analysis: wrote {wrote} files to '{dest_root}'")

    def _build_records(
        self, emitted: List[Tuple[str, bytes]]
    ) -> Tuple[List[Dict[str, Any]], List[Tuple[str, bytes]]]:
        records: List[Dict[str, Any]] = []
        sums_in: List[Tuple[str, bytes]] = []
        for rel_posix, data in emitted:
            path = _emitted(rel_posix, self.cfg.emitted_prefix)
            rec = {"kind": "file", "path": path, "sha256": Integrity.sha256_bytes(data)}
            records.append(rec)
            sums_in.append((path, data))
        return records, sums_in

    def _maybe_normalize(self, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        try:
            return _apply_normalization(records, self.rules) if self.rules else records
        except Exception as e:
            _log(f"Normalize: skipped ({type(e).__name__}: {e})")
            return records

    def run(self, external_source: Optional[Path] = None) -> PackagerResult:
        cfg = self.cfg

        # 1) Plugins + extensions (influence discovery if include_globs not set)
        plugins = self._load_plugins()
        plugin_exts: Tuple[str, ...] = tuple(sorted({ext for p in plugins for ext in getattr(p, "extensions", ())}))

        # 2) Discover source files directly under source_root
        files = FileDiscovery(cfg, plugin_exts).discover()

        # 3) Collect contents
        src_pairs = self._collect_sources(files)

        # 4) Plugin artifacts
        extras = self._plugin_artifacts(plugins, src_pairs)

        # 5) Optionally persist analysis/** to disk for later publishing
        self._write_analysis_files(extras)

        # 6) Combine for manifest
        combined: List[Tuple[str, bytes]] = src_pairs + extras

        # 7) Records + sums input
        records, sums_in = self._build_records(combined)

        # 8) Optional normalization
        records = self._maybe_normalize(records)

        # 9) Write manifest JSONL
        writer = BundleWriter(Path(cfg.out_bundle))
        writer.write(records)

        # 10) Write SHA256SUMS
        writer.write_sums(Path(cfg.out_sums), sums_in)

        # 11) Write run-spec and guide (assistant handoff)
        rsw = RunSpecWriter(Path(cfg.out_runspec))
        runspec = rsw.build_snapshot(cfg, {"source_root": str(cfg.source_root), "emitted_prefix": str(cfg.emitted_prefix)})
        rsw.write(runspec)

        gw = GuideWriter(Path(cfg.out_guide))
        gw.write(cfg, {"purpose": "assistant-handoff"}, {})

        _log(
            f"Wrote: {Path(cfg.out_bundle).name}, {Path(cfg.out_sums).name}, "
            f"{Path(cfg.out_runspec).name}, {Path(cfg.out_guide).name}"
        )
        return PackagerResult(
            out_bundle=Path(cfg.out_bundle),
            out_sums=Path(cfg.out_sums),
            out_runspec=Path(cfg.out_runspec),
            out_guide=Path(cfg.out_guide),
        )
