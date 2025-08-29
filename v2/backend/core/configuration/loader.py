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

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union, List
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

def get_config_root(paths: Optional["ConfigPaths"] = None) -> Path:
    """Return the path to the /config directory (for callers that read YAML directly)."""
    if paths is None:
        paths = ConfigPaths.detect()
    return paths.config_dir


def get_spine(paths: Optional["ConfigPaths"] = None, profile: Optional[str] = None) -> SpineConfig:
    """
    Return caps/pipelines/profile locations the rest of the code expects.
    Back-compat wrapper so callers don't need to pass ConfigPaths explicitly.
    """
    if paths is None:
        paths = ConfigPaths.detect(profile=profile or "default")
    elif profile and profile != paths.spine_profile_dir.name:
        # Re-detect if caller asks for a different profile than 'paths' holds
        paths = ConfigPaths.detect(project_root=paths.root, profile=profile)
    return SpineConfig(
        caps_path=paths.spine_caps,
        pipelines_root=paths.spine_pipeline_dir,
        profile=paths.spine_profile_dir.name,
    )



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
class SpineConfig:
    caps_path: Path
    pipelines_root: Path
    profile: str

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


# --- add/replace this dataclass definition (keep existing naming) ---
# replace or extend your existing VarsConfig
@dataclass
class VarsConfig:
    # Stage toggles
    run_fetch_targets: bool = True
    run_build_prompts: bool = True
    run_run_llm: bool = True
    run_unpack: bool = True
    run_sanitize: bool = True
    run_verify: bool = True
    run_save_patch: bool = True
    run_apply_patch_sandbox: bool = False
    run_archive_and_replace: bool = False
    run_rollback: bool = False

    # General knobs
    max_rows: int = 200
    out_base: str = ""
    exclude_globs: List[str] = field(default_factory=list)
    segment_excludes: List[str] = field(default_factory=list)

    # Pipeline/DB specifics
    sqlalchemy_table: str = "introspection_index"
    status_filter: str = ""

    # Extras used by your runner
    ask_spec: Dict[str, Any] = field(default_factory=dict)
    docstrings_model_path: str = ""




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


def get_db(paths: Optional["ConfigPaths"] = None) -> DbConfig:
    if paths is None:
        paths = ConfigPaths.detect()
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


def get_llm(paths: Optional["ConfigPaths"] = None) -> LlmsConfig:
    if paths is None:
        paths = ConfigPaths.detect()
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


# --- replace the entire get_pipeline_vars(...) with this back-compat wrapper ---
def get_pipeline_vars(arg: Union["ConfigPaths", str, None] = None) -> VarsConfig:
    """
    Load pipeline variables from vars.yml/vars.yaml in the active spine profile.

    Accepts:
      - None              -> auto-detect default profile
      - str (profile)     -> that profile
      - ConfigPaths       -> use as-is

    Tolerant to legacy keys:
      RUN_FETCH  -> run_fetch_targets
      RUN_BUILD  -> run_build_prompts
      RUN_WRITE  -> run_save_patch
      RUN_APPLY  -> run_apply_patch_sandbox
      RUN_ARCHIVE-> run_archive_and_replace
    """
    if arg is None:
        paths = ConfigPaths.detect()
    elif isinstance(arg, ConfigPaths):
        paths = arg
    else:
        paths = ConfigPaths.detect(profile=str(arg))

    vars_yml = _first_existing(
        paths.spine_profile_dir / "vars.yml",
        paths.spine_profile_dir / "vars.yaml",
    )
    if not vars_yml:
        raise ConfigError(f"[config] Missing required YAML file: {paths.spine_profile_dir / 'vars.yml'}")

    data: Dict[str, Any] = _read_yaml_required(vars_yml)

    def _get(keys: List[str], default=None):
        for k in keys:
            if k in data:
                return data[k]
            lk = k.lower()
            if lk in data:
                return data[lk]
        return default

    def _b_any(keys: List[str], default: bool) -> bool:
        v = _get(keys, None)
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(v)
        if isinstance(v, str):
            s = v.strip().lower()
            if s in {"1", "true", "yes", "on"}:
                return True
            if s in {"0", "false", "no", "off"}:
                return False
        return default

    def _lst(keys: List[str]) -> List[str]:
        v = _get(keys, None)
        if v is None:
            return []
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        if isinstance(v, list):
            return [str(x) for x in v]
        return []

    vc = VarsConfig(
        # Stage toggles (accept both new + legacy names)
        run_fetch_targets=_b_any(["RUN_FETCH_TARGETS", "RUN_FETCH"], True),
        run_build_prompts=_b_any(["RUN_BUILD_PROMPTS", "RUN_BUILD"], True),
        run_run_llm=_b_any(["RUN_LLM"], True),
        run_unpack=_b_any(["RUN_UNPACK"], True),
        run_sanitize=_b_any(["RUN_SANITIZE"], True),
        run_verify=_b_any(["RUN_VERIFY"], True),
        run_save_patch=_b_any(["RUN_SAVE_PATCH", "RUN_WRITE"], True),
        run_apply_patch_sandbox=_b_any(["RUN_APPLY_PATCH_SANDBOX", "RUN_APPLY"], False),
        run_archive_and_replace=_b_any(["RUN_ARCHIVE_AND_REPLACE", "RUN_ARCHIVE"], False),
        run_rollback=_b_any(["RUN_ROLLBACK"], False),

        # General knobs
        max_rows=int(_get(["MAX_ROWS", "max_rows"], 200) or 200),
        out_base=str(_get(["OUT_BASE", "out_base"], "") or ""),
        exclude_globs=_lst(["EXCLUDE_GLOBS", "exclude_globs"]),
        segment_excludes=_lst(["SEGMENT_EXCLUDES", "segment_excludes"]),

        # DB specifics
        sqlalchemy_table=str(_get(["SQLALCHEMY_TABLE", "sqlalchemy_table"], "introspection_index")),
        status_filter=str(_get(["STATUS_FILTER", "status_filter"], "") or ""),

        # Extras
        ask_spec=dict(_get(["ASK_SPEC", "ask_spec"], {}) or {}),
        docstrings_model_path=str(_get(["DOCSTRINGS_MODEL_PATH", "docstrings_model_path"], "") or ""),
    )
    return vc




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
    "SpineConfig",                 # NEW
    "get_repo_root",
    "get_config_root",  # NEW
    "get_spine",                   # NEW
    "get_db",
    "get_llm",
    "get_packager",
    "get_normalize",
    "get_pipeline_vars",
    "get_secrets",
]










