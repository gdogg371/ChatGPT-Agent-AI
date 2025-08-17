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
except ImportError:
    from .config import PackConfig, TransportOptions
    from .paths import PathOps
    from .discovery import DiscoveryEngine, DiscoveryConfig
    from ..io.manifest_writer import BundleWriter
    from ..io.runspec_writer import RunSpecWriter
    from ..io.guide_writer import GuideWriter
    from ..languages.python.plugin import PythonAnalyzer

# Normalization (package-local first, then repo-root fallback)
try:
    from .normalize import NormalizationRules, apply_normalization as _apply_normalization
except ImportError:
    from normalize import NormalizationRules, apply_normalization as _apply_normalization  # type: ignore

# Publisher (absolute, then relative)
try:
    from packager.io.publisher import Publisher, LocalPublisher, GitHubPublisher, PublishItem
except ImportError:
    from ..io.publisher import Publisher, LocalPublisher, GitHubPublisher, PublishItem  # type: ignore


# ---- lightweight FileRec shim (avoid hard dependency on bundle_io) ----
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
    """Copy an external tree into codebase/ honoring excludes and globs, avoiding self-recursion."""
    def __init__(self, cfg: PackConfig, log: Callable[[str], None]) -> None:
        self.cfg = cfg
        self._log = log

    @staticmethod
    def _is_within(child: Path, parent: Path) -> bool:
        try:
            child.resolve().relative_to(parent.resolve())
            return True
        except Exception:
            return False

    def ingest(self, external_root: Path) -> List[Path]:
        ext = external_root.resolve()
        dest = self.cfg.source_root.resolve()
        ingest_excludes = tuple(sorted(set(self.cfg.segment_excludes) | {dest.name}))
        self._log(f"Ingestion: scanning '{ext}' (excluding segments: {ingest_excludes})")
        eng = DiscoveryEngine(DiscoveryConfig(
            root=ext,
            segment_excludes=ingest_excludes,
            include_globs=self.cfg.include_globs,
            exclude_globs=self.cfg.exclude_globs,
            case_insensitive=self.cfg.effective_case_insensitive(),
            follow_symlinks=self.cfg.follow_symlinks,
        ))
        discovered = eng.discover()
        paths = [p for p in discovered if not self._is_within(p, dest)]
        self._log(f"Ingestion: discovered {len(discovered)} files; {len(discovered)-len(paths)} skipped (inside staging)")

        # Clear destination (keep folder)
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

        # Copy
        copied: List[Path] = []
        for sp in paths:
            if not sp.exists():
                self._log(f"Ingestion: skipped vanished file '{sp}'")
                continue
            rel = sp.relative_to(ext)
            dp = self.cfg.source_root / rel
            dp.parent.mkdir(parents=True, exist_ok=True)
            try:
                dp.write_bytes(sp.read_bytes())
            except FileNotFoundError:
                self._log(f"Ingestion: source disappeared while copying '{sp}' — skipping")
                continue
            copied.append(dp)

        self._log(f"Ingestion: copied {len(copied)} files into '{self.cfg.source_root}'")
        return copied


class FileDiscovery:
    """Deterministic discovery with depth-aware segment excludes and globs."""
    def __init__(self, cfg: PackConfig, log: Callable[[str], None]) -> None:
        self.cfg = cfg
        self._log = log

    def discover(self) -> List[Path]:
        eng = DiscoveryEngine(DiscoveryConfig(
            root=self.cfg.source_root,
            segment_excludes=self.cfg.segment_excludes,
            include_globs=self.cfg.include_globs,
            exclude_globs=self.cfg.exclude_globs,
            case_insensitive=self.cfg.effective_case_insensitive(),
            follow_symlinks=self.cfg.follow_symlinks,
        ))
        paths = eng.discover()
        self._log(f"Discovery: {len(paths)} files under '{self.cfg.source_root}'")
        return paths


