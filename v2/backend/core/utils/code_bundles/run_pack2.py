

from __future__ import annotations



import os
import sys

from pathlib import Path

from typing import Any, Dict, List, Optional, Tuple, Iterable
from urllib import error, parse, request
# Runtime flow logging (strict)



# Ensure the embedded packager is importable first
ROOT = Path(__file__).parent
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# Embedded packager
from packager.core.orchestrator import Packager
import packager.core.orchestrator as orch_mod  # provenance


# (added) Handoff writer
from v2.backend.core.utils.code_bundles.code_bundles.src.packager.io.guide_writer import GuideWriter

# Manifest helpers + enrichment


# ---- analysis emitter wiring (robust loader) ----










from v2.backend.core.configuration.loader import (
    get_repo_root,
    get_packager,
    get_secrets,
    ConfigError,
    ConfigPaths,
)

























































