# File: backend/core/utils/code_bundles/code_bundles_v2/src/packager/core/orchestrator.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any
import base64, json, hashlib, os, fnmatch, urllib.request, urllib.error, importlib, sys

# ----- robust imports: absolute first, then relative fallback -----
try:
    from packager.core.paths import PathOps
    from packager.core.discovery import DiscoveryEngine, DiscoveryConfig
    from packager.io.manifest_writer import BundleWriter
    from packager.io.runspec_writer import RunSpecWriter
    from packager.io.guide_writer import GuideWriter
    from packager.languages.base import discover_language_plugins, LoadedPlugin
except ImportError:  # allow running without -m or odd sys.path
    from .paths import PathOps
    from .discovery import DiscoveryEngine, DiscoveryConfig
    from ..io.manifest_writer import BundleWriter
    from ..io.runspec_writer import RunSpecWriter
    from ..io.guide_writer import GuideWriter
    from ..languages.base import discover_language_plugins, LoadedPlugin  # type: ignore

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


# ----------------- Source ingest & discovery (stable) -------------------------

class SourceIngestor:
    """Copies an external tree into codebase/ honoring excludes and globs."""
    def __init__(self, cfg) -> None:
        self.cfg = cfg

    def _effective_case_insensitive(self) -> bool:
        return bool(getattr(self.cfg, "case_insensitive", False))

    def _effective_globs(self, kind: str, plugin_exts: Tuple[str, ...]) -> Tuple[str, ...]:
        """
        Ensure language plugin extensions are included when caller asked for sources.
        kind: 'include' | 'exclude'
        """
        globs = tuple(getattr(self.cfg, f"{kind}_globs", ()) or ())
        if not globs:
            # default to all registered plugin extensions
            if plugin_exts:
                return tuple(sorted({f"**/*{ext}" for ext in plugin_exts}))
            return globs
        # if user specified python globs, augment with .pyi; likewise no-op otherwise
        add: List[str] = []
        for ext in plugin_exts:
            patt = f"**/*{ext}"
            if any(patt.endswith(ext) and patt in globs for patt in globs):
                continue
            # If they already included a wildcard like **/*, don't add.
            if any(x.endswith("*") and x.startswith("**/") for x in globs):
                continue
            if patt not in globs:
                add.append(patt)
        return tuple(globs) + tuple(add)

    def ingest(self, external_root: Path, plugin_exts: Tuple[str, ...]) -> List[Path]:
        ext = external_root.resolve()
        _log(f"Ingestion: scanning '{ext}'")
        eng = DiscoveryEngine(DiscoveryConfig(
            root=ext,
            segment_excludes=tuple(getattr(self.cfg, "segment_excludes", ()) or ()),
            include_globs=self._effective_globs("include", plugin_exts),
            exclude_globs=self._effective_globs("exclude", plugin_exts),
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
    def __init__(self, cfg, plugin_exts: Tuple[str, ...]) -> None:
        self.cfg = cfg
        self.plugin_exts = plugin_exts

    def _effective_case_insensitive(self) -> bool:
        return bool(getattr(self.cfg, "case_insensitive", False))

    def _effective_globs(self, kind: str) -> Tuple[str, ...]:
        """
        Ensure plugin extensions are included if include_globs not specified.
        """
        globs = tuple(getattr(self.cfg, f"{kind}_globs", ()) or ())
        if not globs:
            if self.plugin_exts:
                return tuple(sorted({f"**/*{ext}" for ext in self.plugin_exts}))
            return globs
        # Augment includes with plugin exts when user specified narrow globs
        if kind == "include":
            add = [f"**/*{ext}" for ext in self.plugin_exts if f"**/*{ext}" not in globs]
            globs = tuple(globs) + tuple(add)
        return globs

    def discover(self) -> List[Path]:
        eng = DiscoveryEngine(DiscoveryConfig(
            root=Path(self.cfg.source_root),
            segment_excludes=tuple(getattr(self.cfg, "segment_excludes", ()) or ()),
            include_globs=self._effective_globs("include"),
            exclude_globs=self._effective_globs("exclude"),
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

        # ---- NEW: discover language plugins and compute default extension set
        self.plugins: List[LoadedPlugin] = discover_language_plugins()
        ext_set = set()
        for pl in self.plugins:
            for e in pl.extensions:
                if not e.startswith("."):
                    continue
                ext_set.add(e)

        self.plugin_exts: Tuple[str, ...] = tuple(sorted(ext_set))
        _log(f"Plugins: loaded {len(self.plugins)} plugin(s), extensions={self.plugin_exts or '∅'}")

        # wire discovery with plugin extensions
        self.discovery = FileDiscovery(cfg, self.plugin_exts)
        self.normalizer = NormalizerAdapter(rules)
        self.bundle = BundleWriter(Path(self.cfg.out_bundle))
        self.run_writer = RunSpecWriter(Path(self.cfg.out_runspec))
        self.guide_writer = GuideWriter(Path(self.cfg.out_guide))
        self._ingestor = SourceIngestor(cfg)

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
            self._ingestor.ingest(external_source, self.plugin_exts)
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

        # Build per-plugin payloads by extension match
        per_plugin_payload: Dict[str, List[Tuple[str, bytes]]] = {pl.name: [] for pl in self.plugins}
        text_map: Dict[str, str] = {}

        for path, data, sha in normed:
            records.append({
                "type": "file", "path": path,
                "content_b64": base64.b64encode(data).decode("ascii"),
                "sha256": sha
            })

            # route file to plugins by extension
            for pl in self.plugins:
                if any(path.endswith(ext) for ext in pl.extensions):
                    per_plugin_payload[pl.name].append((path, data))
            try:
                text_map[path] = data.decode("utf-8")
            except Exception:
                pass
        _log(f"Bundle: prepared {len(records)} JSONL records (including dir header)")

        # Language analysis artifacts (best-effort per plugin)
        for pl in self.plugins:
            payloads = per_plugin_payload.get(pl.name, [])
            if not payloads:
                continue
            try:
                _log(f"Analysis[{pl.name}]: running on {len(payloads)} files")
                artifacts = pl.analyze(payloads)
                _log(f"Analysis[{pl.name}]: produced {len(artifacts)} artifacts")
                for rel, obj in artifacts.items():
                    payload = json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
                    records.append({
                        "type": "file", "path": rel,
                        "content_b64": base64.b64encode(payload).decode("ascii"),
                        "sha256": hashlib.sha256(payload).hexdigest()
                    })
            except Exception as e:
                _log(f"Analysis[{pl.name}]: skipped due to error: {type(e).__name__}: {e}")

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
        meta = {"source_root": str(self.cfg.source_root), "emitted_prefix": self.cfg.emitted_prefix}
        try:
            runspec_obj = RunSpecWriter.build_snapshot(self.cfg, meta, prompts_public)  # type: ignore[arg-type]
        except TypeError:
            try:
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
            if lp.endswith(".py") or lp.endswith(".pyi"):
                roles.append("python")
                if lp.endswith("__main__.py"): roles.append("entrypoint")
            if lp.endswith(".ts") or lp.endswith(".tsx"):
                roles.append("typescript")
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

        # Simple GitHub push (unchanged)
        def _headers(token: str) -> Dict[str, str]:
            return {
                "Authorization": f"token {token}",
                "User-Agent": "code-bundles-packager",
                "Accept": "application/vnd.github+json",
            }

        def _api_url(owner: str, repo: str, repo_path: str) -> str:
            repo_path = repo_path.lstrip("/")
            return f"https://api.github.com/repos/{owner}/{repo}/contents/{repo_path}"

        def _get_sha_if_exists(owner: str, repo: str, repo_path: str, branch: str) -> Optional[str]:
            import urllib.request, json
            url = _api_url(owner, repo, repo_path) + f"?ref={branch}"
            req = urllib.request.Request(url, headers=_headers(token), method="GET")
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    obj = json.loads(r.read().decode("utf-8") or "{}")
                    return obj.get("sha")
            except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
                if e.code == 404:
                    return None
                raise
            except urllib.error.URLError:
                return None

        owner = getattr(gh, "owner", "")
        repo = getattr(gh, "repo", "")
        branch = getattr(gh, "branch", "main")
        base_path = getattr(gh, "base_path", "") or getattr(gh, "base", "") or ""

        # 1) Outputs
        outputs_repo = [
            ("output/design_manifest/assistant_handoff.v1.json", Path(self.cfg.out_guide).read_bytes()),
            ("output/design_manifest/superbundle.run.json", Path(self.cfg.out_runspec).read_bytes()),
        ]
        _log(f"Publish(GitHub): outputs selected (2): {[x[0] for x in outputs_repo]}")
        ok_out = 0
        for repo_path, data in outputs_repo:
            payload = {
                "message": f"Publish {repo_path}",
                "content": base64.b64encode(data).decode("ascii"),
                "branch": branch,
            }
            sha = _get_sha_if_exists(owner, repo, repo_path, branch)
            if sha:
                payload["sha"] = sha
            repo_path_eff = (base_path.strip("/") + "/" if base_path else "") + repo_path
            req = urllib.request.Request(
                _api_url(owner, repo, repo_path_eff),
                headers=_headers(token) | {"Content-Type": "application/json"},
                data=json.dumps(payload).encode("utf-8"),
                method="PUT",
            )
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    if 200 <= r.status < 300:
                        ok_out += 1
            except Exception as e:
                _log(f"Publish(GitHub): PUT failed for '{repo_path_eff}': {type(e).__name__}: {e}")
        _log(f"Publish(GitHub): outputs pushed = {ok_out}/2")

        # 2) Code mirror
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
            payload = {
                "message": f"Mirror {repo_path}",
                "content": base64.b64encode(p.read_bytes()).decode("ascii"),
                "branch": branch,
            }
            sha = _get_sha_if_exists(owner, repo, repo_path, branch)
            if sha:
                payload["sha"] = sha
            repo_path_eff = (base_path.strip("/") + "/" if base_path else "") + repo_path
            req = urllib.request.Request(
                _api_url(owner, repo, repo_path_eff),
                headers=_headers(token) | {"Content-Type": "application/json"},
                data=json.dumps(payload).encode("utf-8"),
                method="PUT",
            )
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    if 200 <= r.status < 300:
                        ok_code += 1
            except Exception as e:
                _log(f"Publish(GitHub): PUT failed for '{repo_path_eff}': {type(e).__name__}: {e}")
        _log(f"Publish(GitHub): code files pushed = {ok_code}/{send_code}")
