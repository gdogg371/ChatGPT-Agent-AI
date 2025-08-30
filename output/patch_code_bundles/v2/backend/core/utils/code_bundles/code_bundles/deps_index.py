# File: v2/backend/core/utils/code_bundles/code_bundles/deps_index.py
"""
Dependency manifest indexer (stdlib-only).

Scans common dependency files across ecosystems and emits JSONL-ready records.

Per-file record (examples)
--------------------------
Python (requirements.txt):
{
  "kind": "deps.index",
  "path": "requirements.txt",
  "ecosystem": "python",
  "manifest": "requirements.txt",
  "is_lock": false,
  "packages": [
    {"name":"fastapi","version":null,"specifier":">=0.110,<1.0","scope":"prod"},
    {"name":"uvicorn","version":"0.30.0","specifier":"==0.30.0","scope":"prod"}
  ],
  "meta": {"lines": 42}
}

Node (package.json):
{
  "kind": "deps.index",
  "path": "web/package.json",
  "ecosystem": "node",
  "manifest": "package.json",
  "is_lock": false,
  "packages": [
    {"name":"react","version":"18.2.0","scope":"prod"},
    {"name":"typescript","version":"5.5.3","scope":"dev"},
    {"name":"eslint","version":"8.57.0","scope":"dev"}
  ],
  "meta": {"name":"my-app","engines":{"node":">=18"}}
}

Docker (Dockerfile):
{
  "kind": "deps.index",
  "path": "services/api/Dockerfile",
  "ecosystem": "docker",
  "manifest": "Dockerfile",
  "is_lock": false,
  "packages": [{"name":"python","version":"3.12-slim","scope":"base"}],
  "meta": {"from_lines": 2}
}

Summary record
--------------
{
  "kind": "deps.index.summary",
  "files": 7,
  "ecosystems": {"python": 3, "node": 2, "dotnet": 1, "docker": 1},
  "manifests": {"requirements.txt": 2, "pyproject.toml": 1, "package.json": 2, "Dockerfile": 1, "MyApp.csproj": 1},
  "packages_unique": 123,
  "top_packages": [{"name":"react","ecosystem":"node","count":2}, {"name":"fastapi","ecosystem":"python","count":2}],
  "lockfiles": {"count": 3, "by_kind": {"package-lock.json": 1, "yarn.lock": 1, "poetry.lock": 1}}
}

Notes
-----
* Uses only the Python standard library. TOML parsing uses 'tomllib' (Python 3.11+).
* Paths returned are repo-relative POSIX. If your pipeline distinguishes local vs
  GitHub path modes, map 'path' before appending to the manifest.
* This is a pragmatic scanner — it won’t perfectly parse every ecosystem — but it
  captures useful metadata across many common formats.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

try:  # Python 3.11+
    import tomllib  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

RepoItem = Tuple[Path, str]  # (local_path, repo_relative_posix)
_MAX_READ_BYTES = 2 * 1024 * 1024  # 2 MiB safety cap


# ──────────────────────────────────────────────────────────────────────────────
# IO helpers
# ──────────────────────────────────────────────────────────────────────────────

def _read_text_limited(p: Path, limit: int = _MAX_READ_BYTES) -> str:
    try:
        with p.open("rb") as f:
            data = f.read(limit + 1)
        if len(data) > limit:
            data = data[:limit]
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""


# ──────────────────────────────────────────────────────────────────────────────
# Python: requirements*.txt / constraints / pyproject.toml / setup.cfg
# ──────────────────────────────────────────────────────────────────────────────

# A very loose requirement line: pkg[extra1,extra2] (op version) ; markers ...
_REQ_RE = re.compile(
    r"^\s*(?:-e\s+|--editable\s+)?"
    r"(?P<name>[A-Za-z0-9_.-]+)"
    r"(?:\[(?P<extras>[A-Za-z0-9_,.-]+)\])?"
    r"\s*(?P<op>==|>=|<=|~=|!=|>|<)?\s*(?P<version>[^\s;#]+)?",
)

def _parse_requirements_text(text: str) -> List[Dict]:
    pkgs: List[Dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Trim inline comments starting with ' #' (and common pip options)
        line = line.split(" #", 1)[0].strip()
        if line.startswith(("-", "--")):
            # skip options (-r, --hash, --index-url, etc.)
            continue
        if line.startswith(("git+", "hg+", "svn+", "bzr+")):
            # VCS URL — best-effort name extraction is unreliable; record raw
            pkgs.append({"name": line, "version": None, "specifier": None, "scope": "prod"})
            continue
        m = _REQ_RE.match(line)
        if not m:
            continue
        name = m.group("name")
        extras = m.group("extras")
        op = m.group("op")
        ver = m.group("version")
        spec = f"{op}{ver}" if op and ver else None
        pkgs.append({
            "name": name,
            "version": ver if op == "==" else None,
            "specifier": spec,
            "scope": "prod",
            "extras": extras.split(",") if extras else [],
        })
    return pkgs

def _pyproject_dependencies(data: dict) -> List[Dict]:
    pkgs: List[Dict] = []
    # PEP 621
    proj = data.get("project") or {}
    deps = proj.get("dependencies") or []
    for d in deps:
        pkgs.append(_pep_dep_to_pkg(d, scope="prod"))
    opt = proj.get("optional-dependencies") or {}
    for group, lst in opt.items():
        for d in lst or []:
            pkgs.append(_pep_dep_to_pkg(d, scope=f"optional:{group}"))
    # Poetry
    tool = data.get("tool") or {}
    poetry = tool.get("poetry") or {}
    for section, scope in (
        ("dependencies", "prod"),
        ("dev-dependencies", "dev"),
        ("group", None),  # poetry >=1.2 groups
    ):
        if section == "group":
            groups = poetry.get("group") or {}
            for gname, gval in groups.items():
                if isinstance(gval, dict):
                    for sec, sc in (("dependencies", f"optional:{gname}"),):
                        dd = gval.get(sec) or {}
                        pkgs.extend(_poetry_dep_table_to_pkgs(dd, sc))
            continue
        dd = poetry.get(section) or {}
        pkgs.extend(_poetry_dep_table_to_pkgs(dd, scope))
    return pkgs

def _pep_dep_to_pkg(dep: str, scope: str) -> Dict:
    # A very rough split: "name[extras] (spec)" OR "name (spec)"
    # Keep the raw dep string as specifier; version only if pinned "=="
    m = _REQ_RE.match(dep)
    spec = None
    version = None
    name = dep
    extras: List[str] = []
    if m:
        name = m.group("name") or dep
        op = m.group("op")
        ver = m.group("version")
        if op and ver:
            spec = f"{op}{ver}"
            if op == "==":
                version = ver
        ex = m.group("extras")
        if ex:
            extras = ex.split(",")
    return {"name": name, "version": version, "specifier": spec or dep, "scope": scope, "extras": extras}

def _poetry_dep_table_to_pkgs(tbl: dict, scope: Optional[str]) -> List[Dict]:
    out: List[Dict] = []
    for name, val in (tbl or {}).items():
        version = None
        spec = None
        extras: List[str] = []
        if isinstance(val, str):
            spec = val
            if spec.startswith(("^", "~=", "~", ">=", "<=", "==")):
                if spec.startswith("=="):
                    version = spec[2:]
        elif isinstance(val, dict):
            version = val.get("version")
            spec = version or None
            if isinstance(val.get("extras"), list):
                extras = [str(x) for x in val["extras"]]
            # markers/python/platform are ignored here
        out.append({"name": name, "version": version, "specifier": spec, "scope": scope or "prod", "extras": extras})
    return out

def _setup_cfg_deps(text: str) -> List[Dict]:
    # Parse minimal [options] install_requires and [options.extras_require]
    # Avoid ConfigParser due to continuation nuances; do a light-weight parse.
    pkgs: List[Dict] = []
    section = None
    cur_key = None
    buf: List[str] = []

    def flush():
        nonlocal buf, cur_key
        if cur_key == "install_requires":
            pkgs.extend(_parse_requirements_text("\n".join(buf)))
        elif cur_key and cur_key.startswith("extras_require:"):
            group = cur_key.split(":", 1)[1]
            extras_pkgs = _parse_requirements_text("\n".join(buf))
            for e in extras_pkgs:
                e["scope"] = f"optional:{group}"
                pkgs.append(e)
        buf = []
        cur_key = None

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            if buf:
                buf.append("")
            continue
        if line.startswith("[") and line.endswith("]"):
            # new section
            flush()
            section = line.strip("[]").strip().lower()
            continue
        if section == "options":
            if re.match(r"^\s*install_requires\s*=\s*$", line):
                flush()
                cur_key = "install_requires"
                buf = []
                continue
        if section == "options.extras_require":
            m = re.match(r"^\s*([A-Za-z0-9_.-]+)\s*=\s*$", line)
            if m:
                flush()
                cur_key = f"extras_require:{m.group(1)}"
                buf = []
                continue
        # list items (indented)
        if cur_key and re.match(r"^\s+", raw):
            item = raw.strip()
            if item.startswith(("#", ";")):
                continue
            buf.append(item)
        else:
            # unrelated line
            pass
    flush()
    return pkgs


# ──────────────────────────────────────────────────────────────────────────────
# Node: package.json / package-lock.json / yarn.lock
# ──────────────────────────────────────────────────────────────────────────────

def _package_json_pkgs(obj: dict) -> List[Dict]:
    pkgs: List[Dict] = []
    for key, scope in (
        ("dependencies", "prod"),
        ("devDependencies", "dev"),
        ("peerDependencies", "peer"),
        ("optionalDependencies", "optional"),
        ("bundledDependencies", "bundled"),
        ("bundleDependencies", "bundled"),
    ):
        d = obj.get(key) or {}
        if isinstance(d, dict):
            for name, spec in d.items():
                pkgs.append({"name": name, "version": _normalize_npm_version(spec), "specifier": spec, "scope": scope})
    return pkgs

def _normalize_npm_version(spec: str) -> Optional[str]:
    # If spec is a pinned version like "1.2.3" or "1.2.3-beta", keep it; else None
    if isinstance(spec, str) and spec and not any(spec.startswith(p) for p in ("^", "~", ">=", "<=", ">", "<", "*", "git+", "file:", "link:", "workspace:", "http://", "https://")):
        return spec
    return None

def _package_lock_pkgs(obj: dict) -> List[Dict]:
    # Collect "dependencies" recursively if available (v1 & v2 have different shapes)
    pkgs: Dict[str, str] = {}

    def walk_deps(d: dict):
        for name, meta in (d or {}).items():
            if isinstance(meta, dict):
                ver = meta.get("version")
                if isinstance(ver, str):
                    pkgs[name] = ver
                # v1 nested 'dependencies'
                walk_deps(meta.get("dependencies") or {})

    # v2 has "packages": {"node_modules/pkg": {"version": "x"}}
    if "packages" in obj and isinstance(obj["packages"], dict):
        for k, meta in obj["packages"].items():
            if not isinstance(meta, dict):
                continue
            name = meta.get("name")
            ver = meta.get("version")
            if name and isinstance(ver, str):
                pkgs[name] = ver
        # also merge top-level dependencies if present
        walk_deps(obj.get("dependencies") or {})
    else:
        walk_deps(obj.get("dependencies") or {})

    return [{"name": n, "version": v, "specifier": v, "scope": "locked"} for n, v in sorted(pkgs.items())]

def _yarn_lock_pkgs(text: str) -> List[Dict]:
    # Very rough extraction: lines like
    #   react@^18.2.0:
    #     version "18.2.0"
    pkgs: Dict[str, str] = {}
    cur_names: List[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if not line.startswith(" "):  # a key line
            # may be: "react@^18.2.0", "react@npm:^18.2.0", "react@18.2.0, react@^18.0.0"
            cur_names = [part.strip().strip('"').strip("'").split("@", 1)[0] for part in line.split(",")]
            continue
        m = re.search(r'^\s*version\s+"([^"]+)"', line)
        if m and cur_names:
            ver = m.group(1)
            for nm in cur_names:
                if nm:
                    pkgs[nm] = ver
    return [{"name": n, "version": v, "specifier": v, "scope": "locked"} for n, v in sorted(pkgs.items())]


# ──────────────────────────────────────────────────────────────────────────────
# .NET: .csproj / packages.config
# ──────────────────────────────────────────────────────────────────────────────

def _csproj_pkgs(text: str) -> List[Dict]:
    pkgs: List[Dict] = []
    try:
        root = ET.fromstring(text)
    except Exception:
        return pkgs
    # PackageReference elements
    for pr in root.findall(".//PackageReference"):
        name = pr.attrib.get("Include") or pr.attrib.get("Update")
        version = pr.attrib.get("Version")
        if not version:
            vnode = pr.find("Version")
            if vnode is not None and vnode.text:
                version = vnode.text.strip()
        if name:
            pkgs.append({"name": name, "version": version, "specifier": version, "scope": "prod"})
    return pkgs

def _packages_config_pkgs(text: str) -> List[Dict]:
    pkgs: List[Dict] = []
    try:
        root = ET.fromstring(text)
    except Exception:
        return pkgs
    for pkg in root.findall(".//package"):
        name = pkg.attrib.get("id")
        version = pkg.attrib.get("version")
        scope = "dev" if (pkg.attrib.get("developmentDependency") == "true") else "prod"
        if name:
            pkgs.append({"name": name, "version": version, "specifier": version, "scope": scope})
    return pkgs


# ──────────────────────────────────────────────────────────────────────────────
# Rust: Cargo.toml
# ──────────────────────────────────────────────────────────────────────────────

def _cargo_toml_pkgs(text: str) -> List[Dict]:
    if tomllib is None:
        return []
    try:
        data = tomllib.loads(text)
    except Exception:
        return []
    pkgs: List[Dict] = []
    for sec, scope in (
        ("dependencies", "prod"),
        ("dev-dependencies", "dev"),
        ("build-dependencies", "build"),
    ):
        d = data.get(sec) or {}
        if isinstance(d, dict):
            for name, v in d.items():
                version = None
                spec = None
                if isinstance(v, str):
                    spec = v
                    # pinned if exact number
                    if re.match(r"^\d+\.\d+(\.\d+)?", v):
                        version = v
                elif isinstance(v, dict):
                    if "version" in v and isinstance(v["version"], str):
                        spec = v["version"]
                        version = v["version"]
                pkgs.append({"name": name, "version": version, "specifier": spec, "scope": scope})
    return pkgs


# ──────────────────────────────────────────────────────────────────────────────
# Go: go.mod
# ──────────────────────────────────────────────────────────────────────────────

def _gomod_pkgs(text: str) -> List[Dict]:
    pkgs: List[Dict] = []
    in_require_block = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        if line.startswith("require ("):
            in_require_block = True
            continue
        if in_require_block:
            if line.startswith(")"):
                in_require_block = False
                continue
            # e.g., github.com/pkg/errors v0.9.1 // indirect
            parts = line.split()
            if len(parts) >= 2:
                name, ver = parts[0], parts[1]
                pkgs.append({"name": name, "version": ver, "specifier": ver, "scope": "prod"})
            continue
        if line.startswith("require "):
            _, rest = line.split("require", 1)
            parts = rest.strip().split()
            if len(parts) >= 2:
                name, ver = parts[0], parts[1]
                pkgs.append({"name": name, "version": ver, "specifier": ver, "scope": "prod"})
    return pkgs


# ──────────────────────────────────────────────────────────────────────────────
# Java/Maven: pom.xml
# ──────────────────────────────────────────────────────────────────────────────

def _pom_pkgs(text: str) -> List[Dict]:
    pkgs: List[Dict] = []
    try:
        root = ET.fromstring(text)
    except Exception:
        return pkgs
    # Namespaces are common; try to ignore via wildcard
    for dep in root.findall(".//{*}dependency"):
        gid = _xml_text(dep.find("{*}groupId"))
        aid = _xml_text(dep.find("{*}artifactId"))
        ver = _xml_text(dep.find("{*}version"))
        scope = _xml_text(dep.find("{*}scope")) or "prod"
        if gid and aid:
            name = f"{gid}:{aid}"
            pkgs.append({"name": name, "version": ver, "specifier": ver, "scope": scope})
    return pkgs

def _xml_text(node: Optional[ET.Element]) -> Optional[str]:
    if node is not None and node.text:
        return node.text.strip()
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Dockerfile: FROM
# ──────────────────────────────────────────────────────────────────────────────

def _dockerfile_bases(text: str) -> List[Dict]:
    pkgs: List[Dict] = []
    from_lines = 0
    for raw in text.splitlines():
        m = re.match(r"^\s*FROM\s+([^\s@#]+)(?:@([^ \t#]+))?(?:\s+AS\s+\w+)?", raw, flags=re.IGNORECASE)
        if not m:
            continue
        from_lines += 1
        image = m.group(1)
        digest = m.group(2)
        name, version = _split_image(image)
        spec = digest or version
        pkgs.append({"name": name, "version": version, "specifier": spec, "scope": "base"})
    # Keep a count in meta (returned by caller)
    return pkgs


def _split_image(image: str) -> Tuple[str, Optional[str]]:
    # "python:3.12-slim" -> ("python", "3.12-slim"); "gcr.io/proj/img:tag"
    if "@" in image:  # digest form, leave version None; spec will carry digest
        image = image.split("@", 1)[0]
    if ":" in image and "/" in image.split(":")[0]:
        # if a registry with port (e.g., localhost:5000/x) — last colon is tag sep only if after last slash
        last_colon = image.rfind(":")
        last_slash = image.rfind("/")
        if last_colon > last_slash:
            repo = image[:last_colon]
            tag = image[last_colon + 1 :]
            return repo, tag
        return image, None
    if ":" in image:
        name, tag = image.split(":", 1)
        return name, tag
    return image, None


# ──────────────────────────────────────────────────────────────────────────────
# Master per-file analyzer
# ──────────────────────────────────────────────────────────────────────────────

def analyze_file(*, local_path: Path, repo_rel_posix: str) -> Optional[Dict]:
    """
    Inspect a single file and, if it looks like a dependency manifest/lockfile,
    return a JSON-ready record; otherwise return None.
    """
    bn = local_path.name
    bn_low = bn.lower()
    text = _read_text_limited(local_path)

    # Python manifests
    if bn_low in ("requirements.txt", "requirements-dev.txt", "requirements-prod.txt", "constraints.txt") or bn_low.endswith(".requirements.txt"):
        pkgs = _parse_requirements_text(text)
        return {
            "kind": "deps.index",
            "path": repo_rel_posix,
            "ecosystem": "python",
            "manifest": bn,
            "is_lock": False,
            "packages": pkgs,
            "meta": {"lines": len(text.splitlines())},
        }

    if bn_low == "pyproject.toml":
        if tomllib is None:
            return {
                "kind": "deps.index",
                "path": repo_rel_posix,
                "ecosystem": "python",
                "manifest": bn,
                "is_lock": False,
                "packages": [],
                "meta": {"error": "tomllib unavailable"},
            }
        try:
            data = tomllib.loads(text)
        except Exception:
            data = {}
        pkgs = _pyproject_dependencies(data)
        return {
            "kind": "deps.index",
            "path": repo_rel_posix,
            "ecosystem": "python",
            "manifest": bn,
            "is_lock": False,
            "packages": pkgs,
            "meta": {"project": (data.get("project") or {}).get("name")},
        }

    if bn_low == "setup.cfg":
        pkgs = _setup_cfg_deps(text)
        return {
            "kind": "deps.index",
            "path": repo_rel_posix,
            "ecosystem": "python",
            "manifest": bn,
            "is_lock": False,
            "packages": pkgs,
            "meta": {},
        }

    if bn_low == "poetry.lock":
        # Not fully parsed; count entries by 'name = "x"' + 'version = "y"'
        pkgs: List[Dict] = []
        name = None
        version = None
        for raw in text.splitlines():
            m1 = re.match(r'^\s*name\s*=\s*"([^"]+)"', raw)
            m2 = re.match(r'^\s*version\s*=\s*"([^"]+)"', raw)
            if m1:
                name = m1.group(1)
            if m2:
                version = m2.group(1)
            if name and version:
                pkgs.append({"name": name, "version": version, "specifier": version, "scope": "locked"})
                name = None
                version = None
        return {
            "kind": "deps.index",
            "path": repo_rel_posix,
            "ecosystem": "python",
            "manifest": bn,
            "is_lock": True,
            "packages": pkgs,
            "meta": {"count": len(pkgs)},
        }

    # Node manifests and locks
    if bn_low == "package.json":
        try:
            obj = json.loads(text or "{}")
        except Exception:
            obj = {}
        pkgs = _package_json_pkgs(obj)
        meta = {"name": obj.get("name"), "engines": obj.get("engines")}
        return {
            "kind": "deps.index",
            "path": repo_rel_posix,
            "ecosystem": "node",
            "manifest": bn,
            "is_lock": False,
            "packages": pkgs,
            "meta": meta,
        }

    if bn_low == "package-lock.json":
        try:
            obj = json.loads(text or "{}")
        except Exception:
            obj = {}
        pkgs = _package_lock_pkgs(obj)
        return {
            "kind": "deps.index",
            "path": repo_rel_posix,
            "ecosystem": "node",
            "manifest": bn,
            "is_lock": True,
            "packages": pkgs,
            "meta": {"lockfileVersion": obj.get("lockfileVersion")},
        }

    if bn_low == "yarn.lock":
        pkgs = _yarn_lock_pkgs(text)
        return {
            "kind": "deps.index",
            "path": repo_rel_posix,
            "ecosystem": "node",
            "manifest": bn,
            "is_lock": True,
            "packages": pkgs,
            "meta": {"count": len(pkgs)},
        }

    # .NET
    if bn_low.endswith(".csproj"):
        pkgs = _csproj_pkgs(text)
        return {
            "kind": "deps.index",
            "path": repo_rel_posix,
            "ecosystem": "dotnet",
            "manifest": bn,
            "is_lock": False,
            "packages": pkgs,
            "meta": {},
        }

    if bn_low == "packages.config":
        pkgs = _packages_config_pkgs(text)
        return {
            "kind": "deps.index",
            "path": repo_rel_posix,
            "ecosystem": "dotnet",
            "manifest": bn,
            "is_lock": True,
            "packages": pkgs,
            "meta": {},
        }

    # Rust
    if bn_low == "cargo.toml":
        pkgs = _cargo_toml_pkgs(text)
        return {
            "kind": "deps.index",
            "path": repo_rel_posix,
            "ecosystem": "rust",
            "manifest": bn,
            "is_lock": False,
            "packages": pkgs,
            "meta": {},
        }

    # Go
    if bn_low == "go.mod":
        pkgs = _gomod_pkgs(text)
        return {
            "kind": "deps.index",
            "path": repo_rel_posix,
            "ecosystem": "go",
            "manifest": bn,
            "is_lock": False,
            "packages": pkgs,
            "meta": {},
        }

    # Java/Maven
    if bn_low == "pom.xml":
        pkgs = _pom_pkgs(text)
        return {
            "kind": "deps.index",
            "path": repo_rel_posix,
            "ecosystem": "java",
            "manifest": bn,
            "is_lock": False,
            "packages": pkgs,
            "meta": {},
        }

    # Docker
    if bn == "Dockerfile" or bn_low.endswith(".dockerfile"):
        bases = _dockerfile_bases(text)
        return {
            "kind": "deps.index",
            "path": repo_rel_posix,
            "ecosystem": "docker",
            "manifest": bn,
            "is_lock": False,
            "packages": bases,
            "meta": {"from_lines": sum(1 for _ in re.finditer(r'(?im)^\s*FROM\b', text))},
        }

    return None


# ──────────────────────────────────────────────────────────────────────────────
# Repository scan with summary
# ──────────────────────────────────────────────────────────────────────────────

def scan(repo_root: Path, discovered: Iterable[RepoItem]) -> List[Dict]:
    """
    Scan discovered files, indexing dependency manifests/lockfiles and returning:
      - One 'deps.index' record per recognized manifest/lockfile
      - One 'deps.index.summary' record at the end
    """
    results: List[Dict] = []
    ecos_counter: Counter[str] = Counter()
    manifest_counter: Counter[str] = Counter()
    lock_counter: Counter[str] = Counter()
    pkg_seen: Set[Tuple[str, str]] = set()  # (ecosystem, name)
    pkg_counts: Counter[Tuple[str, str]] = Counter()

    for local, rel in discovered:
        rec = analyze_file(local_path=local, repo_rel_posix=rel)
        if not rec:
            continue
        results.append(rec)
        eco = str(rec.get("ecosystem") or "unknown")
        ecos_counter[eco] += 1
        manifest_counter[str(rec.get("manifest") or "")] += 1
        if rec.get("is_lock"):
            lock_counter[str(rec.get("manifest") or "")] += 1
        for p in rec.get("packages") or []:
            name = p.get("name")
            if not name:
                continue
            key = (eco, name)
            pkg_seen.add(key)
            pkg_counts[key] += 1

    # Build top packages across ecosystems
    top = [{"name": n, "ecosystem": e, "count": c} for ((e, n), c) in pkg_counts.most_common(50)]

    summary = {
        "kind": "deps.index.summary",
        "files": len(results),
        "ecosystems": dict(sorted(ecos_counter.items(), key=lambda kv: (-kv[1], kv[0]))),
        "manifests": dict(sorted(manifest_counter.items(), key=lambda kv: (-kv[1], kv[0]))),
        "packages_unique": len(pkg_seen),
        "top_packages": top,
        "lockfiles": {
            "count": sum(lock_counter.values()),
            "by_kind": dict(sorted(lock_counter.items(), key=lambda kv: (-kv[1], kv[0]))),
        },
    }
    results.append(summary)
    return results


__all__ = ["scan", "analyze_file"]
