# v2/backend/core/utils/code_bundles/code_bundles/src/packager/core/orchestrator.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any, Callable
import base64, json, hashlib

# ---- robust imports (absolute first, then package-relative fallbacks) ----
try:
    from packager.core.config import PackConfig, TransportOptions
    from packager.core.paths import PathOps
    from packager.core.discovery import DiscoveryEngine, DiscoveryConfig
    from packager.io.manifest_writer import BundleWriter
    from packager.io.runspec_writer import RunSpecWriter
    from packager.io.guide_writer import GuideWriter
    from packager.languages.python.plugin import PythonAnalyzer
except Exception:  # package-relative fallbacks
    from ..core.config import PackConfig, TransportOptions  # type: ignore
    from ..core.paths import PathOps  # type: ignore
    from ..core.discovery import DiscoveryEngine, DiscoveryConfig  # type: ignore
    from ..io.manifest_writer import BundleWriter  # type: ignore
    from ..io.runspec_writer import RunSpecWriter  # type: ignore
    from ..io.guide_writer import GuideWriter  # type: ignore
    from ..languages.python.plugin import PythonAnalyzer  # type: ignore

# Publisher (absolute, then relative)
try:
    from packager.io.publisher import Publisher, LocalPublisher, GitHubPublisher, PublishItem
except ImportError:
    from ..io.publisher import Publisher, LocalPublisher, GitHubPublisher, PublishItem  # type: ignore


@dataclass(frozen=True)
class _FileRec:
    path: str
    data: bytes
    sha256: str


@dataclass
class PackagerResult:
    out_bundle: Path
    out_sums: Path
    out_runspec: Path
    out_guide: Path


def _logprint(msg: str) -> None:
    print(f"[packager] {msg}", flush=True)


class SourceIngestor:
    def __init__(self, cfg: PackConfig, log: Callable[[str], None]) -> None:
        self.cfg = cfg
        self._log = log

    def _is_within(self, p: Path, base: Path) -> bool:
        try:
            p.resolve().relative_to(base.resolve())
            return True
        except Exception:
            return False

    def ingest(self, external_source: Path) -> List[Path]:
        src_root = external_source
        dest = self.cfg.source_root

        eng = DiscoveryEngine(DiscoveryConfig(
            root=src_root,
            include_globs=self.cfg.include_globs,
            exclude_globs=self.cfg.exclude_globs,
            follow_symlinks=self.cfg.follow_symlinks,
            case_insensitive=self.cfg.case_insensitive,
            segment_excludes=self.cfg.segment_excludes,
        ))
        discovered = eng.discover()
        paths = [p for p in discovered if not self._is_within(p, dest)]
        self._log(f"Ingestion: discovered {len(discovered)} files; {len(discovered)-len(paths)} skipped (inside staging)")

        self._log(f"Ingestion: clearing destination '{dest}'")
        self.cfg.source_root.mkdir(parents=True, exist_ok=True)
        for p in sorted(self.cfg.source_root.rglob("*"), reverse=True):
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

        copied: List[Path] = []
        for sp in paths:
            if not sp.exists():
                self._log(f"Ingestion: missing '{sp}' — skipping")
                continue
            rel = sp.relative_to(src_root)
            dp = dest / rel
            PathOps.ensure_dir(dp)
            try:
                dp.write_bytes(sp.read_bytes())
                copied.append(dp)
            except Exception as e:
                self._log(f"Ingestion: failed to copy '{sp}' → '{dp}': {type(e).__name__}: {e}")
        self._log(f"Ingestion: copied {len(copied)} files")
        return copied


class FileDiscovery:
    def __init__(self, cfg: PackConfig, log: Callable[[str], None]) -> None:
        self.cfg = cfg
        self._log = log

    def discover(self) -> List[Path]:
        eng = DiscoveryEngine(DiscoveryConfig(
            root=self.cfg.source_root,
            include_globs=self.cfg.include_globs,
            exclude_globs=self.cfg.exclude_globs,
            follow_symlinks=self.cfg.follow_symlinks,
            case_insensitive=self.cfg.case_insensitive,
            segment_excludes=self.cfg.segment_excludes,
        ))
        paths = eng.discover()
        self._log(f"Discover: {len(paths)} files")
        return paths


