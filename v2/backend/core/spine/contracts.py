# SPDX-License-Identifier: MIT
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Mapping, Optional
import time
import uuid


def _uuid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


@dataclass(slots=True)
class Envelope:
    """
    Canonical envelope that every spine Task travels inside.
    Keep this stable; version with semver if you need to change.
    """
    envelope_version: str
    id: str
    ts: str
    trace_id: str
    producer: str
    intent: str            # discover|analyze|plan|patch|verify|publish|heartbeat|pipeline
    subject: str           # res://..., file://..., git://..., etc.
    capability: str        # e.g., "analyze.generic.v1"
    annotations: Dict[str, Any] = field(default_factory=dict)
    ttl_s: int = 900


def new_envelope(
    *,
    intent: str,
    subject: str,
    capability: str,
    producer: str = "spine",
    annotations: Optional[Mapping[str, Any]] = None,
    version: str = "1.0.0",
) -> Envelope:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return Envelope(
        envelope_version=version,
        id=_uuid("evt"),
        ts=ts,
        trace_id=_uuid("trc"),
        producer=producer,
        intent=str(intent),
        subject=str(subject),
        capability=str(capability),
        annotations=dict(annotations or {}),
    )


@dataclass(slots=True)
class Task:
    """
    Canonical task: envelope + schema-named payload blob.
    The payload schema name should match capability family, e.g. "analyze.generic.v1".
    """
    envelope: Envelope
    payload_schema: str
    payload: Dict[str, Any]


@dataclass(slots=True)
class Artifact:
    """
    Canonical artifact emitted by providers and the engine.
    Examples:
      - kind="CodeBundle", uri="res://codebundle/<id>", sha256="<64-hex>", meta={...}
      - kind="DiffPatch",  uri="file://.../patch.diff", sha256="<64-hex>", meta={...}
      - kind="Result",     uri="spine://result/<cap>", sha256="", meta={"result": {...}}
      - kind="Text",       uri="spine://text/<cap>", sha256="", meta={"text": "..."}
      - kind="Problem",    uri="spine://problem/<cap>", sha256="", meta={"problem": {...}}
    """
    kind: str
    uri: str
    sha256: str
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Problem:
    """
    Canonical error/problem report.
    """
    code: str                # e.g., ValidationError | CapabilityUnavailable | ProviderError | ...
    message: str
    retryable: bool = False
    details: Dict[str, Any] = field(default_factory=dict)


# Convenience for serialization where needed
def to_dict(env: Envelope | Task | Artifact | Problem) -> Dict[str, Any]:
    return asdict(env)


