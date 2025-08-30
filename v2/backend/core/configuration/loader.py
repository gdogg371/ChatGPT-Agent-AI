from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any, Dict, Optional

import yaml


# ──────────────────────────────────────────────────────────────────────────────
# Errors
# ──────────────────────────────────────────────────────────────────────────────
class ConfigError(RuntimeError):
    pass


# ──────────────────────────────────────────────────────────────────────────────
# Secrets surface
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class SecretsConfig:
    """
    Secrets surface used by multiple tools. Fields are optional so callers can
    apply their own fail-fast policies.
    """
    openai_api_key: Optional[str] = None
    github_token: Optional[str] = None


# ──────────────────────────────────────────────────────────────────────────────
# Path resolution
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class ConfigPaths:
    repo_root: Path
    config_dir: Path
    spine_dir: Path
    spine_profile_dir: Path
    secrets_dir: Path

    @staticmethod
    def detect() -> "ConfigPaths":
        """
        Detect repository root and standard config locations by walking upward.
        """
        # Use this file's location as an anchor and walk up to project root (contains /config and /v2)
        here = Path(__file__).resolve()
        cur = here
        root = None
        for _ in range(12):
            if (cur / "config").exists() and (cur / "v2").exists():
                root = cur
                break
            if cur.parent == cur:
                break
            cur = cur.parent
        repo_root = (root or Path.cwd()).resolve()

        config_dir = (repo_root / "config").resolve()
        spine_dir = (config_dir / "spine").resolve()
        spine_profile_dir = (spine_dir / "pipelines" / "default").resolve()
        # Your logs show "secret_management" (singular)
        secrets_dir = (repo_root / "secret_management").resolve()

        return ConfigPaths(
            repo_root=repo_root,
            config_dir=config_dir,
            spine_dir=spine_dir,
            spine_profile_dir=spine_profile_dir,
            secrets_dir=secrets_dir,
        )


def get_repo_root() -> Path:
    return ConfigPaths.detect().repo_root


# ──────────────────────────────────────────────────────────────────────────────
# YAML helper
# ──────────────────────────────────────────────────────────────────────────────
def _read_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"YAML file is not a mapping: {path}")
    return data


# ──────────────────────────────────────────────────────────────────────────────
# Packager configuration (config/packager.yml)
# ──────────────────────────────────────────────────────────────────────────────
def get_packager() -> NS:
    """
    Load config/packager.yml and normalize into a stable shape:
      - publish.* returned as-is
      - If top-level `github:` or `mode:` exist, fold into publish.*
      - Copy publish.clean.* convenience flags up to publish.* when present
    """
    paths = ConfigPaths.detect()
    packager_yml = paths.config_dir / "packager.yml"
    data = _read_yaml(packager_yml) if packager_yml.exists() else {}

    publish = data.get("publish") or {}
    if not isinstance(publish, dict):
        publish = {}

    # Normalize possible top-level keys to publish.*
    if "mode" in data and "mode" not in publish:
        publish["mode"] = data.get("mode")
    if "github" in data and "github" not in publish and isinstance(data["github"], dict):
        publish["github"] = data["github"]

    # Ensure github map exists
    github = publish.get("github") or {}
    if not isinstance(github, dict):
        github = {}
    # Normalize fields
    owner = (github.get("owner") or "") if isinstance(github.get("owner"), str) else ""
    repo = (github.get("repo") or "") if isinstance(github.get("repo"), str) else ""
    branch = (github.get("branch") or "main") if isinstance(github.get("branch"), str) else "main"
    base_path = (github.get("base_path") or "") if isinstance(github.get("base_path"), str) else ""
    github = {"owner": owner, "repo": repo, "branch": branch, "base_path": base_path}
    publish["github"] = github

    # Common fields (keep whatever exists; runner will decide usage)
    include_globs = list(data.get("include_globs", []))
    exclude_globs = list(data.get("exclude_globs", []))
    segment_excludes = list(data.get("segment_excludes", []))
    emitted_prefix = str(data.get("emitted_prefix", "output/patch_code_bundles"))

    # Cleaning flags: accept publish.clean.* and/or flat publish.* flags
    clean = publish.get("clean") or {}
    if not isinstance(clean, dict):
        clean = {}
    clean_repo_root = bool(clean.get("clean_repo_root", False))
    clean_artifacts = bool(clean.get("clean_artifacts", False))
    clean_before_publish = bool(publish.get("clean_before_publish", False)) or clean_artifacts

    # Attach normalized back
    publish["clean_before_publish"] = bool(clean_before_publish)
    publish["clean_repo_root"] = bool(clean_repo_root)
    publish["clean_artifacts"] = bool(clean_artifacts)

    return NS(
        include_globs=include_globs,
        exclude_globs=exclude_globs,
        segment_excludes=segment_excludes,
        emitted_prefix=emitted_prefix,
        publish=publish,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Secrets
# ──────────────────────────────────────────────────────────────────────────────
def _validate_non_empty_string(val: Any) -> Optional[str]:
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


def get_secrets(paths: Optional[ConfigPaths] = None) -> SecretsConfig:
    """
    Load secrets from secret_management/secrets.yml (or .yaml).

    Mapping (authoritative):
      github.api_key  -> SecretsConfig.github_token
      openai.api_key  -> SecretsConfig.openai_api_key

    If a 'github:' section exists but lacks a non-empty 'api_key', raise ConfigError.
    """
    paths = paths or ConfigPaths.detect()

    # Look for secrets.{yml,yaml}
    yml = paths.secrets_dir / "secrets.yml"
    yaml_path = paths.secrets_dir / "secrets.yaml"
    secrets_path = yml if yml.exists() else yaml_path if yaml_path.exists() else None

    data: Dict[str, Any] = {}
    if secrets_path and secrets_path.exists():
        data = _read_yaml(secrets_path)

    # Extract OpenAI key
    openai_api_key = None
    if "openai" in data and isinstance(data["openai"], dict):
        openai_api_key = _validate_non_empty_string(data["openai"].get("api_key"))
        if openai_api_key is None and "api_key" in data["openai"]:
            raise ConfigError(
                f"Invalid openai.api_key in {secrets_path}: must be a non-empty string."
            )
    if openai_api_key is None:
        for k in ("openai_api_key", "OPENAI_API_KEY"):
            openai_api_key = _validate_non_empty_string(data.get(k))
            if openai_api_key:
                break

    # Extract GitHub token (authoritative section)
    github_token = None
    if "github" in data and isinstance(data["github"], dict):
        github_token = _validate_non_empty_string(data["github"].get("api_key"))
        # allow alias "token"
        if github_token is None and "token" in data["github"]:
            github_token = _validate_non_empty_string(data["github"].get("token"))
        if github_token is None:
            raise ConfigError(
                f"Invalid or missing github.api_key in {secrets_path}. "
                f"Add:\n  github:\n    api_key: \"<YOUR_TOKEN>\""
            )

    return SecretsConfig(
        openai_api_key=openai_api_key,
        github_token=github_token,
    )

