#v2\backend\core\configuration\loader.py
"""
Configuration loader for YAML-only, fail-fast settings.

This module centralizes reading all config/secrets files with platform-agnostic
paths and strict validation. It does not read environment variables and it does
not embed defaults for required values: missing files/keys raise immediately.

Key points:
- Supports both .yml and .yaml filenames.
- No hardcoded paths; all resolution is relative to the detected project root.
- Dataclasses provide typed access to configuration sections.
- Exposes ConfigError for callers that want to catch configuration issues.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml


# ------------------------------ errors ----------------------------------------


class ConfigError(RuntimeError):
    """Raised for configuration/validation problems detected by the loader."""


# ------------------------------ helpers ---------------------------------------


def _project_root(anchor: Optional[Path] = None) -> Path:
    """
    Best-effort project root detection: walk upward until we find a folder that
    contains 'config' or 'v2'. Fallback to current working directory.
    """
    p = (anchor or Path(__file__)).resolve()
    for _ in range(8):
        if (p / "config").exists() or (p / "v2").exists():
            return p
        p = p.parent
    return Path.cwd().resolve()


def get_repo_root() -> Path:
    """
    Public alias used by legacy callers (e.g., run_pack.py).
    Returns the detected project root Path.
    """
    return _project_root()


def _first_existing(*candidates: Path) -> Optional[Path]:
    for c in candidates:
        if c and c.exists() and c.is_file():
            return c
    return None


def _read_yaml_required(path: Path) -> Dict[str, Any]:
    if not path.exists() or not path.is_file():
        raise ConfigError(f"[config] Missing required YAML file: {path}")
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        raise ConfigError(f"[config] Failed to parse YAML: {path} :: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"[config] YAML root must be a mapping: {path}")
    return data


def _req_str(mapping: Dict[str, Any], key: str) -> str:
    val = mapping.get(key)
    if not isinstance(val, str) or not val.strip():
        raise ConfigError(f"[config] Missing/invalid string for key: {key}")
    return val


def _opt_str(mapping: Dict[str, Any], key: str) -> Optional[str]:
    val = mapping.get(key)
    if val is None:
        return None
    if not isinstance(val, str):
        raise ConfigError(f"[config] Invalid string for key: {key}")
    return val


def _req_list_str(mapping: Dict[str, Any], key: str) -> Tuple[str, ...]:
    """
    Require a key that must exist and be a list of strings.
    Empty list is allowed, but the key must be present.
    """
    if key not in mapping:
        raise ConfigError(f"[config] Missing required list key: {key}")
    val = mapping.get(key)
    if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
        raise ConfigError(f"[config] Key '{key}' must be a list of strings")
    return tuple(val)


def _norm_posix(path_str: str) -> str:
    """Normalize a path to a POSIX-like form for cross-platform compatibility."""
    return Path(path_str).as_posix()


# ------------------------------ dataclasses -----------------------------------


@dataclass
class ConfigPaths:
    root: Path
    config_dir: Path
    secrets_dir: Path
    spine_caps: Path
    spine_pipeline_dir: Path
    spine_profile_dir: Path

    @staticmethod
    def detect(project_root: Optional[Path] = None, profile: str = "default") -> "ConfigPaths":
        root = _project_root(project_root)
        config_dir = root / "config"
        secrets_dir = root / "secret_management"

        spine_dir = config_dir / "spine"
        spine_caps = spine_dir / "capabilities.yml"
        spine_pipeline_dir = spine_dir / "pipelines"
        spine_profile_dir = spine_pipeline_dir / profile

        return ConfigPaths(
            root=root,
            config_dir=config_dir,
            secrets_dir=secrets_dir,
            spine_caps=spine_caps,
            spine_pipeline_dir=spine_pipeline_dir,
            spine_profile_dir=spine_profile_dir,
        )


@dataclass
class DbConfig:
    url: str
    table: str

    @property
    def sqlalchemy_url(self) -> str:
        return self.url


@dataclass
class LlmsConfig:
    provider: str
    model: str


@dataclass
class PackagerConfig:
    """
    Packager configuration used by run_pack.py

    Required keys from config/packager.yml:
      - emitted_prefix: str
      - include_globs: list[str]
      - exclude_globs: list[str]
      - segment_excludes: list[str]
    Optional:
      - publish: dict
    """
    emitted_prefix: str
    include_globs: Tuple[str, ...]
    exclude_globs: Tuple[str, ...]
    segment_excludes: Tuple[str, ...]
    publish: Optional[Dict[str, Any]] = None  # dict or None


@dataclass
class VarsConfig:
    max_rows: int
    status_filter: str
    out_base: str
    run_fetch: bool
    run_build: bool
    run_llm: bool
    run_unpack: bool
    run_sanitize: bool
    run_verify: bool
    run_write: bool
    run_apply: bool
    run_archive: bool
    run_rollback: bool
    exclude_globs: Tuple[str, ...]
    segment_excludes: Tuple[str, ...]
    ask_spec: Dict[str, Any]
    docstrings_model_path: Optional[str] = None


@dataclass
class SecretsConfig:
    """
    Secrets surface used by multiple tools. Fields are optional here so the
    loader can centralize fail-fast policy per caller as needed.
    """
    openai_api_key: Optional[str] = None
    github_token: Optional[str] = None
    # extendable: huggingface_token, anthropic_api_key, etc.


# ------------------------------ section loaders --------------------------------


def get_db(paths: ConfigPaths) -> DbConfig:
    """
    Load DB settings from config/db.yml (or .yaml). For SQLite you can specify
    either 'url' or 'path'. If 'path' is present, it is converted to a URL.
    """
    db_yml = _first_existing(
        paths.config_dir / "db.yml",
        paths.config_dir / "db.yaml",
    )
    if not db_yml:
        raise ConfigError(f"[config] Missing required YAML file: {paths.config_dir / 'db.yml'}")
    data = _read_yaml_required(db_yml)

    url = _opt_str(data, "url")
    path = _opt_str(data, "path")
    if bool(url) and bool(path):
        raise ConfigError("[config] Provide either 'url' or 'path' in db.yml, not both.")
    if not url and not path:
        raise ConfigError("[config] db.yml requires 'url' or 'path'.")

    if not url and path:
        # allow relative paths; normalize to URL
        url = f"sqlite:///{Path(path).resolve().as_posix()}"

    table = _req_str(
        _read_yaml_required(paths.spine_profile_dir / "vars.yml"),
        "sqlalchemy_table",
    )

    return DbConfig(url=url or "", table=table)


def get_llm(paths: ConfigPaths) -> LlmsConfig:
    """
    Load LLM defaults for the patch loop from config/spine/pipelines/<profile>/llm.yml.
    """
    llm_yml = _first_existing(
        paths.spine_profile_dir / "llm.yml",
        paths.spine_profile_dir / "llm.yaml",
    )
    if not llm_yml:
        raise ConfigError(f"[config] Missing required YAML file: {paths.spine_profile_dir / 'llm.yml'}")
    data = _read_yaml_required(llm_yml)
    provider = _req_str(data, "provider")
    model = _req_str(data, "model")
    return LlmsConfig(provider=provider, model=model)


def get_packager(paths: Optional[ConfigPaths] = None) -> PackagerConfig:
    """
    Load packager settings from config/packager.yml (or .yaml).

    Required keys:
      emitted_prefix, include_globs, exclude_globs, segment_excludes
    Optional:
      publish (dict)

    Backward-compatible signature:
      - If `paths` is None, detect via ConfigPaths.detect() using default profile.
      - Existing callers that pass ConfigPaths continue to work unchanged.
    """
    if paths is None:
        paths = ConfigPaths.detect()

    pack_yml = _first_existing(
        paths.config_dir / "packager.yml",
        paths.config_dir / "packager.yaml",
    )
    if not pack_yml:
        raise ConfigError(f"[config] Missing required YAML file: {paths.config_dir / 'packager.yml'}")
    data = _read_yaml_required(pack_yml)

    emitted_prefix = _req_str(data, "emitted_prefix")
    include_globs = _req_list_str(data, "include_globs")
    exclude_globs = _req_list_str(data, "exclude_globs")
    segment_excludes = _req_list_str(data, "segment_excludes")
    publish = data.get("publish") if isinstance(data.get("publish"), dict) else None

    return PackagerConfig(
        emitted_prefix=emitted_prefix,
        include_globs=include_globs,
        exclude_globs=exclude_globs,
        segment_excludes=segment_excludes,
        publish=publish,
    )


def get_normalize(paths: ConfigPaths) -> Dict[str, Any]:
    """
    Load code-bundle normalization rules, if present.
    """
    norm_yml = _first_existing(
        paths.config_dir / "normalize.yml",
        paths.config_dir / "normalize.yaml",
    )
    return _read_yaml_required(norm_yml) if norm_yml else {}


def get_pipeline_vars(paths: ConfigPaths) -> VarsConfig:
    """
    Load per-profile variables from config/spine/pipelines/<profile>/vars.yml.
    """
    vars_yml = _first_existing(
        paths.spine_profile_dir / "vars.yml",
        paths.spine_profile_dir / "vars.yaml",
    )
    if not vars_yml:
        raise ConfigError(f"[config] Missing required YAML file: {paths.spine_profile_dir / 'vars.yml'}")
    data = _read_yaml_required(vars_yml)

    def _req_bool(k: str) -> bool:
        v = data.get(k)
        if isinstance(v, bool):
            return v
        raise ConfigError(f"[config] Missing/invalid boolean for key: {k}")

    exclude_globs = tuple([str(x) for x in (data.get("exclude_globs") or []) if isinstance(x, str)])
    segment_excludes = tuple([str(x) for x in (data.get("segment_excludes") or []) if isinstance(x, str)])

    return VarsConfig(
        max_rows=int(data.get("max_rows") or 200),
        status_filter=_req_str(data, "status_filter"),
        out_base=_req_str(data, "out_base"),
        run_fetch=_req_bool("RUN_FETCH"),
        run_build=_req_bool("RUN_BUILD"),
        run_llm=_req_bool("RUN_LLM"),
        run_unpack=_req_bool("RUN_UNPACK"),
        run_sanitize=_req_bool("RUN_SANITIZE"),
        run_verify=_req_bool("RUN_VERIFY"),
        run_write=_req_bool("RUN_WRITE"),
        run_apply=_req_bool("RUN_APPLY"),
        run_archive=_req_bool("RUN_ARCHIVE"),
        run_rollback=_req_bool("RUN_ROLLBACK"),
        exclude_globs=exclude_globs,
        segment_excludes=segment_excludes,
        ask_spec=dict(data.get("ask_spec") or {}),
        docstrings_model_path=_opt_str(data, "docstrings_model_path"),
    )


def get_secrets(paths: ConfigPaths) -> SecretsConfig:
    """
    Load secrets from secret_management/secrets.yml (or .yaml). This function
    performs only mapping/normalization — it does not fallback to env vars.

    Accepted keys (any of these forms):
      openai:
        api_key: "<key>"
      OPENAI_API_KEY: "<key>"
      openai_api_key: "<key>"

      github:
        token: "<token>"
        api_token: "<token>"
        pat: "<token>"
      GITHUB_TOKEN: "<token>"
      github_token: "<token>"
    """
    sec_yml = _first_existing(
        paths.secrets_dir / "secrets.yml",
        paths.secrets_dir / "secrets.yaml",
    )
    if not sec_yml:
        raise ConfigError(f"[config] Missing required YAML file: {paths.secrets_dir / 'secrets.yml'}")
    data = _read_yaml_required(sec_yml)

    # OpenAI
    openai_key = (
        (data.get("openai") or {}).get("api_key")
        or data.get("OPENAI_API_KEY")
        or data.get("openai_api_key")
    )
    if openai_key is not None and not isinstance(openai_key, str):
        raise ConfigError("[config] openai.api_key must be a string")

    # GitHub
    gdict = data.get("github") or {}
    github_token = (
        gdict.get("token")
        or gdict.get("api_token")
        or gdict.get("pat")
        or data.get("GITHUB_TOKEN")
        or data.get("github_token")
    )
    if github_token is not None and not isinstance(github_token, str):
        raise ConfigError("[config] github token must be a string")

    return SecretsConfig(
        openai_api_key=openai_key,
        github_token=github_token,
    )


__all__ = [
    "ConfigError",
    "ConfigPaths",
    "DbConfig",
    "LlmsConfig",
    "PackagerConfig",
    "VarsConfig",
    "SecretsConfig",
    "get_repo_root",
    "get_db",
    "get_llm",
    "get_packager",
    "get_normalize",
    "get_pipeline_vars",
    "get_secrets",
]









