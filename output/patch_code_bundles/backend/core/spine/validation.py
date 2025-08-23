# File: v2/backend/core/spine/validation.py
from __future__ import annotations

"""
Validation registry for Spine capabilities.

- Providers can register input/output validators for a capability.
- The Registry invokes VALIDATORS.validate_input/validate_output before/after provider calls.
- Validators raise Exceptions on failure; they return None on success.

Keep validators *fast* and side-effect free.
"""

from typing import Any, Callable, Dict, Iterable, Optional, Sequence


# ------------------------------ Types ------------------------------------------

Validator = Callable[[Any], None]


# ------------------------------ Registry ---------------------------------------

class ValidationRegistry:
    """
    Holds per-capability input/output validators.

    Usage:
      VALIDATORS.register_input("docstrings.scan.python.v1", require_fields(["roots"]))
      VALIDATORS.register_output("docstrings.scan.python.v1", forbid_empty(["stats"]))
    """

    def __init__(self) -> None:
        self._in: Dict[str, Validator] = {}
        self._out: Dict[str, Validator] = {}

    # ---- Registration ----

    def register_input(self, capability: str, validator: Validator) -> None:
        if not callable(validator):
            raise TypeError("input validator must be callable")
        self._in[capability] = validator

    def register_output(self, capability: str, validator: Validator) -> None:
        if not callable(validator):
            raise TypeError("output validator must be callable")
        self._out[capability] = validator

    # ---- Lookup ----

    def get_input(self, capability: str) -> Optional[Validator]:
        return self._in.get(capability)

    def get_output(self, capability: str) -> Optional[Validator]:
        return self._out.get(capability)

    # ---- Execution ----

    def validate_input(self, capability: str, payload: Any) -> None:
        v = self._in.get(capability)
        if v:
            v(payload)

    def validate_output(self, capability: str, result: Any) -> None:
        v = self._out.get(capability)
        if v:
            v(result)


# Global, shared registry
VALIDATORS = ValidationRegistry()


# ------------------------------ Convenience API --------------------------------

def register_input_validator(capability: str, validator: Validator) -> None:
    VALIDATORS.register_input(capability, validator)


def register_output_validator(capability: str, validator: Validator) -> None:
    VALIDATORS.register_output(capability, validator)


def get_input_validator(capability: str) -> Optional[Validator]:
    return VALIDATORS.get_input(capability)


def get_output_validator(capability: str) -> Optional[Validator]:
    return VALIDATORS.get_output(capability)


# ------------------------------ Helper validators ------------------------------

def require_fields(fields: Iterable[str]) -> Validator:
    """
    Ensure payload is a dict and contains all required keys (any value allowed).
    """
    required = tuple(fields)

    def _v(payload: Any) -> None:
        if not isinstance(payload, dict):
            raise ValueError(f"payload must be dict with keys {list(required)}, got {type(payload).__name__}")
        missing = [k for k in required if k not in payload]
        if missing:
            raise ValueError(f"missing required fields: {missing}")

    return _v


def forbid_empty(fields: Iterable[str]) -> Validator:
    """
    Ensure these keys, if present in a dict, are not None or empty string.
    """
    keys = tuple(fields)

    def _v(obj: Any) -> None:
        if not isinstance(obj, dict):
            return
        empty = [k for k in keys if (k in obj and (obj[k] is None or obj[k] == ""))]
        if empty:
            raise ValueError(f"empty disallowed fields: {empty}")

    return _v


def all_of(*validators: Validator) -> Validator:
    """
    Compose validators; all must pass.
    """
    vals: Sequence[Validator] = validators

    def _v(obj: Any) -> None:
        for fn in vals:
            fn(obj)

    return _v


def any_of(*validators: Validator) -> Validator:
    """
    Compose validators; at least one must pass. If all fail, raise the last error.
    """

    vals: Sequence[Validator] = validators

    def _v(obj: Any) -> None:
        last_err: Optional[Exception] = None
        for fn in vals:
            try:
                fn(obj)
                return
            except Exception as e:
                last_err = e
        if last_err:
            raise last_err
        # If no validators provided, treat as pass-through

    return _v