class NormalizerAdapter:
    """Apply normalization to the discovered files (fallback to identity if rules lack .apply)."""
    def __init__(self, rules: Any, log: Callable[[str], None]) -> None:
        self.rules = rules
        self._log = log

    def _apply_text(self, text: str) -> str:
        r = self.rules
        if r is None:
            return text
        for attr in ("apply", "normalize", "process", "run"):
            fn = getattr(r, attr, None)
            if callable(fn):
                try:
                    return fn(text)
                except Exception as e:
                    self._log(f"Normalize: rule.{attr} failed: {type(e).__name__}: {e}; using original")
                    return text
        return text

    def normalize(self, inputs: List[Tuple[str, bytes]]) -> List[Tuple[str, bytes, str]]:
        normed: List[Tuple[str, bytes, str]] = []
        for path, data in inputs:
            try:
                text = data.decode("utf-8", errors="replace")
                out_text = self._apply_text(text)
                out = out_text.encode("utf-8", errors="replace")
            except Exception as e:
                self._log(f"Normalize: fallback passthrough for '{path}': {type(e).__name__}: {e}")
                out = data
            sha = hashlib.sha256(out).hexdigest()
            normed.append((path, out, sha))
        self._log(f"Normalize: {len(normed)} files")
        return normed


class PromptEmbedder:
    def __init__(self, cfg: PackConfig, log: Callable[[str], None]) -> None:
        self.cfg = cfg
        self._log = log

    def build(self) -> Optional[dict]:
        if not self.cfg.prompts or self.cfg.prompt_mode != "embed":
            return None
        src = self.cfg.prompts
        pub: Dict[str, Any] = {"kind": getattr(src, "kind", "unknown"), "paths": []}
        try:
            return pub
        except Exception as e:
            self._log(f"Prompts: skip (meta) due to error: {type(e).__name__}: {e}")
            return None