class NormalizerAdapter:
    """Wrap normalization to return (path, bytes, sha256) tuples."""
    def __init__(self, rules: NormalizationRules, log: Callable[[str], None]) -> None:
        self.rules = rules
        self._log = log

    def normalize(self, path_bytes: List[Tuple[str, bytes]]) -> List[Tuple[str, bytes, str]]:
        self._log(f"Normalize: applying rules to {len(path_bytes)} files")
        recs_in = [_FileRec(path=p, data=b, sha256="") for p, b in path_bytes]
        recs_out = _apply_normalization(recs_in, rules=self.rules)
        out = [(fr.path, fr.data, getattr(fr, "sha256", hashlib.sha256(fr.data).hexdigest())) for fr in recs_out]
        self._log(f"Normalize: complete ({len(out)} files)")
        return out


class PromptEmbedder:
    """Embed prompts from fs_dir, zip (path or bytes), or inline dict."""
    def __init__(self, cfg: PackConfig, log: Callable[[str], None]) -> None:
        self.cfg = cfg
        self._log = log

    def build(self) -> Optional[dict]:
        src = self.cfg.prompts
        if src is None or self.cfg.prompt_mode == "omit":
            self._log("Prompts: none (omitted)")
            return None

        files: List[Tuple[str, bytes]] = []

        if src.kind == "fs_dir":
            base = src.value if isinstance(src.value, Path) else Path(str(src.value))
            if not base.exists():
                self._log(f"Prompts: directory not found '{base}', skipping")
                return None
            for p in sorted(base.rglob("*")):
                if p.is_file():
                    files.append((p.relative_to(base).as_posix(), p.read_bytes()))
            self._log(f"Prompts: loaded {len(files)} files from '{base}'")

        elif src.kind == "zip":
            import io, zipfile
            if isinstance(src.value, (bytes, bytearray)):
                zbytes = bytes(src.value)
                src_desc = "<inline-zip-bytes>"
            else:
                zp = src.value if isinstance(src.value, Path) else Path(str(src.value))
                if not zp.exists():
                    self._log(f"Prompts: zip not found '{zp}', skipping")
                    return None
                zbytes = zp.read_bytes()
                src_desc = str(zp)
            with zipfile.ZipFile(io.BytesIO(zbytes)) as zf:
                for name in sorted(zf.namelist()):
                    if name.endswith("/"):
                        continue
                    files.append((name.lstrip("./"), zf.read(name)))
            self._log(f"Prompts: loaded {len(files)} files from {src_desc}")

        elif src.kind == "inline":
            if isinstance(src.value, dict):
                for k, v in src.value.items():
                    data = v.encode("utf-8") if isinstance(v, str) else bytes(v)
                    files.append((str(k).lstrip("./"), data))
                self._log(f"Prompts: loaded {len(files)} inline entries")
            else:
                self._log("Prompts: inline value not a dict, skipping")
                return None
        else:
            self._log(f"Prompts: unknown kind '{src.kind}', skipping")
            return None

        manifest = {
            "version": "1",
            "entries": {},
            "files": [{"path": f"prompts/{rel}", "sha256": hashlib.sha256(data).hexdigest()} for rel, data in files],
        }
        self._log("Prompts: manifest prepared")
        return {
            "pack": "prompts/prompt_pack.json",
            "entries": {},
            "_files": files,
            "_manifest": json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        }


