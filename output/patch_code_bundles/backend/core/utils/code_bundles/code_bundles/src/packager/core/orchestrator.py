# src/packager/core/orchestrator.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any
import base64
import json
import hashlib
import urllib.request
import urllib.error
import urllib.parse
import sys
import inspect

# ----- robust imports: absolute first, then relative fallback -----
try:
    from packager.core.config import PackConfig  # only for typing / intent
    from packager.core.paths import PathOps
    from packager.core.discovery import DiscoveryEngine, DiscoveryConfig
    from packager.io.manifest_writer import BundleWriter
    from packager.io.runspec_writer import RunSpecWriter
    from packager.io.guide_writer import GuideWriter
    from packager.languages.python.plugin import PythonAnalyzer
except ImportError:  # allow running without -m or odd sys.path
    from .paths import PathOps
    from .discovery import DiscoveryEngine, DiscoveryConfig
    from ..io.manifest_writer import BundleWriter
    from ..io.runspec_writer import RunSpecWriter
    from ..io.guide_writer import GuideWriter
    from ..languages.python.plugin import PythonAnalyzer

# Normalization: package-local first, then repo-root fallback
try:
    from .normalize import NormalizationRules, apply_normalization as _apply_normalization
except ImportError:
    try:
        from normalize import NormalizationRules, apply_normalization as _apply_normalization  # type: ignore
    except Exception:
        # permissive fallback: no-op rules
        @dataclass
        class NormalizationRules:  # type: ignore
            pass
        def _apply_normalization(recs, rules=None):  # type: ignore
            return recs

# FileRec shim: don’t hard-crash if bundle_io isn’t importable in this context
try:
    from bundle_io import FileRec as _FileRec  # path, data, sha256
except Exception:
    from dataclasses import dataclass as _dc
    @_dc(frozen=True)
    class _FileRec:  # minimal compatible shape
        path: str
        data: bytes
        sha256: str


def _log(msg: str) -> None:
    print(f"[packager] {msg}", flush=True)


@dataclass
class PackagerResult:
    out_bundle: Path
    out_sums: Path
    out_runspec: Path
    out_guide: Path


# --------------------------- Source ingest -------------------------------------
class SourceIngestor:
    """Copies an external tree into codebase/ honoring excludes and globs."""
    def __init__(self, cfg) -> None:
        self.cfg = cfg

    def ingest(self, external_root: Path) -> List[Path]:
        ext = external_root.resolve()
        _log(f"Ingestion: scanning '{ext}'")
        eng = DiscoveryEngine(DiscoveryConfig(
            root=ext,
            segment_excludes=tuple(self.cfg.segment_excludes),
            include_globs=tuple(self.cfg.include_globs),
            exclude_globs=tuple(self.cfg.exclude_globs),
            case_insensitive=getattr(self.cfg, "case_insensitive", False),
            follow_symlinks=getattr(self.cfg, "follow_symlinks", False),
        ))
        paths = eng.discover()
        _log(f"Ingestion: discovered {len(paths)} files to copy")

        # Clear target (keep folder)
        _log(f"Ingestion: clearing destination '{self.cfg.source_root}'")
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
                pass  # best-effort

        # Copy files
        copied: List[Path] = []
        for sp in paths:
            rel = sp.relative_to(ext)
            dp = self.cfg.source_root / rel
            dp.parent.mkdir(parents=True, exist_ok=True)
            dp.write_bytes(sp.read_bytes())
            copied.append(dp)
        _log(f"Ingestion: copied {len(copied)} files into '{self.cfg.source_root}'")
        return copied


# --------------------------- Discovery wrapper ---------------------------------
class FileDiscovery:
    """Deterministic discovery with depth-aware segment excludes and globs."""
    def __init__(self, cfg) -> None:
        self.cfg = cfg

    def discover(self) -> List[Path]:
        eng = DiscoveryEngine(DiscoveryConfig(
            root=self.cfg.source_root,
            segment_excludes=tuple(self.cfg.segment_excludes),
            include_globs=tuple(self.cfg.include_globs),
            exclude_globs=tuple(self.cfg.exclude_globs),
            case_insensitive=getattr(self.cfg, "case_insensitive", False),
            follow_symlinks=getattr(self.cfg, "follow_symlinks", False),
        ))
        paths = eng.discover()
        _log(f"Discovery: {len(paths)} files under '{self.cfg.source_root}'")
        return paths