class Packager:
    def __init__(self, cfg: PackConfig, rules: Any) -> None:
        self.cfg = cfg
        self.rules = rules
        self._log = _logprint

        self.discovery = FileDiscovery(cfg, self._log)
        self.normalizer = NormalizerAdapter(rules, self._log)
        self.bundle = BundleWriter(cfg.out_bundle)
        self.run_writer = RunSpecWriter(cfg.out_runspec)
        self.guide_writer = GuideWriter(cfg.out_guide)

    def _emit_for_file(self, rel_path: str, data: bytes, sha256: str) -> List[dict]:
        return [{
            "type": "file_chunk",
            "path": rel_path,
            "byte_start": 0,
            "byte_end": len(data),
            "chunk_index": 0,
            "chunks_total": 1,
            "sha256_file": sha256,
            "sha256_chunk": hashlib.sha256(data).hexdigest(),
            "content_b64": base64.b64encode(data).decode("ascii"),
        }]

    def _skip_publish_path(self, emitted_path: str) -> bool:
        parts = emitted_path.split("/")
        seg_ex = set(self.cfg.segment_excludes)
        for seg in parts:
            if seg in seg_ex:
                return True
        lower = emitted_path.lower()
        if lower.endswith((".pyc", ".pyo", ".pyd", ".so", ".dll", ".dylib", ".class", ".o", ".a", ".lib", ".exe")):
            return True
        return False

    def _group_dir_for_part(self, idx: int, t: TransportOptions) -> Optional[str]:
        if not t.group_dirs:
            return None
        group_ix = idx // max(1, t.parts_per_dir)
        return f"{t.part_stem}_{group_ix+1:0{t.dir_suffix_width}d}"

    def _default_reading_order(self, split_info: Optional[dict]) -> List[str]:
        ro: List[str] = [
            "Start at codebase/ for the plain-text source.",
            "Then open analysis/contents_index.json for an inventory.",
            "Use analysis/roles.json and analysis/entrypoints.json to navigate.",
        ]
        if split_info and split_info.get("parts"):
            ro.append("If using transport parts, reassemble using the parts index and SHA256 sums.")
        return ro

    def _build_guide_compat(self, *, split_info: Optional[dict], prompts_public: Optional[dict]) -> Any:
        """Try various GuideWriter.build signatures; return a JSON-serializable object."""
        reading_order = self._default_reading_order(split_info)
        prompts_meta = prompts_public or {}
        split_meta = split_info or {}

        # 1) Legacy positional: (reading_order, cfg, prompts_meta, split_info)
        try:
            g = self.guide_writer.build(reading_order, self.cfg, prompts_meta, split_meta)
            return g
        except TypeError:
            pass
        # 2) Keyword-capable variants
        for kw in (
            dict(cfg=self.cfg, prompts_meta=prompts_meta, split_info=split_meta, reading_order=reading_order),
            dict(config=self.cfg, prompts=prompts_meta, split_info=split_meta),
            dict(cfg=self.cfg),
        ):
            try:
                g = self.guide_writer.build(**kw)  # type: ignore[arg-type]
                return g
            except TypeError:
                continue
        # 3) Minimal: synthesize a guide ourselves
        return {
            "reading_order": reading_order,
            "constraints": {"offline_only": True},
            "notes": "Fallback guide (writer.build signatures incompatible).",
        }

    def _write_guide_direct(self, guide_obj: Any) -> None:
        """Bypass GuideWriter.write(); persist JSON directly to cfg.out_guide."""
        try:
            if hasattr(guide_obj, "to_dict"):
                payload = guide_obj.to_dict()  # type: ignore[attr-defined]
            else:
                payload = guide_obj
            self.cfg.out_guide.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            # Last-resort ultra-minimal handoff so the pipeline doesn't die
            fallback = {
                "error": f"guide serialize failed: {type(e).__name__}: {e}",
                "reading_order": self._default_reading_order(None),
            }
            self.cfg.out_guide.write_text(json.dumps(fallback, ensure_ascii=False, indent=2), encoding="utf-8")

    # --- sums helpers -----------------------------------------------------------
    def _sha256_file(self, p: Path) -> str:
        h = hashlib.sha256()
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()

    def _write_sums_file(self, parts: List[Path], idx_path: Optional[Path]) -> None:
        """Write SHA256 sums for transport artifacts to cfg.out_sums."""
        out_dir = self.cfg.out_bundle.parent
        sums_fp = self.cfg.out_sums
        sums_fp.parent.mkdir(parents=True, exist_ok=True)

        lines: List[str] = []
        to_hash: List[Path] = []

        if parts:
            to_hash.extend(parts)
            if idx_path and idx_path.exists():
                to_hash.append(idx_path)
        else:
            if self.cfg.out_bundle.exists():
                to_hash.append(self.cfg.out_bundle)

        for pth in to_hash:
            rel = pth.relative_to(out_dir).as_posix()
            sha = self._sha256_file(pth)
            lines.append(f"{sha}  {rel}")

        sums_fp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        _logprint(f"Write: sums → '{sums_fp.name}'")

    def run(self, external_source: Optional[Path] = None) -> PackagerResult:
        self._log("Packager: start")

        publish_items: List[PublishItem] = []

        if external_source is not None:
            self._log(f"Packager: ingest external source '{external_source}'")
            SourceIngestor(self.cfg, self._log).ingest(external_source)
        else:
            self._log("Packager: no external source provided; using existing codebase/")

        paths = self.discovery.discover()

        path_bytes: List[Tuple[str, bytes]] = []
        prefix = self.cfg.emitted_prefix if self.cfg.emitted_prefix.endswith("/") else (self.cfg.emitted_prefix + "/")
        for p in paths:
            try:
                rel = p.relative_to(self.cfg.source_root).as_posix()
            except ValueError:
                rel = p.name
            ep = f"{prefix}{rel}"
            try:
                data = p.read_bytes()
            except Exception:
                self._log(f"Read: skip unreadable file '{p}'")
                continue
            path_bytes.append((ep, data))
        self._log(f"Read: collected {len(path_bytes)} files for normalization")
        normed = self.normalizer.normalize(path_bytes)

        records: List[dict] = []
        records.append({"type": "dir", "path": prefix})

        python_payloads: List[Tuple[str, bytes]] = []
        text_map: Dict[str, str] = {}
        for path, data, sha in normed:
            for rec in self._emit_for_file(path, data, sha):
                records.append(rec)
            if getattr(self.cfg.publish, "publish_codebase", False):
                if self._skip_publish_path(path):
                    self._log(f"Publish: skipping '{path}' (segment/binary filter)")
                else:
                    publish_items.append(PublishItem(path=path, data=data))
            if path.endswith(".py"):
                python_payloads.append((path, data))
                try:
                    text_map[path] = data.decode("utf-8", errors="replace")
                except Exception:
                    pass

        # Python analysis (instantiate with NO args)
        try:
            if python_payloads:
                analyzer = PythonAnalyzer()
                res = analyzer.analyze([(p, b) for (p, b) in python_payloads])
                if isinstance(res, tuple) and len(res) >= 3:
                    imports, calls, symbols = res[0], res[1], res[2]
                elif isinstance(res, dict):
                    imports, calls, symbols = res.get("imports", {}), res.get("calls", {}), res.get("symbols", {})
                else:
                    raise ValueError("Unexpected PythonAnalyzer.analyze return")
                graphs = {
                    "graphs/imports.json": imports,
                    "graphs/calls.json": calls,
                    "graphs/symbols.json": symbols,
                }
                for rel, obj in graphs.items():
                    payload = json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
                    sha = hashlib.sha256(payload).hexdigest()
                    for rec in self._emit_for_file(rel, payload, sha):
                        records.append(rec)
                    if getattr(self.cfg.publish, "publish_analysis", False):
                        publish_items.append(PublishItem(path=rel, data=payload))
        except Exception as e:
            self._log(f"Analysis: skipped due to error: {type(e).__name__}: {e}")

        contents_index = [{"p": p, "sha256": s, "bytes": len(b), "enc": "utf-8", "nl": "lf"} for (p, b, s) in normed]
        roles_map = self._roles_map([p for (p, _, __) in normed])
        entrypoints = self._scan_entrypoints(text_map)
        extras = {
            "analysis/contents_index.json": contents_index,
            "analysis/roles.json": roles_map,
            "analysis/entrypoints.json": entrypoints,
        }
        for rel, obj in extras.items():
            payload = json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
            sha = hashlib.sha256(payload).hexdigest()
            for rec in self._emit_for_file(rel, payload, sha):
                records.append(rec)
            if getattr(self.cfg.publish, "publish_analysis", False):
                publish_items.append(PublishItem(path=rel, data=payload))
        self._log(f"Extras: added {len(extras)} analysis summaries")

        prompts_meta = PromptEmbedder(self.cfg, self._log).build()
        prompts_public = dict(prompts_meta) if prompts_meta is not None else None

        self._log(f"Write: bundle → '{self.cfg.out_bundle.name}'")
        self.bundle.write(records)

        # --------- Transport artifacts: ONLY when publish_transport=True ----------
        t = self.cfg.transport
        parts: List[Path] = []
        removed_monolith = False
        split_info: Optional[Dict[str, Any]] = None
        idx_path: Optional[Path] = None

        do_transport = bool(getattr(self.cfg.publish, "publish_transport", False))
        if t.split_bytes > 0 and do_transport:
            self._log("Split: enabled")
            data = self.cfg.out_bundle.read_bytes()

            idx = 0
            index: List[dict] = []
            for i in range(0, len(data), t.split_bytes):
                chunk = data[i:i + t.split_bytes]
                idx += 1
                sha = hashlib.sha256(chunk).hexdigest()
                stem = f"{t.part_stem}.part{idx:02d}{t.part_ext if t.transport_as_text else ''}"
                group = self._group_dir_for_part(idx-1, t)
                if group:
                    pth = self.cfg.out_bundle.parent / group / stem
                else:
                    pth = self.cfg.out_bundle.parent / stem
                PathOps.ensure_dir(pth)
                pth.write_bytes(chunk)
                parts.append(pth)
                rel = f"{group}/{stem}" if group else stem
                index.append({"path": rel, "sha256": sha, "bytes": len(chunk)})
                self._log(f"Split: part {idx} → '{rel}' ({len(chunk)} bytes)")

            monolith_sha = hashlib.sha256(data).hexdigest()
            idx_path = self.cfg.out_bundle.parent / t.parts_index_name
            idx_payload = {
                "original_name": self.cfg.out_bundle.name,
                "reassembled_sha256": monolith_sha,
                "parts": index,
                "payload_format": "jsonl",
                "transport_hint": ("txt" if t.transport_as_text else "jsonl"),
                "chunk_records": bool(t.chunk_records),
                "chunk_bytes": t.chunk_bytes,
                "grouping": {
                    "group_dirs": t.group_dirs,
                    "parts_per_dir": t.parts_per_dir,
                    "dir_suffix_width": t.dir_suffix_width,
                    "dir_pattern": f"{t.part_stem}_{{:0{t.dir_suffix_width}d}}",
                },
            }
            idx_path.write_text(json.dumps(idx_payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
            self._log(f"Split: index → '{idx_path.name}'")

            if not t.preserve_monolith:
                self.cfg.out_bundle.unlink(missing_ok=True)
                removed_monolith = True
                self._log("Split: removed original monolithic bundle after splitting (preserve_monolith=False)")

            split_info = {
                "parts": [rec["path"] for rec in index],
                "removed_monolith": removed_monolith,
            }

            # SHA256SUMS only when we actually emit transport artifacts
            self._write_sums_file(parts, idx_path)
        elif do_transport:
            # No split requested but still publishing transport -> write sums for monolith
            self._write_sums_file(parts=[], idx_path=None)
        else:
            # GitHub/plain-text mode: NO parts, NO index, NO sums
            split_info = None
            idx_path = None

        prov = {
            "emitted_prefix": self.cfg.emitted_prefix,
            "source_root": str(self.cfg.source_root),
        }
        self._log(f"Write: run-spec → '{self.cfg.out_runspec.name}'")
        snap = self.run_writer.build_snapshot(self.cfg, prov, prompts_public)
        self.run_writer.write(snap)

        # ---- GUIDE: build (compat) then write directly to JSON file ------------
        self._log(f"Write: guide → '{self.cfg.out_guide.name}'")
        guide = self._build_guide_compat(split_info=split_info, prompts_public=prompts_public)
        self._write_guide_direct(guide)

        if getattr(self.cfg.publish, "publish_handoff", False):
            publish_items.append(PublishItem(path="handoff/assistant_handoff.v1.json", data=self.cfg.out_guide.read_bytes()))
            publish_items.append(PublishItem(path="handoff/superbundle.run.json", data=self.cfg.out_runspec.read_bytes()))

        if getattr(self.cfg.publish, "publish_transport", False):
            if idx_path and idx_path.exists():
                publish_items.append(PublishItem(path=f"transport/{idx_path.name}", data=idx_path.read_bytes()))
            if self.cfg.out_sums.exists():
                publish_items.append(PublishItem(path="transport/design_manifest.SHA256SUMS", data=self.cfg.out_sums.read_bytes()))
            for pth in parts:
                rel = pth.name if pth.parent == self.cfg.out_bundle.parent else f"{pth.parent.name}/{pth.name}"
                publish_items.append(PublishItem(path=f"transport/{rel}", data=pth.read_bytes()))

        pub = getattr(self.cfg, "publish", None)
        if pub:
            # Local publish ONLY if explicitly configured; no invented repo_layout
            if pub.mode in ("local", "both") and pub.local_publish_root:
                root = pub.local_publish_root
                self._log(f"Publish(Local): writing {len(publish_items)} files under '{root}'")
                LocalPublisher(root, clean_before_publish=bool(getattr(pub, "clean_before_publish", False))).publish(publish_items)

            if pub.mode in ("github", "both"):
                if not pub.github or not pub.github_token:
                    raise RuntimeError("GitHub publish selected but github coordinates/token not configured.")

                # Code files to push come from codebase/**
                code_items: List[PublishItem] = [it for it in publish_items if it.path.startswith("codebase/")]

                # Always also push the actual handoff files from OUT (canonical names at repo root)
                code_items.append(PublishItem(path="assistant_handoff.v1.json", data=self.cfg.out_guide.read_bytes()))
                code_items.append(PublishItem(path="superbundle.run.json", data=self.cfg.out_runspec.read_bytes()))

                self._log(
                    f"Publish(GitHub): repo={pub.github.owner}/{pub.github.repo} "
                    f"branch={pub.github.branch} base='{pub.github.base_path}' items={len(code_items)} "
                    f"(code + handoff)"
                )
                gh = GitHubPublisher(
                    owner=pub.github.owner,
                    repo=pub.github.repo,
                    branch=pub.github.branch,
                    base_path=pub.github.base_path,
                    token=pub.github_token,
                    clean_before_publish=bool(getattr(pub, "clean_before_publish", False)),
                )
                gh.publish(code_items)

        self._log("Packager: done")
        return PackagerResult(self.cfg.out_bundle, self.cfg.out_sums, self.cfg.out_runspec, self.cfg.out_guide)

    # ---- helpers ---------------------------------------------------------------
    def _roles_map(self, paths: List[str]) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for p in paths:
            roles: List[str] = []
            lp = p.lower()
            if "/tests/" in lp or lp.endswith(("_test.py", "test.py")):
                roles.append("tests")
            if "/scripts/" in lp or lp.endswith(".sh"):
                roles.append("scripts")
            if p.endswith((".md", ".rst", ".txt")):
                roles.append("docs")
            if p.endswith((".yml", ".yaml", ".json", ".toml", ".ini", ".cfg")):
                roles.append("config")
            if p.endswith(".py"):
                roles.append("python")
            if lp.endswith("setup.py") or "/build/" in lp or "/dist/" in lp:
                roles.append("build")
            if roles:
                out[p] = roles
        return out

    def _scan_entrypoints(self, text_map: Dict[str, str]) -> List[Dict[str, str]]:
        entries = []
        for p, t in text_map.items():
            if p.endswith(".py") and "__name__" in t and "__main__" in t:
                entries.append({"path": p, "reason": "if __name__ == '__main__'"})
            if p.endswith(".sh") and t.strip().startswith("#!"):
                entries.append({"path": p, "reason": "shebang script"})
        return entries
