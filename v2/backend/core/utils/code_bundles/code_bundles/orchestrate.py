# src/packager/core/orchestrator.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any
import base64, json, hashlib

# ----- robust imports: absolute first, then relative fallback -----
try:
    from packager.core.config import PackConfig
    from packager.core.paths import PathOps
    from packager.core.discovery import DiscoveryEngine, DiscoveryConfig
    from packager.io.manifest_writer import BundleWriter
    from packager.io.runspec_writer import RunSpecWriter
    from packager.io.guide_writer import GuideWriter
    from packager.languages.python.plugin import PythonAnalyzer
except ImportError:  # allow running the file without -m, or odd sys.path
    from .config import PackConfig
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
    from normalize import NormalizationRules, apply_normalization as _apply_normalization

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

# Optional external publisher function (do NOT import non-existent classes)
try:
    from packager.io.publisher import publish as _external_publish  # type: ignore
except Exception:
    _external_publish = None  # gracefully absent


def _log(msg: str) -> None:
    print(f"[packager] {msg}", flush=True)


@dataclass
class PackagerResult:
    out_bundle: Path
    out_sums: Path
    out_runspec: Path
    out_guide: Path


class SourceIngestor:
    """Copies an external tree into codebase/ honoring excludes and globs."""
    def __init__(self, cfg: PackConfig) -> None:
        self.cfg = cfg

    def ingest(self, external_root: Path) -> List[Path]:
        ext = external_root.resolve()
        _log(f"Ingestion: scanning '{ext}'")
        eng = DiscoveryEngine(DiscoveryConfig(
            root=ext,
            segment_excludes=self.cfg.segment_excludes,
            include_globs=self.cfg.include_globs,
            exclude_globs=self.cfg.exclude_globs,
            case_insensitive=self.cfg.effective_case_insensitive(),
            follow_symlinks=self.cfg.follow_symlinks,
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


class FileDiscovery:
    """Deterministic discovery with depth-aware segment excludes and globs."""
    def __init__(self, cfg: PackConfig) -> None:
        self.cfg = cfg

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
        _log(f"Discovery: {len(paths)} files under '{self.cfg.source_root}'")
        return paths


class NormalizerAdapter:
    """Wraps normalization to return (path, bytes, sha256) tuples."""
    def __init__(self, rules: Optional[NormalizationRules]) -> None:
        self.rules = rules

    def normalize(self, path_bytes: List[Tuple[str, bytes]]) -> List[Tuple[str, bytes, str]]:
        _log(f"Normalize: applying rules to {len(path_bytes)} files")
        # Build FileRec-like inputs without relying on top-level import success
        recs_in = [_FileRec(path=p, data=b, sha256="") for p, b in path_bytes]
        recs_out = _apply_normalization(recs_in, rules=self.rules) if self.rules is not None else recs_in
        out = [(fr.path, fr.data, getattr(fr, "sha256", hashlib.sha256(fr.data).hexdigest())) for fr in recs_out]
        _log(f"Normalize: complete ({len(out)} files)")
        return out


class PromptEmbedder:
    """Embeds prompts from fs_dir, zip (path or bytes), or inline dict."""
    def __init__(self, cfg: PackConfig) -> None:
        self.cfg = cfg

    def build(self) -> Optional[dict]:
        src = self.cfg.prompts
        if src is None or self.cfg.prompt_mode == "omit":
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


class Packager:
    """Core orchestrator that writes the bundle, checksums, run spec, and guide; then pushes code files to GitHub."""
    def __init__(self, cfg: PackConfig, rules: Optional[NormalizationRules]) -> None:
        self.cfg = cfg
        self.rules = rules
        self.discovery = FileDiscovery(cfg)
        self.normalizer = NormalizerAdapter(rules)
        self.bundle = BundleWriter(cfg.out_bundle)
        self.run_writer = RunSpecWriter(cfg.out_runspec)
        self.guide_writer = GuideWriter(cfg.out_guide)

    def run(self, external_source: Optional[Path] = None) -> PackagerResult:
        _log("Packager: start")
        # 0) Optional ingestion
        if external_source is not None:
            _log(f"Packager: ingest external source '{external_source}'")
            SourceIngestor(self.cfg).ingest(external_source)
        else:
            _log("Packager: no external source provided; using existing codebase/")

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

        # 7) Write files
        _log(f"Write: emitting bundle to '{self.cfg.out_bundle}'")
        self.bundle.write(records)

        runspec = RunSpecWriter.build_snapshot(
            self.cfg,
            {"source_root": str(self.cfg.source_root), "emitted_prefix": self.cfg.emitted_prefix},
            prompts_public,
        )
        _log(f"Write: run-spec → '{self.cfg.out_runspec}'")
        self.run_writer.write(runspec)

        reading_order = [
            {"path": "assistant_handoff.v1.json", "why": "Start here"},
            {"path": "analysis/contents_index.json", "why": "Quick file inventory"},
            {"path": "analysis/ldt.json", "why": "Code structure (Python)"},
            {"path": "analysis/roles.json", "why": "File roles"},
            {"path": "analysis/entrypoints.json", "why": "Runnable entrypoints"},
        ]
        _log(f"Write: guide → '{self.cfg.out_guide}'")
        self.guide_writer.write(reading_order, self.cfg, prompts_public)

        # Checksums
        sums_inputs = [
            (self.cfg.out_bundle.name, self.cfg.out_bundle.read_bytes()),
            (self.cfg.out_runspec.name, self.cfg.out_runspec.read_bytes()),
            (self.cfg.out_guide.name, self.cfg.out_guide.read_bytes()),
        ]
        _log(f"Write: checksums → '{self.cfg.out_sums}' ({len(sums_inputs)} artifacts)")
        self.bundle.write_sums(self.cfg.out_sums, sums_inputs)

        # 8) PUBLISH: always push raw code files to GitHub first
        try:
            self._publish_code_to_github(records)
        except Exception as e:
            _log(f"Publish(GitHub): code push failed: {type(e).__name__}: {e}")

        # 9) Optional external publisher (handoff/transport/etc.) AFTER code push
        if callable(_external_publish):
            try:
                _external_publish(self.cfg, records)  # type: ignore[arg-type]
            except Exception as e:
                _log(f"External publish skipped due to error: {type(e).__name__}: {e}")

        _log("Packager: done")
        return PackagerResult(self.cfg.out_bundle, self.cfg.out_sums, self.cfg.out_runspec, self.cfg.out_guide)

    # ---- helpers: roles/entrypoints
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

    # ---- internal GitHub publisher (raw code files) --------------------------
    def _publish_code_to_github(self, records: List[dict]) -> None:
        pub = getattr(self.cfg, "publish", None)
        gh = getattr(pub, "github", None) if pub else None
        token = getattr(pub, "github_token", "") if pub else ""
        mode = getattr(pub, "mode", None) if pub else None

        if mode not in ("github", "both") or not gh or not token:
            _log("Publish(GitHub): disabled or missing credentials; skipping code push")
            return

        owner = getattr(gh, "owner", "").strip()
        repo = getattr(gh, "repo", "").strip()
        branch = getattr(gh, "branch", "main").strip() or "main"
        base_path = (getattr(gh, "base_path", "") or "").strip().lstrip("/")

        emitted_prefix = self.cfg.emitted_prefix.rstrip("/") + "/"

        # Decide which files to push: only the raw code paths under emitted_prefix
        def is_binary_path(p: str) -> bool:
            p_lower = p.lower()
            # conservative skip list for obvious binaries
            for ext in (".dll", ".so", ".dylib", ".exe", ".bin", ".pdf", ".zip"):
                if p_lower.endswith(ext):
                    return True
            return False

        to_push: List[Tuple[str, str]] = []  # (repo_path, content_b64)
        for rec in records:
            if rec.get("type") != "file":
                continue
            rel = rec.get("path", "")
            if not rel.startswith(emitted_prefix):
                continue
            if is_binary_path(rel):
                _log(f"Publish: skipping '{rel}' (segment/binary filter)")
                continue
            content_b64 = rec.get("content_b64")
            if not isinstance(content_b64, str):
                continue

            repo_rel = f"{base_path}/{rel}".strip("/") if base_path else rel
            to_push.append((repo_rel, content_b64))

        if not to_push:
            _log("Publish(GitHub): no code files selected to push")
            return

        # Push via GitHub Contents API (create or update)
        pushed = 0
        for repo_path, content_b64 in to_push:
            try:
                self._github_put_file(owner, repo, branch, repo_path, content_b64, token,
                                      message=f"packager: update {repo_path}")
                pushed += 1
            except Exception as e:
                _log(f"Publish(GitHub): failed PUT '{repo_path}': {type(e).__name__}: {e}")

        _log(f"Publish(GitHub): repo={owner}/{repo} branch={branch} base='{base_path}' items={pushed} (code)")

    # ---- minimal GitHub API helpers (urllib, no third-party deps) ------------
    def _github_get_sha(self, owner: str, repo: str, branch: str, repo_path: str, token: str) -> Optional[str]:
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError
        from urllib.parse import quote

        url_path = quote(repo_path.lstrip("/"), safe="/")
        url = f"https://api.github.com/repos/{owner}/{repo}/contents/{url_path}?ref={branch}"
        req = Request(url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "code-bundles-packager"
        })
        try:
            with urlopen(req) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                sha = data.get("sha")
                return sha
        except HTTPError as e:
            if e.code == 404:
                return None
            raise

    def _github_put_file(
        self,
        owner: str,
        repo: str,
        branch: str,
        repo_path: str,
        content_b64: str,
        token: str,
        message: str = "update via packager",
    ) -> None:
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError
        from urllib.parse import quote
        import contextlib

        sha = self._github_get_sha(owner, repo, branch, repo_path, token)
        payload = {
            "message": message,
            "content": content_b64,
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha

        body = json.dumps(payload).encode("utf-8")
        url_path = quote(repo_path.lstrip("/"), safe="/")
        url = f"https://api.github.com/repos/{owner}/{repo}/contents/{url_path}"

        req = Request(url, data=body, method="PUT", headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "code-bundles-packager"
        })
        with contextlib.closing(urlopen(req)) as resp:
            # 201 (created) or 200 (updated) are both fine
            if resp.status not in (200, 201):
                raise RuntimeError(f"unexpected status {resp.status}")



