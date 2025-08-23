# File: backend/core/spine/validation.py
from __future__ import annotations

"""
Spine Validation Registry
=========================

Lightweight, pluggable validators for Spine inputs/outputs.

Design:
- Validators are simple callables that receive a single value and MUST
  raise an Exception (ValueError/TypeError/etc.) on invalid input.
- You can register validators per-capability (exact string match) and/or
  globally for all capabilities using the "*" wildcard.
- At dispatch time, the Registry calls:
    VALIDATORS.validate_input(capability, payload)
    ... provider executes ...
    VALIDATORS.validate_output(capability, baton_or_artifacts)

Convenience helpers:
- require_fields([...])  -> ensure dict contains named keys (not None/empty by default)
- optional_fields([...]) -> ensure keys, when present, satisfy a predicate
- is_dict() / is_list() / is_instance(type)
"""

from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union

ValidatorFn = Callable[[Any], None]


class ValidatorRegistry:
    """Holds input/output validators keyed by capability name (or '*')."""

    def __init__(self) -> None:
        self._input: Dict[str, List[ValidatorFn]] = {}
        self._output: Dict[str, List[ValidatorFn]] = {}

    # --------------------------- registration ---------------------------

    def register_input(self, capability: str, validator: ValidatorFn) -> None:
        """Register an input validator for a capability (or '*' for all)."""
        if not callable(validator):
            raise TypeError("validator must be callable")
        self._input.setdefault(capability, []).append(validator)

    def register_output(self, capability: str, validator: ValidatorFn) -> None:
        """Register an output validator for a capability (or '*' for all)."""
        if not callable(validator):
            raise TypeError("validator must be callable")
        self._output.setdefault(capability, []).append(validator)

    # --------------------------- invocation ----------------------------

    def validate_input(self, capability: str, value: Any) -> None:
        """Run '*' then capability-specific input validators; raise on failure."""
        self._run_all(self._collect(self._input, capability), value)

    def validate_output(self, capability: str, value: Any) -> None:
        """Run '*' then capability-specific output validators; raise on failure."""
        self._run_all(self._collect(self._output, capability), value)

    # --------------------------- internals -----------------------------

    @staticmethod
    def _collect(table: Dict[str, List[ValidatorFn]], capability: str) -> List[ValidatorFn]:
        out: List[ValidatorFn] = []
        if "*" in table:
            out.extend(table["*"])
        if capability in table:
            out.extend(table[capability])
        return out

    @staticmethod
    def _run_all(validators: Iterable[ValidatorFn], value: Any) -> None:
        errors: List[str] = []
        for v in validators:
            try:
                v(value)
            except Exception as e:
                errors.append(str(e) or e.__class__.__name__)
        if errors:
            raise ValueError("; ".join(errors))


# A global registry used by the Spine
VALIDATORS = ValidatorRegistry()

# ---------------------------------------------------------------------------
# Convenience validator factories
# ---------------------------------------------------------------------------

def require_fields(
    fields: Iterable[str],
    *,
    allow_empty: bool = False,
    allow_none: bool = False,
) -> ValidatorFn:
    """
    Ensure 'value' is a dict and contains each field in `fields`.
    By default disallows None/empty ("", [], {}) values.
    """
    fields = list(fields)

    def _validate(value: Any) -> None:
        if not isinstance(value, dict):
            raise TypeError("payload must be a dict")
        missing: List[str] = []
        empties: List[str] = []
        for k in fields:
            if k not in value:
                missing.append(k)
                continue
            v = value.get(k)
            if v is None and not allow_none:
                empties.append(k)
            elif (v == "" or v == [] or v == {}) and not allow_empty:
                empties.append(k)
        if missing:
            raise ValueError(f"missing required fields: {', '.join(missing)}")
        if empties:
            raise ValueError(f"empty/None fields not allowed: {', '.join(empties)}")

    return _validate


def optional_fields(
    fields: Iterable[str],
    *,
    predicate: Optional[Callable[[Any], bool]] = None,
) -> ValidatorFn:
    """
    If any of the fields are present, ensure they satisfy `predicate` (if given).
    """
    fields = list(fields)
    predicate = predicate or (lambda _v: True)

    def _validate(value: Any) -> None:
        if not isinstance(value, dict):
            raise TypeError("payload must be a dict")
        bad: List[str] = []
        for k in fields:
            if k in value and not predicate(value[k]):
                bad.append(k)
        if bad:
            raise ValueError(f"optional fields failed predicate: {', '.join(bad)}")

    return _validate


def is_dict() -> ValidatorFn:
    def _v(x: Any) -> None:
        if not isinstance(x, dict):
            raise TypeError("value must be a dict")
    return _v


def is_list() -> ValidatorFn:
    def _v(x: Any) -> None:
        if not isinstance(x, list):
            raise TypeError("value must be a list")
    return _v


def is_instance(t: type) -> ValidatorFn:
    def _v(x: Any) -> None:
        if not isinstance(x, t):
            raise TypeError(f"value must be instance of {t.__name__}")
    return _v


__all__ = [
    "ValidatorRegistry",
    "VALIDATORS",
    "require_fields",
    "optional_fields",
    "is_dict",
    "is_list",
    "is_instance",
]


# ---------------------------- static self-test ---------------------------------

if __name__ == "__main__":
    """
    Static checks for the validation registry & helpers.
    Exits non-zero on failure.
    """
    failures = 0

    # new isolated registry for tests (don't mutate the global one)
    reg = ValidatorRegistry()

    # 1) Register a global input validator and a per-cap validator
    reg.register_input("*", is_dict())
    reg.register_input("cap.foo", require_fields(["x", "y"]))

    try:
        reg.validate_input("cap.foo", {"x": 1, "y": 2})
        print("[validation.selftest] input ok: OK")
    except Exception as e:
        print("[validation.selftest] input ok: FAIL", e)
        failures += 1

    # 2) Missing field should fail
    try:
        reg.validate_input("cap.foo", {"x": 1})
        print("[validation.selftest] input missing: FAIL")
        failures += 1
    except Exception:
        print("[validation.selftest] input missing: OK")

    # 3) Output validators (list expected)
    reg.register_output("*", is_list())
    try:
        reg.validate_output("cap.any", [{"a": 1}])
        print("[validation.selftest] output ok: OK")
    except Exception as e:
        print("[validation.selftest] output ok: FAIL", e)
        failures += 1

    try:
        reg.validate_output("cap.any", {"not": "a list"})
        print("[validation.selftest] output type check: FAIL")
        failures += 1
    except Exception:
        print("[validation.selftest] output type check: OK")

    raise SystemExit(failures)