class Packager:
    """Core orchestrator: writes bundle, checksums, run spec, guide, and (optionally) publishes."""
    def __init__(self, cfg: PackConfig, rules: NormalizationRules) -> None:
        self.cfg = cfg
        self.rules = rules
        self._log = _logprint

        self.discovery = FileDiscovery(cfg, self._log)
        self.normalizer = NormalizerAdapter(rules, self._log)
        self.bundle = BundleWriter(cfg.out_bundle)
        self.run_writer = RunSpecWriter(cfg.out_runspec)
        self.guide_writer = GuideWriter(cfg.out_guide)

    # -- record emission (base64) -------------------------------------------------
    def _emit_file_records_chunked(self, path: str, data: bytes, sha_file: str, chunk_bytes: Optional[int]) -> List[Dict[str, Any]]:
        if not chunk_bytes or chunk_bytes <= 0:
            return [{
                "type": "file", "path": path,
                "content_b64": base64.b64encode(data).decode("ascii"),
                "sha256": sha_file
            }]
        recs: List[Dict[str, Any]] = []
        n = len(data)
        if n == 0:
            recs.append({
                "type": "file_chunk", "path": path,
                "chunk_index": 0, "chunks_total": 1,
                "byte_start": 0, "byte_end": 0,
                "sha256_chunk": hashlib.sha256(b"").hexdigest(),
                "sha256_file": sha_file, "content_b64": ""
            })
            return recs
        chunks_total = (n + chunk_bytes - 1) // chunk_bytes
        off = 0
        idx = 0
        while off < n:
            end = min(off + chunk_bytes, n)
            blob = data[off:end]
            recs.append({
                "type": "file_chunk", "path": path,
                "chunk_index": idx, "chunks_total": chunks_total,
                "byte_start": off, "byte_end": end,
                "sha256_chunk": hashlib.sha256(blob).hexdigest(),
                "sha256_file": sha_file,
                "content_b64": base64.b64encode(blob).decode("ascii")
            })
            idx += 1
            off = end
        return recs

    def _emit_for_file(self, path: str, data: bytes, sha_file: str) -> List[Dict[str, Any]]:
        t: TransportOptions = self.cfg.transport
        if t.chunk_records:
            return self._emit_file_records_chunked(path, data, sha_file, t.chunk_bytes)
        return [{
            "type": "file", "path": path,
            "content_b64": base64.b64encode(data).decode("ascii"),
            "sha256": sha_file
        }]

    # -- publish filtering --------------------------------------------------------
    def _skip_publish_path(self, emitted_path: str) -> bool:
        """
        Skip non-source and cache/binary junk for GitHub/local mirrors:
          - any path segment in cfg.segment_excludes (e.g., __pycache__, .git, node_modules, dist, output, software)
          - common compiled/binary extensions
        """
        parts = emitted_path.split("/")
        seg_ex = set(self.cfg.segment_excludes)
        for seg in parts:
            if seg in seg_ex:
                return True
        lower = emitted_path.lower()
        if lower.endswith((".pyc", ".pyo", ".pyd", ".so", ".dll", ".dylib", ".class", ".o", ".a", ".lib", ".exe")):
            return True
        return False

    # -- part grouping ------------------------------------------------------------
    def _group_dir_for_part(self, part_num: int, t: TransportOptions, root: Path) -> Path:
        """Directory for a given 1-based part number (e.g., design_manifest_01/)."""
        group = (part_num - 1) // max(1, t.parts_per_dir) + 1
        suffix = f"{group:0{t.dir_suffix_width}d}"
        return root / f"{t.part_stem}_{suffix}"

    def _split_lines_to_parts(self, src: Path, *, max_bytes: int, t: TransportOptions) -> List[Path]:
        """
        Split manifest into parts; if grouping enabled, place each part into
        design_manifest_01/, design_manifest_02/, … (10 files per dir by default).
        """
        parts: List[Path] = []
        part = 1
        written = 0

        # first destination (dir may be grouped)
        dir_path = self._group_dir_for_part(part, t, src.parent) if t.group_dirs else src.parent
        dir_path.mkdir(parents=True, exist_ok=True)
        out_path = dir_path / f"{t.part_stem}.part{part:02d}{(t.part_ext if t.transport_as_text else '.jsonl')}"
        out = out_path.open("w", encoding="utf-8")
        parts.append(out_path)

        with src.open("r", encoding="utf-8") as f:
            for line in f:
                b = len(line.encode("utf-8"))
                if written and written + b > max_bytes:
                    out.close()
                    part += 1
                    written = 0
                    dir_path = self._group_dir_for_part(part, t, src.parent) if t.group_dirs else src.parent
                    dir_path.mkdir(parents=True, exist_ok=True)
                    out_path = dir_path / f"{t.part_stem}.part{part:02d}{(t.part_ext if t.transport_as_text else '.jsonl')}"
                    out = out_path.open("w", encoding="utf-8")
                    parts.append(out_path)
                out.write(line)
                written += b
        out.close()
        return parts

    # -- main ---------------------------------------------------------------------
    def run(self, external_source: Optional[Path] = None) -> PackagerResult:
        self._log("Packager: start")

        # Collect items to publish (optional GitHub/local mirror)
        publish_items: List[PublishItem] = []

        # 0) Optional ingestion
        if external_source is not None:
            self._log(f"Packager: ingest external source '{external_source}'")
            SourceIngestor(self.cfg, self._log).ingest(external_source)
        else:
            self._log("Packager: no external source provided; using existing codebase/")

        # 1) Discover
        paths = self.discovery.discover()

        # 2) Read + normalize
        path_bytes: List[Tuple[str, bytes]] = []
        for p in paths:
            rel = PathOps.to_posix_rel(p, self.cfg.source_root)
            ep = PathOps.emitted_path(rel, self.cfg.emitted_prefix)
            try:
                data = p.read_bytes()
            except Exception:
                self._log(f"Read: skip unreadable file '{p}'")
                continue
            path_bytes.append((ep, data))
        self._log(f"Read: collected {len(path_bytes)} files for normalization")
        normed = self.normalizer.normalize(path_bytes)

        # 3) Build JSONL records
        records: List[dict] = []
        prefix = self.cfg.emitted_prefix if self.cfg.emitted_prefix.endswith("/") else self.cfg.emitted_prefix + "/"
        records.append({"type": "dir", "path": prefix})

        python_payloads: List[Tuple[str, bytes]] = []
        text_map: Dict[str, str] = {}
        emitted_count = 1
        for path, data, sha in normed:
            # emit bundle records
            for rec in self._emit_for_file(path, data, sha):
                records.append(rec)
                emitted_count += 1
            # collect for publishing (codebase) with skip filter
            if getattr(self.cfg.publish, "publish_codebase", False):
                if self._skip_publish_path(path):
                    self._log(f"Publish: skipping '{path}' (segment/binary filter)")
                else:
                    publish_items.append(PublishItem(path=path, data=data))
            # analysis input set for python analyzers
            if path.endswith(".py"):
                python_payloads.append((path, data))
            # for entrypoint detection
            try:
                text_map[path] = data.decode("utf-8")
            except Exception:
                pass

        self._log(
            f"Bundle: prepared {emitted_count} JSONL records (incl. dir) "
            f"[chunks={'on' if self.cfg.transport.chunk_records else 'off'}, chunk_bytes={self.cfg.transport.chunk_bytes}]"
        )

        # 4) Python analysis artifacts (best-effort)
        py = PythonAnalyzer()
        try:
            self._log(f"Analysis: running Python analyzers on {len(python_payloads)} files")
            artifacts = py.analyze(python_payloads)
            self._log(f"Analysis: produced {len(artifacts)} artifacts")
            for rel, obj in artifacts.items():
                payload = json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
                sha = hashlib.sha256(payload).hexdigest()
                for rec in self._emit_for_file(rel, payload, sha):
                    records.append(rec)
                    emitted_count += 1
                if getattr(self.cfg.publish, "publish_analysis", False):
                    publish_items.append(PublishItem(path=rel, data=payload))
        except Exception as e:
            self._log(f"Analysis: skipped due to error: {type(e).__name__}: {e}")

        # 5) Extra artifacts
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
                emitted_count += 1
            if getattr(self.cfg.publish, "publish_analysis", False):
                publish_items.append(PublishItem(path=rel, data=payload))
        self._log(f"Extras: added {len(extras)} analysis summaries")

        # 6) Optional prompts
        prompts_public: Optional[dict]
        prompts_meta = PromptEmbedder(self.cfg, self._log).build()
        if prompts_meta is not None:
            for rel, data in prompts_meta.get("_files", []):
                sha = hashlib.sha256(data).hexdigest()
                for rec in self._emit_for_file(f"prompts/{rel}", data, sha):
                    records.append(rec)
                    emitted_count += 1
                if getattr(self.cfg.publish, "publish_prompts", False):
                    publish_items.append(PublishItem(path=f"prompts/{rel}", data=data))
            man_bytes = prompts_meta.get("_manifest", b"{}")
            man_sha = hashlib.sha256(man_bytes).hexdigest()
            for rec in self._emit_for_file("prompts/prompt_pack.json", man_bytes, man_sha):
                records.append(rec)
                emitted_count += 1
            if getattr(self.cfg.publish, "publish_prompts", False):
                publish_items.append(PublishItem(path="prompts/prompt_pack.json", data=man_bytes))
            self._log(f"Prompts: embedded {len(prompts_meta.get('_files', []))} files + manifest")
            prompts_public = {k: v for k, v in prompts_meta.items() if not k.startswith("_")}
        else:
            prompts_public = None

        # 7) Write bundle
        self._log(f"Write: emitting bundle to '{self.cfg.out_bundle}'")
        self.bundle.write(records)

        # 8) Split, index
        parts: List[Path] = []
        split_info: Optional[Dict[str, Any]] = None
        t = self.cfg.transport
        removed_monolith = False
        monolith_bytes: Optional[bytes] = None
        monolith_sha: Optional[str] = None

        if t.split_bytes and t.split_bytes > 0:
            try:
                monolith_bytes = self.cfg.out_bundle.read_bytes()
                monolith_sha = hashlib.sha256(monolith_bytes).hexdigest()
                self._log(
                    f"Split: partitioning '{self.cfg.out_bundle.name}' into ~{t.split_bytes} byte parts "
                    f"(group_dirs={t.group_dirs}, parts_per_dir={t.parts_per_dir})"
                )

                parts = self._split_lines_to_parts(self.cfg.out_bundle, max_bytes=t.split_bytes, t=t)
                self._log(f"Split: wrote {len(parts)} part(s) across {len(set(p.parent for p in parts))} directory(ies)")

                index = []
                for pth in parts:
                    data = pth.read_bytes()
                    index.append({
                        "path": pth.name if pth.parent == self.cfg.out_bundle.parent else f"{pth.parent.name}/{pth.name}",
                        "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest()
                    })

                idx_path = self.cfg.out_bundle.parent / t.parts_index_name
                idx_payload = {
                    "original_name": self.cfg.out_bundle.name,
                    "reassembled_sha256": monolith_sha,
                    "parts": index,                          # NOTE: paths include subdir when grouped
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
                    "parts": [rec["path"] for rec in index],   # relative paths (with subdirs)
                    "index": idx_path.name,
                    "max_bytes": t.split_bytes,
                    "original_removed": not t.preserve_monolith,
                    "monolith_sha256": monolith_sha,
                    "transport_hint": ("txt" if t.transport_as_text else "jsonl"),
                    "grouping": idx_payload["grouping"],
                }
            except Exception as e:
                self._log(f"Split: disabled due to error: {type(e).__name__}: {e}")

        # 9) Run-spec and guide
        runspec = RunSpecWriter.build_snapshot(
            self.cfg,
            {"source_root": str(self.cfg.source_root), "emitted_prefix": self.cfg.emitted_prefix},
            prompts_public
        )
        self._log(f"Write: run-spec → '{self.cfg.out_runspec}'")
        self.run_writer.write(runspec)

        reading_order = [
            {"path": "assistant_handoff.v1.json", "why": "Start here"},
            {"path": "analysis/contents_index.json", "why": "Quick file inventory"},
            {"path": "analysis/ldt.json", "why": "Code structure (Python)"},
            {"path": "analysis/roles.json", "why": "File roles"},
            {"path": "analysis/entrypoints.json", "why": "Runnable entrypoints"},
        ]
        self._log(f"Write: guide → '{self.cfg.out_guide}'")
        self.guide_writer.write(reading_order, self.cfg, prompts_public, split_info)

        # 10) Checksums
        sums_inputs: List[Tuple[str, bytes]] = []

        # Include the monolith bytes for checksum line even if file was removed.
        if monolith_bytes is not None:
            sums_inputs.append((self.cfg.out_bundle.name, monolith_bytes))
        elif not removed_monolith:
            # fallback: include from disk if still present
            try:
                sums_inputs.append((self.cfg.out_bundle.name, self.cfg.out_bundle.read_bytes()))
            except Exception:
                pass

        sums_inputs.append((self.cfg.out_runspec.name, self.cfg.out_runspec.read_bytes()))
        sums_inputs.append((self.cfg.out_guide.name, self.cfg.out_guide.read_bytes()))
        for pth in parts:
            rel = pth.name if pth.parent == self.cfg.out_bundle.parent else f"{pth.parent.name}/{pth.name}"
            sums_inputs.append((rel, pth.read_bytes()))
        idx_path = self.cfg.out_bundle.parent / self.cfg.transport.parts_index_name
        if idx_path.exists():
            sums_inputs.append((idx_path.name, idx_path.read_bytes()))
        self._log(f"Write: checksums → '{self.cfg.out_sums}' ({len(sums_inputs)} artifacts)")
        self.bundle.write_sums(self.cfg.out_sums, sums_inputs)

        # 11) Prepare publish set: handoff/run-spec and (optionally) transport
        if getattr(self.cfg.publish, "publish_handoff", False):
            publish_items.append(PublishItem(path="handoff/assistant_handoff.v1.json", data=self.cfg.out_guide.read_bytes()))
            publish_items.append(PublishItem(path="handoff/superbundle.run.json", data=self.cfg.out_runspec.read_bytes()))

        if getattr(self.cfg.publish, "publish_transport", False):
            if idx_path.exists():
                publish_items.append(PublishItem(path=f"transport/{idx_path.name}", data=idx_path.read_bytes()))
            publish_items.append(PublishItem(path="transport/design_manifest.SHA256SUMS", data=self.cfg.out_sums.read_bytes()))
            for pth in parts:
                rel = pth.name if pth.parent == self.cfg.out_bundle.parent else f"{pth.parent.name}/{pth.name}"
                publish_items.append(PublishItem(path=f"transport/{rel}", data=pth.read_bytes()))

        # 12) Publish (local mirror and/or GitHub)
        pub = getattr(self.cfg, "publish", None)
        if pub:
            if pub.mode in ("local", "both"):
                root = pub.local_publish_root or (self.cfg.out_bundle.parent / "repo_layout")
                self._log(f"Publish(Local): writing {len(publish_items)} files under '{root}'")
                LocalPublisher(root, clean_before_publish=bool(getattr(pub, "clean_before_publish", False))).publish(publish_items)
            if pub.mode in ("github", "both"):
                if not pub.github or not pub.github_token:
                    raise RuntimeError("GitHub publish selected but github coordinates/token not configured.")
                self._log(
                    f"Publish(GitHub): repo={pub.github.owner}/{pub.github.repo} "
                    f"branch={pub.github.branch} base='{pub.github.base_path}' items={len(publish_items)}"
                )
                gh = GitHubPublisher(
                    owner=pub.github.owner,
                    repo=pub.github.repo,
                    branch=pub.github.branch,
                    base_path=pub.github.base_path,
                    token=pub.github_token,
                    clean_before_publish=bool(getattr(pub, "clean_before_publish", False)),
                )
                gh.publish(publish_items)

        self._log("Packager: done")
        return PackagerResult(self.cfg.out_bundle, self.cfg.out_sums, self.cfg.out_runspec, self.cfg.out_guide)

    # ---- helpers ---------------------------------------------------------------
    def _roles_map(self, paths: List[str]) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for p in paths:
            roles: List[str] = []
            lp = p.lower()
            if "/tests/" in lp or lp.startswith("tests/") or lp.endswith("_test.py"):
                roles.append("tests")
            if lp.endswith(".sh"):
                roles.append("script")
            if lp.endswith(".sql"):
                roles.append("sql")
            if lp.endswith(".py"):
                roles.append("python")
                if lp.endswith("__main__.py"):
                    roles.append("entrypoint")
            if "/bin/" in lp or "/scripts/" in lp:
                roles.append("script")
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
