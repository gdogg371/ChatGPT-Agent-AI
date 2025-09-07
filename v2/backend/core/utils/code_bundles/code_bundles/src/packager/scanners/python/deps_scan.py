# File: v2/backend/core/utils/code_bundles/code_bundles/src/packager/scanners/deps_scan.py
"""
Dependency scanner — Phase 1 (Python only, stdlib-only)

Emits per-dependency manifest records for the "deps" family so that
deps.index.summary.json can aggregate real data (ecosystems, manifests,
lockfiles, top_packages, etc.).

Supported inputs (in order of authoritativeness for versions):
  1. poetry.lock                (lockfile_kind = "poetry")
  2. requirements*.txt          (pip-style constraints)
  3. pyproject.toml             (PEP 621 / tool.poetry.*)
  4. setup.cfg                  (install_requires / extras_require)

Output record schema (one per dependency):
{
  "family": "deps",
  "ecosystem": "pypi",
  "package": "<name>",
  "version": "<semver or pinned>",         # omitted if unknown
  "manifest": "<basename of manifest>",    # e.g., "pyproject.toml", "requirements.txt", "setup.cfg"
  "manifest_path": "<repo_rel_posix to manifest>",
  "lockfile": "<lockfile basename>",       # e.g., "poetry.lock" (omitted if none)
  "lockfile_kind": "<poetry|pip|...>",     # omitted if none
  "source": "<poetry_lock|requirements|pyproject|setup_cfg>"
}

Determinism:
- Records are deduped by (ecosystem, package).
- When multiple sources specify the same package, the "prefer_version_source"
  order in config decides which version wins. Defaults:
    poetry_lock > requirements > pyproject > setup_cfg
- Output is stably sorted by (ecosystem, package, manifest_path).

Config (packager.yml):
analysis:
  deps:
    enabled: true
    ecosystems: [pypi]
    python:
      enabled: true
      sources:
        poetry_lock: true
        pyproject: true
        requirements: true
        setup_cfg: true
      prefer_version_source: [poetry_lock, requirements, pyproject, setup_cfg]
      parse_limits:
        max_requirements_lines: 10000
        max_packages: 10000
"""

from __future__ import annotations

import configparser
import io
import json
import os
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

try:
    import tomllib  # Python 3.11+
except Exception:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

__all__ = ["scan_dependencies"]


