from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any
import base64, json, hashlib, os, fnmatch, urllib.request, urllib.error

# ----- robust imports: absolute first, then relative fallback -----
try:
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
    try:
        from ..languages.python.plugin import PythonAnalyzer
    except Exception:
        PythonAnalyzer = None  # best-effort, optional

# Normalization rules (optional). If missing, we no-op normalize.
try:
    from .normalize import NormalizationRules, apply_normalization as _apply_normalization
except Exception:
    NormalizationRules = None  # type: ignore
    def _apply_normalization(recs, rules=None):  # type: ignore
        return recs

# FileRec shim for normalization input/output
try:
    from bundle_io import FileRec as _FileRec  # path:str, data:bytes, sha256:str
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


# ---------------- GitHub client (self-contained, minimal) ---------------------

class _GitHubClient:
    def __init__(self, *, token: str, owner: str, repo: str, branch: str = "main", base_path: str = "") -> None:
        self.token = token
        self.owner = owner
        self.repo = repo
        self.branch = branch
        self.base_path = (base_path.strip("/") + "/") if base_path else ""

    def _api_url(self, repo_path: str) -> str:
        repo_path = repo_path.lstrip("/")
        return f"https://api.github.com/repos/{self.owner}/{self.repo}/contents/{repo_path}"

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"token {self.token}",
            "User-Agent": "code-bundles-packager",
            "Accept": "application/vnd.github+json",
        }

    def _get_sha_if_exists(self, repo_path: str) -> Optional[str]:
        url = self._api_url(repo_path) + f"?ref={self.branch}"
        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                obj = json.loads(r.read().decode("utf-8") or "{}")
                return obj.get("sha")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
        except urllib.error.URLError:
            return None  # treat as not-found so PUT will try create

    def put_file(self, *, repo_path: str, data: bytes, message: str) -> bool:
        repo_path = repo_path.lstrip("/")
        if self.base_path:
            repo_path = self.base_path + repo_path
        url = self._api_url(repo_path)

        payload = {
            "message": message,
            "content": base64.b64encode(data).decode("ascii"),
            "branch": self.branch,
        }
        sha = self._get_sha_if_exists(repo_path)
        if sha:
            payload["sha"] = sha

        req = urllib.request.Request(
            url,
            headers={**self._headers(), "Content-Type": "application/json"},
            data=json.dumps(payload).encode("utf-8"),
            method="PUT",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return 200 <= r.status < 300
        except Exception as e:
            _log(f"Publish(GitHub): PUT failed for '{repo_path}': {type(e).__name__}: {e}")
            return False


# ----------------- Source ingest & discovery (stable) -------------------------

class SourceIngestor:
    """Copies an external tree into codebase/ honoring excludes and globs."""
    def __init__(self, cfg) -> None:
        self.cfg = cfg

    def _effective_case_insensitive(self) -> bool:
        return bool(getattr(self.cfg, "case_insensitive", False))

    def ingest(self, external_root: Path) -> List[Path]:
        ext = external_root.resolve()
        _log(f"Ingestion: scanning '{ext}'")
        eng = DiscoveryEngine(DiscoveryConfig(
            root=ext,
            segment_excludes=tuple(getattr(self.cfg, "segment_excludes", ()) or ()),
            include_globs=tuple(getattr(self.cfg, "include_globs", ()) or ()),
            exclude_globs=tuple(getattr(self.cfg, "exclude_globs", ()) or ()),
            case_insensitive=self._effective_case_insensitive(),
            follow_symlinks=bool(getattr(self.cfg, "follow_symlinks", False)),
        ))
        paths = eng.discover()
        _log(f"Ingestion: discovered {len(paths)} files to copy")

        # Clear target (keep folder)
        _log(f"Ingestion: clearing destination '{self.cfg.source_root}'")
        Path(self.cfg.source_root).mkdir(parents=True, exist_ok=True)
        for p in sorted(Path(self.cfg.source_root).rglob("*"), reverse=True):
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
            dp = Path(self.cfg.source_root) / rel
            dp.parent.mkdir(parents=True, exist_ok=True)
            dp.write_bytes(sp.read_bytes())
            copied.append(dp)
        _log(f"Ingestion: copied {len(copied)} files into '{self.cfg.source_root}'")
        return copied


class FileDiscovery:
    """Deterministic discovery with depth-aware segment excludes and globs."""
    def __init__(self, cfg) -> None:
        self.cfg = cfg

    def _effective_case_insensitive(self) -> bool:
        return bool(getattr(self.cfg, "case_insensitive", False))

    def discover(self) -> List[Path]:
        eng = DiscoveryEngine(DiscoveryConfig(
            root=Path(self.cfg.source_root),
            segment_excludes=tuple(getattr(self.cfg, "segment_excludes", ()) or ()),
            include_globs=tuple(getattr(self.cfg, "include_globs", ()) or ()),
            exclude_globs=tuple(getattr(self.cfg, "exclude_globs", ()) or ()),
            case_insensitive=self._effective_case_insensitive(),
            follow_symlinks=bool(getattr(self.cfg, "follow_symlinks", False)),
        ))
        paths = eng.discover()
        _log(f"Discovery: {len(paths)} files under '{self.cfg.source_root}'")
        return paths


class NormalizerAdapter:
    """Wraps normalization to return (path, bytes, sha256) tuples."""
    def __init__(self, rules) -> None:
        self.rules = rules

    def normalize(self, path_bytes: List[Tuple[str, bytes]]) -> List[Tuple[str, bytes, str]]:
        _log(f"Normalize: applying rules to {len(path_bytes)} files")
        if not self.rules or _apply_normalization is None:
            # no-op normalization with computed sha256
            out = [(p, b, hashlib.sha256(b).hexdigest()) for (p, b) in path_bytes]
            _log(f"Normalize: complete ({len(out)} files)")
            return out

        # Build FileRec-like inputs without relying on top-level import success
        recs_in = [_FileRec(path=p, data=b, sha256="") for p, b in path_bytes]
        recs_out = _apply_normalization(recs_in, rules=self.rules)
        out = [(fr.path, fr.data, getattr(fr, "sha256", hashlib.sha256(fr.data).hexdigest())) for fr in recs_out]
        _log(f"Normalize: complete ({len(out)} files)")
        return out


class PromptEmbedder:
    """Embeds prompts from fs_dir, zip (path or bytes), or inline dict."""
    def __init__(self, cfg) -> None:
        self.cfg = cfg

    def build(self) -> Optional[dict]:
        src = getattr(self.cfg, "prompts", None)
        mode = getattr(self.cfg, "prompt_mode", "none")
        if src is None or mode in ("omit", "none"):
            _log("Prompts: none (omitted)")
            return None

        files: List[Tuple[str, bytes]] = []

        if getattr(src, "kind", None) == "fs_dir":
            base = src.value if isinstance(src.value, Path) else Path(str(src.value))
            if not base.exists():
                _log(f"Prompts: directory not found '{base}', skipping")
                return None
            for p in sorted(base.rglob("*")):
                if p.is_file():
                    files.append((p.relative_to(base).as_posix(), p.read_bytes()))
            _log(f"Prompts: loaded {len(files)} files from '{base}'")

        elif getattr(src, "kind", None) == "zip":
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

        elif getattr(src, "kind", None) == "inline":
            if isinstance(src.value, dict):
                for k, v in src.value.items():
                    data = v.encode("utf-8") if isinstance(v, str) else bytes(v)
                    files.append((str(k).lstrip("./"), data))
                _log(f"Prompts: loaded {len(files)} inline entries")
            else:
                _log("Prompts: inline value not a dict, skipping")
                return None
        else:
            _log(f"Prompts: unknown kind '{getattr(src, 'kind', None)}', skipping")
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


# --------------------------------- Packager -----------------------------------

class Packager:
    """Core orchestrator that writes the bundle, checksums, run spec, and guide; then publishes."""
    def __init__(self, cfg, rules) -> None:
        self.cfg = cfg
        self.rules = rules
        self.discovery = FileDiscovery(cfg)
        self.normalizer = NormalizerAdapter(rules)
        self.bundle = BundleWriter(Path(self.cfg.out_bundle))
        self.run_writer = RunSpecWriter(Path(self.cfg.out_runspec))
        self.guide_writer = GuideWriter(Path(self.cfg.out_guide))

    # helper: remove monolith in GitHub mode
    def _maybe_remove_monolith(self) -> None:
        mode = getattr(getattr(self.cfg, "publish", None), "mode", "local")
        preserve = bool(getattr(getattr(self.cfg, "transport", None), "preserve_monolith", False))
        if mode in ("github", "both") and not preserve:
            try:
                Path(self.cfg.out_bundle).unlink(missing_ok=True)
                _log(f"Cleanup: removed monolithic bundle (GitHub mode, preserve_monolith=False) → '{self.cfg.out_bundle}'")
            except Exception:
                pass

    def _should_clean_output_dir(self) -> bool:
        pub = getattr(self.cfg, "publish", None)
        return bool(getattr(pub, "clean_before_publish", False))

    def _clean_output_dir(self) -> None:
        out_dir = Path(self.cfg.out_bundle).parent
        _log(f"Output: cleaning '{out_dir}' before run")
        out_dir.mkdir(parents=True, exist_ok=True)
        for p in sorted(out_dir.rglob("*"), reverse=True):
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

    def run(self, external_source: Optional[Path] = None) -> PackagerResult:
        _log("Packager: start")

        # Optional: clean output dir (requested)
        if self._should_clean_output_dir():
            self._clean_output_dir()

        # Optional ingestion
        if external_source is not None:
            _log(f"Packager: ingest external source '{external_source}'")
            SourceIngestor(self.cfg).ingest(external_source)
        else:
            _log("Packager: no external source provided; using existing codebase/")

        # Discover
        paths = self.discovery.discover()

        # Read + normalize (for bundle/analysis only; code push reads from disk)
        path_bytes: List[Tuple[str, bytes]] = []
        for p in paths:
            rel = PathOps.to_posix_rel(p, Path(self.cfg.source_root))
            ep = PathOps.emitted_path(rel, self.cfg.emitted_prefix)
            try:
                data = p.read_bytes()
            except Exception:
                _log(f"Read: skip unreadable file '{p}'")
                continue
            path_bytes.append((ep, data))
        _log(f"Read: collected {len(path_bytes)} files for normalization")
        normed = self.normalizer.normalize(path_bytes)

        # Build JSONL records
        records: List[dict] = []
        prefix = self.cfg.emitted_prefix if str(self.cfg.emitted_prefix).endswith("/") else str(self.cfg.emitted_prefix) + "/"
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

        # Python analysis artifacts (best-effort)
        if PythonAnalyzer is not None:
            try:
                _log(f"Analysis: running Python analyzers on {len(python_payloads)} files")
                py = PythonAnalyzer()
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
        else:
            _log("Analysis: PythonAnalyzer unavailable; skipping")

        # Extra analysis summaries
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

        # Optional prompts
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

        # Write bundle (monolith), then remove in GitHub mode
        _log(f"Write: emitting bundle to '{self.cfg.out_bundle}'")
        self.bundle.write(records)
        self._maybe_remove_monolith()

        # Run-spec + guide
        # Try full snapshot; if incompatible, fallback minimal
        meta = {"source_root": str(self.cfg.source_root), "emitted_prefix": self.cfg.emitted_prefix}
        try:
            # newer RunSpecWriter likely staticmethod
            runspec_obj = RunSpecWriter.build_snapshot(self.cfg, meta, prompts_public)  # type: ignore[arg-type]
        except TypeError:
            try:
                # maybe different signature
                runspec_obj = RunSpecWriter.build_snapshot(meta, self.cfg, prompts_public)  # type: ignore[misc]
            except Exception as e:
                _log(f"RunSpecWriter.build_snapshot incompatible, falling back: {type(e).__name__}: {e}")
                runspec_obj = {"provenance": meta, "artifacts": []}
        except Exception as e:
            _log(f"RunSpecWriter signature probe failed: {type(e).__name__}: {e}")
            runspec_obj = {"provenance": meta, "artifacts": []}

        _log(f"Write: run-spec → '{self.cfg.out_runspec}'")
        self.run_writer.write(runspec_obj)

        _log(f"Write: guide → '{self.cfg.out_guide}'")
        self.guide_writer.write([
            {"path": "assistant_handoff.v1.json", "why": "Start here"},
            {"path": "analysis/contents_index.json", "why": "Quick file inventory"},
            {"path": "analysis/ldt.json", "why": "Code structure (Python)"},
            {"path": "analysis/roles.json", "why": "File roles"},
            {"path": "analysis/entrypoints.json", "why": "Runnable entrypoints"},
        ], self.cfg, prompts_public)

        # Checksums (skip for GitHub mode)
        pub_mode = getattr(getattr(self.cfg, "publish", None), "mode", "local")
        if pub_mode in ("github", "both"):
            _log("Write: checksums → skipped for GitHub mode")
        else:
            sums_inputs = [
                (Path(self.cfg.out_bundle).name, Path(self.cfg.out_bundle).read_bytes()),
                (Path(self.cfg.out_runspec).name, Path(self.cfg.out_runspec).read_bytes()),
                (Path(self.cfg.out_guide).name, Path(self.cfg.out_guide).read_bytes()),
            ]
            self.bundle.write_sums(Path(self.cfg.out_sums), sums_inputs)
            _log(f"Write: checksums → '{self.cfg.out_sums}' ({len(sums_inputs)} artifacts)")

        # ----------------------- Publishing -----------------------
        self._publish(paths)

        _log("Packager: done")
        return PackagerResult(Path(self.cfg.out_bundle), Path(self.cfg.out_sums), Path(self.cfg.out_runspec), Path(self.cfg.out_guide))

    # ---- helpers
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

    # ----------------- Publishing implementation -----------------

    def _publish(self, discovered_paths: List[Path]) -> None:
        pub = getattr(self.cfg, "publish", None)
        if not pub:
            _log("Publish: no publish options in cfg → skipping")
            return

        mode = getattr(pub, "mode", "local")
        if mode not in ("github", "both"):
            _log(f"Publish: mode '{mode}' → skipping remote publish")
            return

        gh = getattr(pub, "github", None)
        token = getattr(pub, "github_token", "") or ""
        if not (gh and token and getattr(gh, "owner", "") and getattr(gh, "repo", "")):
            _log("Publish(GitHub): missing token/owner/repo → skipping")
            return

        client = _GitHubClient(
            token=token,
            owner=getattr(gh, "owner", ""),
            repo=getattr(gh, "repo", ""),
            branch=getattr(gh, "branch", "main"),
            base_path=getattr(gh, "base_path", "") or getattr(gh, "base", "") or "",
        )

        # 1) Outputs: ONLY assistant_handoff + superbundle (explicit)
        outputs_repo = [
            ("output/design_manifest/assistant_handoff.v1.json", Path(self.cfg.out_guide).read_bytes()),
            ("output/design_manifest/superbundle.run.json", Path(self.cfg.out_runspec).read_bytes()),
        ]
        names = [Path(self.cfg.out_guide).name, Path(self.cfg.out_runspec).name]
        _log(f"Publish(GitHub): outputs selected (2): {names}")
        ok_out = 0
        for repo_path, data in outputs_repo:
            if client.put_file(repo_path=repo_path, data=data, message=f"Publish {repo_path}"):
                ok_out += 1
        _log(f"Publish(GitHub): outputs pushed = {ok_out}/2")

        # 2) Code mirror under output/patch_code_bundles/**
        #    Skip obvious binaries (.dll/.so/.pyd/.exe)
        blocked_ext = {".dll", ".so", ".pyd", ".exe"}
        ok_code = 0
        send_code = 0
        for p in discovered_paths:
            if p.suffix.lower() in blocked_ext:
                continue
            if not p.is_file():
                continue
            rel = p.relative_to(Path(self.cfg.source_root)).as_posix()
            repo_path = f"output/patch_code_bundles/{rel}"
            send_code += 1
            if client.put_file(repo_path=repo_path, data=p.read_bytes(), message=f"Mirror {repo_path}"):
                ok_code += 1
        _log(f"Publish(GitHub): code files pushed = {ok_code}/{send_code}")

