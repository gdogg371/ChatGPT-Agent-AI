# v2/backend/core/utils/code_bundles/code_bundles/execute/config.py
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Optional

# PyYAML is required to parse config/packager.yml (match run_pack behavior).
try:
    import yaml
except Exception as e:
    raise ImportError("PyYAML is required to read config/packager.yml. Install 'pyyaml'.") from e

__all__ = ["build_cfg", "_read_root_publish_analysis", "_read_root_emit_ast", "_read_code_bundle_params"]


# -----------------------
# Small local YAML helpers
# -----------------------

def _load_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _read_yaml_if_exists(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return _load_yaml(path)
    except Exception:
        # Keep loader tolerant; executor will validate required keys later.
        return {}


def _resolve_path(base: Path, maybe_path: Optional[str]) -> Path:
    """
    Resolve a path from YAML. If it's absolute, keep it; if relative, anchor at 'base'.
    If missing/empty, return 'base'.
    """
    if not maybe_path:
        return base
    p = Path(str(maybe_path))
    return p if p.is_absolute() else (base / p)


# -----------------------
# GitHub token resolution
# -----------------------

def _coalesce_github_token(project_root: Path) -> str:
    """
    Mirror run_pack behavior:
      1) Environment variables (strongest):
         GITHUB_TOKEN, GH_TOKEN, GITHUB_API_KEY
      2) secret_management/secrets.yml (if present):
         github.api_key | github.token | github_api_key
      3) Fallback token file (developer convention):
         ~/.config/github/token
    Returns empty string if nothing found (executor will error if GitHub mode is used).
    """
    # 1) Env
    env_token = (
        os.environ.get("GITHUB_TOKEN")
        or os.environ.get("GH_TOKEN")
        or os.environ.get("GITHUB_API_KEY")
        or ""
    )
    if env_token:
        return env_token.strip()

    # 2) secrets.yml (optional)
    secrets_path = project_root / "secret_management" / "secrets.yml"
    secrets = _read_yaml_if_exists(secrets_path)
    gh = secrets.get("github") or {}
    token = (
        gh.get("api_key")
        or gh.get("token")
        or secrets.get("github_api_key")
        or ""
    )
    if token:
        return str(token).strip()

    # 3) ~/.config/github/token (optional)
    token_file = Path.home() / ".config" / "github" / "token"
    if token_file.exists():
        try:
            return token_file.read_text(encoding="utf-8").strip()
        except Exception:
            pass

    return ""


# -----------------------
# Public API
# -----------------------

def _read_root_publish_analysis(project_root: Path) -> bool:
    """
    Read 'publish_analysis' from config/packager.yml (default False if absent).
    """
    cfg_path = Path(project_root) / "config" / "packager.yml"
    if not cfg_path.exists():
        return False
    try:
        data = _load_yaml(cfg_path)
        return bool(data.get("publish_analysis", False))
    except Exception:
        return False


def _read_root_emit_ast(project_root: Path) -> bool:
    """
    Read 'emit_ast' from config/packager.yml (default False if absent).
    """
    cfg_path = Path(project_root) / "config" / "packager.yml"
    if not cfg_path.exists():
        return False
    try:
        data = _load_yaml(cfg_path)
        return bool(data.get("emit_ast", False))
    except Exception:
        return False


def build_cfg(project_root: Path) -> NS:
    """
    Load config strictly from config/packager.yml and resolve GitHub credentials
    using the same precedence run_pack uses (ENV → secrets.yml → token file).
    Also normalizes key publish paths and exposes clean flags for the executor.
    """
    project_root = Path(project_root).resolve()

    cfg_file = project_root / "config" / "packager.yml"
    if not cfg_file.exists():
        raise FileNotFoundError(f"Missing config file: {cfg_file}")

    data = _load_yaml(cfg_file)
    publish = data.get("publish") or {}
    github = publish.get("github") or {}
    clean = publish.get("clean") or {}

    # Normalize / mirror fields
    ns = NS(
        project_root=project_root,
        config=data,
        publish=publish,
        github=github,
        publish_mode=str(publish.get("mode", "both")).lower(),
        publish_analysis=bool(data.get("publish_analysis", False)),
        emit_ast=bool(data.get("emit_ast", False)),
    )

    # Paths (normalize relative to project root)
    ns.emitted_prefix = str(data.get("emitted_prefix", "output/patch_code_bundles")).strip("/")

    ns.staging_root = _resolve_path(project_root, publish.get("staging_root") or "output/staging")
    ns.output_root = _resolve_path(project_root, publish.get("output_root") or "output/patch_code_bundles")
    ns.ingest_root = _resolve_path(project_root, publish.get("ingest_root") or ".")
    ns.local_publish_root = _resolve_path(project_root, publish.get("local_publish_root") or "output/patch_code_bundles/published")

    # Clean flags (so executor doesn’t have to poke through dicts)
    ns.clean_before_publish = bool(publish.get("clean_before_publish", False))
    ns.clean_repo_root = bool(clean.get("clean_repo_root", False))
    ns.clean_artifacts = bool(clean.get("clean_artifacts", False))

    # GitHub coordinates (from YAML)
    ns.github_owner = str(github.get("owner", "")).strip()
    ns.github_repo = str(github.get("repo", "")).strip()
    ns.github_branch = str(github.get("branch", "main")).strip()
    ns.github_base = str(github.get("base_path", "")).strip()

    # GitHub token (ENV → secrets.yml → token file)
    ns.github_token = _coalesce_github_token(project_root)

    return ns


def _read_code_bundle_params(project_root: Path) -> dict:
    """
    Read dynamic params emitted by the pipeline (if present).
    """
    fp = Path(project_root) / "output" / "patch_code_bundles" / "publish_params.json"
    if not fp.exists():
        return {}
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return {}