def scan_dependencies(*, repo_root: str | Path, cfg=None) -> List[dict]:
    """
    Scan the repository for Python dependency sources and emit "deps" records.

    Parameters
    ----------
    repo_root : str | Path
        The root directory of the source repository.
    cfg : Any
        Optional config object (packager config). If present, we read:
          - analysis.deps.ecosystems
          - analysis.deps.python.enabled
          - analysis.deps.python.sources.{poetry_lock,pyproject,requirements,setup_cfg}
          - analysis.deps.python.prefer_version_source
          - analysis.deps.python.parse_limits.{max_requirements_lines,max_packages}
          - segment_excludes (top-level)

    Returns
    -------
    List[dict] : a list of dependency records (see schema in module docstring).
    """
    repo_root = Path(repo_root).resolve()

    # Config defaults (safe, deterministic)
    cfg_ecosystems = {"pypi"}
    py_enabled = True
    sources_enabled = {
        "poetry_lock": True,
        "pyproject": True,
        "requirements": True,
        "setup_cfg": True,
    }
    prefer_order = ["poetry_lock", "requirements", "pyproject", "setup_cfg"]
    max_req_lines = 10_000
    max_pkgs = 10_000
    segment_excludes = {
        ".git", "__pycache__", ".mypy_cache", ".pytest_cache", ".idea", ".vscode",
        "node_modules", "dist", "build", ".venv", "venv", "output", "Archive", "archive",
        "databases", "logs", "software", "terraform", "tests", "tests_adhoc", "tests_adhoc2",
    }

    # Read config if provided
    try:
        if cfg is not None:
            # ecosystems
            ecos = _dig(cfg, ["analysis", "deps", "ecosystems"])
            if isinstance(ecos, (list, tuple, set)):
                cfg_ecosystems = {str(x).lower() for x in ecos}
            # python.enabled
            pe = _dig(cfg, ["analysis", "deps", "python", "enabled"])
            if isinstance(pe, bool):
                py_enabled = pe
            # sources
            srcs = _dig(cfg, ["analysis", "deps", "python", "sources"])
            if isinstance(srcs, dict):
                for k in list(sources_enabled.keys()):
                    v = srcs.get(k)
                    if isinstance(v, bool):
                        sources_enabled[k] = v
            # prefer order
            pref = _dig(cfg, ["analysis", "deps", "python", "prefer_version_source"])
            if isinstance(pref, (list, tuple)) and pref:
                prefer_order = [str(x) for x in pref]
            # limits
            lim = _dig(cfg, ["analysis", "deps", "python", "parse_limits"])
            if isinstance(lim, dict):
                max_req_lines = int(lim.get("max_requirements_lines") or max_req_lines)
                max_pkgs = int(lim.get("max_packages") or max_pkgs)
            # segment excludes
            seg = getattr(cfg, "segment_excludes", None)
            if isinstance(seg, (list, tuple, set)):
                segment_excludes = {str(x) for x in seg} or segment_excludes
    except Exception:
        # Non-fatal; use defaults
        pass

    # Ecosystem gating: only "pypi" for Phase 1
    if "pypi" not in cfg_ecosystems or not py_enabled:
        return []

    # Collect candidates while respecting segment excludes
    candidates = _find_python_dep_files(repo_root, segment_excludes, sources_enabled)

    # Parse and merge with precedence
    merger = _DepMerger(prefer_order=prefer_order, max_pkgs=max_pkgs)

    # poetry.lock
    for lock_path in candidates.poetry_locks:
        for name, version in _parse_poetry_lock(lock_path):
            merger.add(
                package=name,
                version=version,
                source="poetry_lock",
                manifest="pyproject.toml" if (lock_path.parent / "pyproject.toml").exists() else None,
                manifest_path=_relposix(repo_root, lock_path.parent / ("pyproject.toml" if (lock_path.parent / "pyproject.toml").exists() else "")),
                lockfile="poetry.lock",
                lockfile_kind="poetry",
            )

    # requirements*.txt
    for req_path in candidates.requirements:
        for name, version in _parse_requirements(req_path, max_lines=max_req_lines):
            merger.add(
                package=name,
                version=version,
                source="requirements",
                manifest=req_path.name,
                manifest_path=_relposix(repo_root, req_path),
            )

    # pyproject.toml
    for pyp_path in candidates.pyprojects:
        for name, version in _parse_pyproject(pyp_path):
            merger.add(
                package=name,
                version=version,
                source="pyproject",
                manifest="pyproject.toml",
                manifest_path=_relposix(repo_root, pyp_path),
            )

    # setup.cfg
    for cfg_path in candidates.setup_cfgs:
        for name, version in _parse_setup_cfg(cfg_path):
            merger.add(
                package=name,
                version=version,
                source="setup_cfg",
                manifest="setup.cfg",
                manifest_path=_relposix(repo_root, cfg_path),
            )

    # Produce records
    out: List[dict] = []
    for pkg, info in merger.iter_sorted():
        rec = {
            "family": "deps",
            "ecosystem": "pypi",
            "package": pkg,
        }
        if info.version:
            rec["version"] = info.version
        if info.manifest:
            rec["manifest"] = info.manifest
        if info.manifest_path:
            rec["manifest_path"] = info.manifest_path
        if info.lockfile:
            rec["lockfile"] = info.lockfile
        if info.lockfile_kind:
            rec["lockfile_kind"] = info.lockfile_kind
        rec["source"] = info.source
        out.append(rec)

    return out


# ──────────────────────────────────────────────────────────────────────────────
# Data collection
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class _Candidates:
    poetry_locks: List[Path]
    pyprojects: List[Path]
    requirements: List[Path]
    setup_cfgs: List[Path]


def _find_python_dep_files(repo_root: Path, segment_excludes: set[str], sources_enabled: Dict[str, bool]) -> _Candidates:
    poetry_locks: List[Path] = []
    pyprojects: List[Path] = []
    requirements: List[Path] = []
    setup_cfgs: List[Path] = []

    for root, dirs, files in os.walk(repo_root, topdown=True):
        # Prune excluded directories in-place
        dirs[:] = [d for d in dirs if d not in segment_excludes]

        if sources_enabled.get("poetry_lock", True) and "poetry.lock" in files:
            poetry_locks.append(Path(root) / "poetry.lock")
        if sources_enabled.get("pyproject", True) and "pyproject.toml" in files:
            pyprojects.append(Path(root) / "pyproject.toml")
        if sources_enabled.get("setup_cfg", True) and "setup.cfg" in files:
            setup_cfgs.append(Path(root) / "setup.cfg")

        if sources_enabled.get("requirements", True):
            for fname in files:
                if _is_requirements_file(fname):
                    requirements.append(Path(root) / fname)

    # Deterministic ordering
    poetry_locks.sort()
    pyprojects.sort()
    requirements.sort()
    setup_cfgs.sort()
    return _Candidates(poetry_locks, pyprojects, requirements, setup_cfgs)


