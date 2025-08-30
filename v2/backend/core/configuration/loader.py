# File: v2/backend/core/configuration/loader.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any, Dict, Optional, Tuple

import os
import json
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
        Detect repository root and standard config locations by walking upward
        from the current working directory and this file's location.
        """
        candidates = []
        # start from CWD
        candidates.append(Path.cwd())
        # start from this file
        candidates.append(Path(__file__).resolve().parent.parent.parent.parent.parent)  # v2/backend/core/configuration/loader.py -> repo
        # walk upward looking for a 'config' and 'v2' folder
        roots = []
        for base in candidates:
            cur = base.resolve()
            for _ in range(8):
                if (cur / "config").exists() and (cur / "v2").exists():
                    roots.append(cur)
                    break
                if cur.parent == cur:
                    break
                cur = cur.parent
        repo_root = (roots[0] if roots else Path.cwd()).resolve()

        config_dir = (repo_root / "config").resolve()
        spine_dir = (config_dir / "spine").resolve()
        spine_profile_dir = (spine_dir / "pipelines" / "default").resolve()
        secrets_dir = (repo_root / "secret_management").resolve()  # singular, matches your logs

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
# YAML helpers
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
# Packager configuration (packager.yml)
# ──────────────────────────────────────────────────────────────────────────────
def get_packager() -> NS:
    """
    Load config/packager.yml with minimal normalization.
    Only the fields actually used by callers are returned.
    """
    paths = ConfigPaths.detect()
    packager_yml = paths.config_dir / "packager.yml"
    data = _read_yaml(packager_yml) if packager_yml.exists() else {}

    include_globs = list(data.get("include_globs", []))
    exclude_globs = list(data.get("exclude_globs", []))
    segment_excludes = list(data.get("segment_excludes", []))
    emitted_prefix = str(data.get("emitted_prefix", "output/patch_code_bundles"))

    publish = data.get("publish", {}) or {}
    if not isinstance(publish, dict):
        publish = {}

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

    If a 'github:' section exists but lacks a non-empty 'api_key', we raise
    ConfigError with a clear message. We *do not* echo secret values.
    """
    paths = paths or ConfigPaths.detect()

    # Look for secrets.{yml,yaml}
    yml = paths.secrets_dir / "secrets.yml"
    yaml_path = paths.secrets_dir / "secrets.yaml"
    secrets_path = yml if yml.exists() else yaml_path if yaml_path.exists() else None

    data: Dict[str, Any] = {}
    if secrets_path and secrets_path.exists():
        data = _read_yaml(secrets_path)

    # Extract OpenAI
    openai_api_key = None
    if "openai" in data and isinstance(data["openai"], dict):
        openai_api_key = _validate_non_empty_string(data["openai"].get("api_key"))
        if openai_api_key is None and "api_key" in data["openai"]:
            raise ConfigError(
                f"Invalid openai.api_key in {secrets_path}: must be a non-empty string."
            )
    # Flat alternatives (accepted but not preferred)
    if openai_api_key is None:
        for k in ("openai_api_key", "OPENAI_API_KEY"):
            openai_api_key = _validate_non_empty_string(data.get(k))
            if openai_api_key:
                break

    # Extract GitHub
    github_token = None
    if "github" in data and isinstance(data["github"], dict):
        # Authoritative field
        github_token = _validate_non_empty_string(data["github"].get("api_key"))
        # Alias (optional)
        if github_token is None and "token" in data["github"]:
            github_token = _validate_non_empty_string(data["github"].get("token"))

        # If the github section exists but we couldn't get a token -> hard error
        if github_token is None:
            raise ConfigError(
                f"Invalid or missing github.api_key in {secrets_path}. "
                f"Add:\n  github:\n    api_key: \"<YOUR_TOKEN>\""
            )

    # Construct secrets object
    return SecretsConfig(
        openai_api_key=openai_api_key,
        github_token=github_token,
    )