# --------------------------- Normalization adapter -----------------------------
class NormalizerAdapter:
    """Wraps normalization to return (path, bytes, sha256) tuples."""
    def __init__(self, rules: NormalizationRules | None) -> None:
        self.rules = rules

    def normalize(self, path_bytes: List[Tuple[str, bytes]]) -> List[Tuple[str, bytes, str]]:
        _log(f"Normalize: applying rules to {len(path_bytes)} files")
        recs_in = [_FileRec(path=p, data=b, sha256="") for p, b in path_bytes]
        recs_out = _apply_normalization(recs_in, rules=self.rules) if self.rules is not None else recs_in
        out = [(fr.path, fr.data, getattr(fr, "sha256", hashlib.sha256(fr.data).hexdigest())) for fr in recs_out]
        _log(f"Normalize: complete ({len(out)} files)")
        return out


# --------------------------- Prompt pack (optional) ----------------------------
class PromptEmbedder:
    """Embeds prompts from fs_dir, zip (path or bytes), or inline dict."""
    def __init__(self, cfg) -> None:
        self.cfg = cfg

    def build(self) -> Optional[dict]:
        src = getattr(self.cfg, "prompts", None)
        if src is None or getattr(self.cfg, "prompt_mode", "omit") == "omit":
            _log("Prompts: none (omitted)")
            return None

        files: List[Tuple[str, bytes]] = []

        if src.kind == "fs_dir":
            base = src.value if isinstance(src.value, Path) else Path(str(src.value))
            if not base.exists():
                _log(f"Prompts: directory not found '{base}', skipping")
                return None
            for p in sorted(base.rglob("*")):
                if p.is_file():
                    files.append((p.relative_to(base).as_posix(), p.read_bytes()))
            _log(f"Prompts: loaded {len(files)} files from '{base}'")

        elif src.kind == "zip":
            import io, zipfile
            if isinstance(src.value, (bytes, bytearray)):
                zbytes = bytes(src.value)
                src_desc = "<inline-zip-bytes>"
            else:
                zp = src.value if isinstance(src.value, Path) else Path(str(src.value))
                if not zp.exists():
                    _log(f"Prompts: zip not found '{zp}', skipping")
                    return None
                zbytes = zp.read_bytes()
                src_desc = str(zp)
            with zipfile.ZipFile(io.BytesIO(zbytes)) as zf:
                for name in sorted(zf.namelist()):
                    if name.endswith("/"):
                        continue
                    files.append((name.lstrip("./"), zf.read(name)))
            _log(f"Prompts: loaded {len(files)} files from {src_desc}")

        elif src.kind == "inline":
            if isinstance(src.value, dict):
                for k, v in src.value.items():
                    data = v.encode("utf-8") if isinstance(v, str) else bytes(v)
                    files.append((str(k).lstrip("./"), data))
                _log(f"Prompts: loaded {len(files)} inline entries")
            else:
                _log("Prompts: inline value not a dict, skipping")
                return None
        else:
            _log(f"Prompts: unknown kind '{src.kind}', skipping")
            return None

        manifest = {
            "version": "1",
            "entries": {},
            "files": [{"path": f"prompts/{rel}", "sha256": hashlib.sha256(data).hexdigest()} for rel, data in files],
        }
        _log("Prompts: manifest prepared")
        return {
            "pack": "prompts/prompt_pack.json",
            "entries": {},
            "_files": files,
            "_manifest": json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        }


# --------------------------- GitHub uploader (Contents API) --------------------
class _GH:
    @staticmethod
    def _headers(token: str) -> Dict[str, str]:
        return {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "code-bundles-packager/1.0",
        }

    @staticmethod
    def _get_sha(owner: str, repo: str, branch: str, path: str, token: str) -> Optional[str]:
        url = f"https://api.github.com/repos/{owner}/{repo}/contents/{urllib.parse.quote(path)}?ref={urllib.parse.quote(branch)}"
        req = urllib.request.Request(url, headers=_GH._headers(token), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                info = json.loads(resp.read().decode("utf-8"))
                return info.get("sha")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
        except Exception:
            return None

    @staticmethod
    def put_file(owner: str, repo: str, branch: str, path: str, content_b64: str, token: str, message: str) -> None:
        url = f"https://api.github.com/repos/{owner}/{repo}/contents/{urllib.parse.quote(path)}"
        sha = _GH._get_sha(owner, repo, branch, path, token)
        body = {
            "message": message,
            "content": content_b64,
            "branch": branch,
        }
        if sha:
            body["sha"] = sha
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, headers=_GH._headers(token), method="PUT", data=data)
        with urllib.request.urlopen(req, timeout=60) as resp:
            _ = resp.read()