def _is_requirements_file(filename: str) -> bool:
    if not filename.lower().endswith(".txt"):
        return False
    base = filename.lower()
    return base == "requirements.txt" or base.startswith("requirements.")


# ──────────────────────────────────────────────────────────────────────────────
# Parsers (stdlib-only)
# ──────────────────────────────────────────────────────────────────────────────

def _parse_poetry_lock(path: Path) -> Iterator[Tuple[str, Optional[str]]]:
    """
    Parse poetry.lock (TOML since Poetry 1.2). We only need (name, version).
    Fallback to a simple regex if tomllib is unavailable or content is not TOML.
    """
    text = path.read_text(encoding="utf-8", errors="ignore")
    # Try TOML first
    if tomllib:
        try:
            data = tomllib.loads(text)
            # Poetry lock format has [[package]] arrays
            pkgs = data.get("package")
            if isinstance(pkgs, list):
                for pkg in pkgs:
                    name = pkg.get("name")
                    version = pkg.get("version")
                    if isinstance(name, str) and name.strip():
                        yield name.strip(), (version.strip() if isinstance(version, str) else None)
                return
        except Exception:
            pass
    # Fallback: line-by-line heuristic
    name = None
    version = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[[package]]"):
            if name:
                yield name, version
            name = None
            version = None
            continue
        m = re.match(r'name\s*=\s*"(.*?)"\s*$', line)
        if m:
            name = m.group(1).strip()
            continue
        m = re.match(r'version\s*=\s*"(.*?)"\s*$', line)
        if m:
            version = m.group(1).strip()
            continue
    if name:
        yield name, version


_REQ_LINE_RE = re.compile(
    r"""
    ^\s*
    (?P<name>[A-Za-z0-9_.-]+)          # package name
    (?:\[[^\]]*\])?                    # optional extras
    \s*
    (?:
        (?P<op>===|==|~=|>=|<=|!=|>|<) # optional operator
        \s*
        (?P<ver>[A-Za-z0-9*_.+-]+)     # version token
    )?
    """,
    re.VERBOSE,
)


def _parse_requirements(path: Path, *, max_lines: int = 10_000) -> Iterator[Tuple[str, Optional[str]]]:
    count = 0
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if count >= max_lines:
            break
        count += 1
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("-r", "--requirement", "-c", "--constraint", "-f", "--find-links", "--extra-index-url", "--index-url")):
            continue
        if line.startswith(("-e", "--editable")):
            # Editable install; can't extract a version deterministically
            m = re.search(r"#egg=([A-Za-z0-9_.-]+)", line)
            if m:
                yield m.group(1), None
            continue
        m = _REQ_LINE_RE.match(line)
        if not m:
            continue
        name = m.group("name")
        ver = m.group("ver")
        yield name, (ver if ver else None)


def _parse_pyproject(path: Path) -> Iterator[Tuple[str, Optional[str]]]:
    if not tomllib:
        return iter(())
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return iter(())

    # PEP 621
    proj = data.get("project") or {}
    deps = proj.get("dependencies") or []
    if isinstance(deps, list):
        for spec in deps:
            for name, ver in _extract_name_version_from_spec(spec):
                yield name, ver
    opt = proj.get("optional-dependencies") or {}
    if isinstance(opt, dict):
        for _group, items in opt.items():
            if isinstance(items, list):
                for spec in items:
                    for name, ver in _extract_name_version_from_spec(spec):
                        yield name, ver

    # Poetry tables
    tool = data.get("tool") or {}
    poetry = tool.get("poetry") or {}
    pobj = poetry.get("dependencies") or {}
    if isinstance(pobj, dict):
        for name, spec in pobj.items():
            if name.lower() == "python":
                continue
            for n, v in _extract_name_version_from_poetry_entry(name, spec):
                yield n, v
    groups = poetry.get("group") or {}
    if isinstance(groups, dict):
        for _gname, gobj in groups.items():
            if not isinstance(gobj, dict):
                continue
            gdeps = gobj.get("dependencies") or {}
            if isinstance(gdeps, dict):
                for name, spec in gdeps.items():
                    for n, v in _extract_name_version_from_poetry_entry(name, spec):
                        yield n, v


def _parse_setup_cfg(path: Path) -> Iterator[Tuple[str, Optional[str]]]:
    cp = configparser.ConfigParser()
    try:
        cp.read(path, encoding="utf-8")
    except Exception:
        return iter(())
    # install_requires
    if cp.has_option("options", "install_requires"):
        raw = cp.get("options", "install_requires")
        for line in _split_cfg_list(raw):
            for name, ver in _extract_name_version_from_spec(line):
                yield name, ver
    # extras_require (each key is a list)
    if cp.has_section("options.extras_require"):
        for key, raw in cp.items("options.extras_require"):
            for line in _split_cfg_list(raw):
                for name, ver in _extract_name_version_from_spec(line):
                    yield name, ver


# ──────────────────────────────────────────────────────────────────────────────
# Merge logic
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class _DepInfo:
    version: Optional[str]
    source: str
    manifest: Optional[str]
    manifest_path: Optional[str]
    lockfile: Optional[str]
    lockfile_kind: Optional[str]


class _DepMerger:
    def __init__(self, *, prefer_order: Sequence[str], max_pkgs: int) -> None:
        self.prefer_rank = {src: i for i, src in enumerate(prefer_order)}
        self.max_pkgs = max_pkgs
        self._data: Dict[str, _DepInfo] = {}

    def add(
        self,
        *,
        package: str,
        version: Optional[str],
        source: str,
        manifest: Optional[str] = None,
        manifest_path: Optional[Path | str] = None,
        lockfile: Optional[str] = None,
        lockfile_kind: Optional[str] = None,
    ) -> None:
        if len(self._data) >= self.max_pkgs:
            return
        name = package.strip()
        if not name:
            return
        # Normalize path to posix
        mpath = None
        if manifest_path:
            mpath = str(manifest_path).replace("\\", "/")

        info = _DepInfo(
            version=version.strip() if isinstance(version, str) and version.strip() else None,
            source=source,
            manifest=manifest,
            manifest_path=mpath,
            lockfile=lockfile,
            lockfile_kind=lockfile_kind,
        )
        existing = self._data.get(name)
        if not existing:
            self._data[name] = info
            return

        # Decide if new info should replace existing
        if self._rank(source) < self._rank(existing.source):
            self._data[name] = info
        elif self._rank(source) == self._rank(existing.source):
            # Prefer having a version over no version
            if (not existing.version) and info.version:
                self._data[name] = info
            # Prefer lockfile presence
            elif (not existing.lockfile) and info.lockfile:
                self._data[name] = info

    def _rank(self, src: str) -> int:
        return self.prefer_rank.get(src, 9999)

    def iter_sorted(self) -> Iterator[Tuple[str, _DepInfo]]:
        for pkg in sorted(self._data.keys(), key=lambda s: (s.lower(), s)):
            yield pkg, self._data[pkg]


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _relposix(root: Path, p: Path) -> Optional[str]:
    try:
        rel = os.path.relpath(p, root)
        if rel == ".":
            return None
        return rel.replace("\\", "/")
    except Exception:
        return str(p).replace("\\", "/")


def _dig(obj, path: List[str]):
    cur = obj
    for key in path:
        try:
            if isinstance(cur, dict):
                cur = cur.get(key)
            else:
                cur = getattr(cur, key)
        except Exception:
            return None
    return cur


def _split_cfg_list(raw: str) -> List[str]:
    # configparser returns a single string; split per-line and commas
    out: List[str] = []
    for line in raw.splitlines():
        for part in line.split(","):
            s = part.strip()
            if s:
                out.append(s)
    return out


def _extract_name_version_from_spec(spec: str) -> Iterator[Tuple[str, Optional[str]]]:
    """
    Parse requirement-like strings such as:
      "pack==1.2.3", "pack>=1.0", "pack~=2.0", "pack", "pack[extra]==1.0"
    Returns (name, version|None).
    """
    if not isinstance(spec, str):
        return iter(())
    m = _REQ_LINE_RE.match(spec.strip())
    if not m:
        return iter(())
    name = m.group("name")
    ver = m.group("ver")
    return iter([(name, ver if ver else None)])


def _extract_name_version_from_poetry_entry(name: str, spec) -> Iterator[Tuple[str, Optional[str]]]:
    """
    Poetry dependency table entries can be strings or tables.
      tool.poetry.dependencies:
        foo = "^1.2"
        bar = {version="~2.0", optional=true}
    """
    if isinstance(spec, str):
        # Could be a caret/range; we still record the token
        return iter([(name, spec.strip() or None)])
    if isinstance(spec, dict):
        v = spec.get("version")
        if isinstance(v, str):
            v = v.strip()
        return iter([(name, v or None)])
    return iter(())