# --------------------------- Packager ------------------------------------------
class Packager:
    """Core orchestrator that writes the bundle, checksums, run spec, and guide."""
    def __init__(self, cfg, rules: NormalizationRules | None) -> None:
        self.cfg = cfg
        self.rules = rules
        self.discovery = FileDiscovery(cfg)
        self.normalizer = NormalizerAdapter(rules)
        self.bundle = BundleWriter(cfg.out_bundle)
        self.run_writer = RunSpecWriter(cfg.out_runspec)
        self.guide_writer = GuideWriter(cfg.out_guide)

    def _clean_output_dir(self) -> None:
        """Always clean <output>/design_manifest before each run."""
        out_dir = self.cfg.out_bundle.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        _log(f"Output: cleaning '{out_dir}'")
        for p in sorted(out_dir.rglob("*"), reverse=True):
            try:
                if p.is_file() or p.is_symlink():
                    p.unlink()
                elif p.is_dir():
                    p.rmdir()
            except Exception:
                pass

    def run(self, external_source: Optional[Path] = None) -> PackagerResult:
        _log("Packager: start")

        # 0) Optional ingestion into source_root
        if external_source is not None:
            _log(f"Packager: ingest external source '{external_source}'")
            SourceIngestor(self.cfg).ingest(external_source)
        else:
            _log("Packager: no external source provided; using existing codebase/")

        # 0.5) Always clean outputs dir before we emit anything (your request)
        self._clean_output_dir()

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
                _log(f"Read: skip unreadable file '{p}'")
                continue
            path_bytes.append((ep, data))
        _log(f"Read: collected {len(path_bytes)} files for normalization")
        normed = self.normalizer.normalize(path_bytes)

        # 3) Build JSONL records
        records: List[dict] = []
        prefix = self.cfg.emitted_prefix if self.cfg.emitted_prefix.endswith("/") else self.cfg.emitted_prefix + "/"
        records.append({"type": "dir", "path": prefix})

        python_payloads: List[Tuple[str, bytes]] = []
        text_map: Dict[str, str] = {}
        for path, data, sha in normed:
            records.append({
                "type": "file", "path": path,
                "content_b64": base64.b64encode(data).decode("ascii"),
                "sha256": sha
            })
            if path.endswith(".py"):
                python_payloads.append((path, data))
            try:
                text_map[path] = data.decode("utf-8")
            except Exception:
                pass
        _log(f"Bundle: prepared {len(records)} JSONL records (including dir header)")

        # 4) Python analysis artifacts (best-effort)
        py = PythonAnalyzer()
        try:
            _log(f"Analysis: running Python analyzers on {len(python_payloads)} files")
            artifacts = py.analyze(python_payloads)
            _log(f"Analysis: produced {len(artifacts)} artifacts")
            for rel, obj in artifacts.items():
                payload = json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
                records.append({
                    "type": "file", "path": rel,
                    "content_b64": base64.b64encode(payload).decode("ascii"),
                    "sha256": hashlib.sha256(payload).hexdigest()
                })
        except Exception as e:
            _log(f"Analysis: skipped due to error: {type(e).__name__}: {e}")

        # 5) Extra artifacts
        contents_index = [
            {"p": p, "sha256": s, "bytes": len(b), "enc": "utf-8", "nl": "lf"}
            for (p, b, s) in normed
        ]
        roles_map = self._roles_map([p for (p, _, __) in normed])
        entrypoints = self._scan_entrypoints(text_map)
        extras = {
            "analysis/contents_index.json": contents_index,
            "analysis/roles.json": roles_map,
            "analysis/entrypoints.json": entrypoints,
        }
        for rel, obj in extras.items():
            payload = json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
            records.append({
                "type": "file", "path": rel,
                "content_b64": base64.b64encode(payload).decode("ascii"),
                "sha256": hashlib.sha256(payload).hexdigest()
            })
        _log(f"Extras: added {len(extras)} analysis summaries")

        # 6) Optional prompts
        prompts_public: Optional[dict]
        prompts_meta = PromptEmbedder(self.cfg).build()
        if prompts_meta is not None:
            for rel, data in prompts_meta.get("_files", []):
                records.append({
                    "type": "file", "path": f"prompts/{rel}",
                    "content_b64": base64.b64encode(data).decode("ascii"),
                    "sha256": hashlib.sha256(data).hexdigest()
                })
            man_bytes = prompts_meta.get("_manifest", b"{}")
            records.append({
                "type": "file", "path": "prompts/prompt_pack.json",
                "content_b64": base64.b64encode(man_bytes).decode("ascii"),
                "sha256": hashlib.sha256(man_bytes).hexdigest()
            })
            _log(f"Prompts: embedded {len(prompts_meta.get('_files', []))} files + manifest")
            prompts_public = {k: v for k, v in prompts_meta.items() if not k.startswith("_")}
        else:
            prompts_public = None

        # 7) Write files (bundle, run-spec, guide, sums)
        _log(f"Write: emitting bundle to '{self.cfg.out_bundle}'")
        self.bundle.write(records)

        # Support both class/static signatures of RunSpecWriter.build_snapshot
        meta = {"source_root": str(self.cfg.source_root), "emitted_prefix": self.cfg.emitted_prefix}
        runspec_obj: Optional[dict] = None
        try:
            # try class/static (cfg, meta, prompts)
            runspec_obj = RunSpecWriter.build_snapshot(self.cfg, meta, prompts_public)  # type: ignore[arg-type]
        except TypeError as e1:
            _log(f"RunSpecWriter signature probe failed: {type(e1).__name__}: {e1}")
            try:
                runspec_obj = RunSpecWriter.build_snapshot(meta, self.cfg, prompts_public)  # type: ignore[arg-type]
            except Exception as e2:
                _log(f"RunSpecWriter.build_snapshot incompatible, falling back: {type(e2).__name__}: {e2}")
                runspec_obj = None

        if runspec_obj is None:
            _log("RunSpecWriter: using fallback minimal run-spec")
            runspec_obj = {
                "version": "1",
                "provenance": {
                    "source_root": str(self.cfg.source_root),
                    "emitted_prefix": self.cfg.emitted_prefix,
                },
                "prompts": prompts_public or {},
            }

        _log(f"Write: run-spec → '{self.cfg.out_runspec}'")
        self.run_writer.write(runspec_obj)

        reading_order = [
            {"path": "assistant_handoff.v1.json", "why": "Start here"},
            {"path": "analysis/contents_index.json", "why": "Quick file inventory"},
            {"path": "analysis/ldt.json", "why": "Code structure (Python)"},
            {"path": "analysis/roles.json", "why": "File roles"},
            {"path": "analysis/entrypoints.json", "why": "Runnable entrypoints"},
        ]
        _log(f"Write: guide → '{self.cfg.out_guide}'")
        self.guide_writer.write(reading_order, self.cfg, prompts_public)

        # Checksums (still write locally, but we won't publish them)
        sums_inputs = [
            (self.cfg.out_bundle.name, self.cfg.out_bundle.read_bytes()),
            (self.cfg.out_runspec.name, self.cfg.out_runspec.read_bytes()),
            (self.cfg.out_guide.name, self.cfg.out_guide.read_bytes()),
        ]
        _log(f"Write: checksums → '{self.cfg.out_sums}' ({len(sums_inputs)} artifacts)")
        self.bundle.write_sums(self.cfg.out_sums, sums_inputs)

        # 8) Publish
        self._publish_codebase_to_github()
        self._publish_outputs_to_github()

        _log("Packager: done")
        return PackagerResult(self.cfg.out_bundle, self.cfg.out_sums, self.cfg.out_runspec, self.cfg.out_guide)

    # ---- helpers (analysis metadata)
    def _roles_map(self, paths: List[str]) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for p in paths:
            roles: List[str] = []
            lp = p.lower()
            if "/tests/" in lp or lp.startswith("tests/") or lp.endswith("_test.py"): roles.append("tests")
            if lp.endswith(".sh"): roles.append("script")
            if lp.endswith(".sql"): roles.append("sql")
            if lp.endswith(".py"):
                roles.append("python")
                if lp.endswith("__main__.py"): roles.append("entrypoint")
            if "/bin/" in lp or "/scripts/" in lp: roles.append("script")
            if lp.endswith("setup.py") or "/build/" in lp or "/dist/" in lp: roles.append("build")
            if roles: out[p] = roles
        return out

    def _scan_entrypoints(self, text_map: Dict[str, str]) -> List[Dict[str, str]]:
        entries = []
        for p, t in text_map.items():
            if p.endswith(".py") and "__name__" in t and "__main__" in t:
                entries.append({"path": p, "reason": "if __name__ == '__main__'"})
            if p.endswith(".sh") and t.strip().startswith("#!"):
                entries.append({"path": p, "reason": "shebang script"})
        return entries

    # ---- GitHub publishing -----------------------------------------------------
    def _publish_codebase_to_github(self) -> None:
        pub = getattr(self.cfg, "publish", None)
        gh = getattr(pub, "github", None) if pub else None
        token = getattr(pub, "github_token", "") if pub else ""
        mode = getattr(pub, "mode", None) if pub else None
        if mode not in ("github", "both") or not gh or not token:
            return

        owner = getattr(gh, "owner", "").strip()
        repo = getattr(gh, "repo", "").strip()
        branch = getattr(gh, "branch", "main").strip() or "main"
        base_path = (getattr(gh, "base_path", "") or "").strip().lstrip("/")

        # Only when enabled
        if not getattr(pub, "publish_codebase", False):
            return

        # Walk emitted codebase files from source_root → emitted_prefix
        pushed = 0
        for p in sorted(self.cfg.source_root.rglob("*")):
            if not p.is_file():
                continue
            rel = PathOps.to_posix_rel(p, self.cfg.source_root)
            # skip binaries we don't want in GitHub
            if rel.lower().endswith(".dll"):
                _log(f"Publish: skipping '{self.cfg.emitted_prefix}{rel}' (segment/binary filter)")
                continue
            repo_path = "/".join(filter(None, [base_path, self.cfg.emitted_prefix.rstrip("/"), rel]))
            try:
                content_b64 = base64.b64encode(p.read_bytes()).decode("ascii")
                _GH.put_file(owner, repo, branch, repo_path, content_b64, token,
                             message=f"packager: update {repo_path}")
                pushed += 1
            except Exception as e:
                _log(f"Publish(GitHub): failed PUT '{repo_path}': {type(e).__name__}: {e}")

        _log(f"Publish(GitHub): repo={owner}/{repo} branch={branch} base='{base_path}' items={pushed} (code)")

    def _publish_outputs_to_github(self) -> None:
        """
        Publish only:
          - assistant_handoff.v1.json
          - superbundle.run.json
          - split parts + index (when publish_transport=True)

        Never publish:
          - design_manifest.SHA256SUMS
          - monolithic design_manifest.jsonl  (Contents API size limits)
        """
        pub = getattr(self.cfg, "publish", None)
        gh = getattr(pub, "github", None) if pub else None
        token = getattr(pub, "github_token", "") if pub else ""
        mode = getattr(pub, "mode", None) if pub else None

        if mode not in ("github", "both") or not gh or not token:
            _log("Publish(GitHub): outputs disabled or missing credentials; skipping")
            return

        owner = getattr(gh, "owner", "").strip()
        repo = getattr(gh, "repo", "").strip()
        branch = getattr(gh, "branch", "main").strip() or "main"
        base_path = (getattr(gh, "base_path", "") or "").strip().lstrip("/")

        out_dir: Path = self.cfg.out_bundle.parent  # e.g., .../output/design_manifest
        repo_dir_prefix = "/".join(filter(None, [base_path, "output", out_dir.name]))

        # Respect publish_handoff flag for the two JSONs
        targets: List[Path] = []
        if getattr(pub, "publish_handoff", False):
            for f in (self.cfg.out_guide, self.cfg.out_runspec):
                if f.exists() and f.is_file():
                    targets.append(f)

        # Respect publish_transport flag for parts/index
        if getattr(pub, "publish_transport", False):
            for p in sorted(out_dir.rglob("*")):
                if not p.is_file():
                    continue
                name = p.name
                # Never push monolith or sums
                if name == self.cfg.out_bundle.name:
                    continue
                if name == self.cfg.out_sums.name:
                    continue
                # already added core JSONs above
                if p == self.cfg.out_guide or p == self.cfg.out_runspec:
                    continue
                # Only include parts + index or any other non-monolith outputs
                targets.append(p)

        # Final list & log (show first 10 names for brevity)
        names = [t.name for t in targets]
        _log(f"Publish(GitHub): outputs selected ({len(targets)}): {names[:10]}{' ...' if len(names) > 10 else ''}")

        pushed = 0
        for fpath in targets:
            try:
                rel = fpath.relative_to(out_dir).as_posix()
                repo_path = f"{repo_dir_prefix}/{rel}"
                content_b64 = base64.b64encode(fpath.read_bytes()).decode("ascii")
                _GH.put_file(owner, repo, branch, repo_path, content_b64, token,
                             message=f"packager: update {repo_path}")
                pushed += 1
            except Exception as e:
                _log(f"Publish(GitHub): failed PUT '{fpath.name}': {type(e).__name__}: {e}")

        _log(f"Publish(GitHub): repo={owner}/{repo} branch={branch} base='{base_path}' items={pushed} (outputs)")



