#v2/backend/core/spine/bootstrap.py
"""
Spine bootstrap & runner (YAML-driven middlewares, no env).

This module provides:
- A `Spine` class: thin façade around the Registry + a declarative pipeline runner.
- `load_middlewares_from_config()` to plug in middlewares via config/spine.yml.
- `setup_registry()` to load capabilities from YAML.
- `build_spine()` convenience to construct a Spine from a capabilities YAML.

The pipeline runner supports the variable/conditional syntax used in patch_loop.yml:
- ${VAR} or ${VAR:default}
- ${path.to.value?then_expr:else_expr}
- Paths can refer to prior steps, e.g. ${fetch.result.rows}
- Expressions can nest: ${enrich.result?${enrich.result}:${items}}

Notes:
- Environment variable based middleware loading has been removed.
- All paths/middlewares must be supplied by callers or read via the centralized loader.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, TYPE_CHECKING
import re
import yaml

from .loader import CapabilitiesLoader
from .registry import Registry
from .contracts import Artifact, Envelope, Task, new_envelope, to_dict

# Middlewares are callables with the Registry middleware signature.
Middleware = Callable[
    [Callable[[str, "Task", Dict[str, Any]], List["Artifact"]], str, "Task", Dict[str, Any]],
    List["Artifact"],
]

if TYPE_CHECKING:
    from .contracts import Problem  # noqa: F401


# --------------------------------------------------------------------------------------
# Utilities for importing callables from "module:function" strings
# --------------------------------------------------------------------------------------

def _split_target(target: str) -> Tuple[str, str]:
    if ":" not in target:
        raise ValueError(f"invalid target (expected 'module:function'): {target!r}")
    mod, fn = target.split(":", 1)
    return mod.strip(), fn.strip()


def _import_callable(target: str) -> Callable[..., Any]:
    mod_name, fn_name = _split_target(target)
    mod = import_module(mod_name)
    fn = getattr(mod, fn_name, None)
    if not callable(fn):
        raise ValueError(f"target is not callable: {target}")
    return fn


# --------------------------------------------------------------------------------------
# Middleware loading from centralized config
# --------------------------------------------------------------------------------------

def load_middlewares_from_config(targets: Optional[List[str]] = None) -> List[Middleware]:
    """
    Load middlewares specified as ["pkg.mod:fn", ...].

    Callers typically obtain the list from the centralized loader:
        from v2.backend.core.configuration.loader import get_spine
        mws = load_middlewares_from_config(list(get_spine().middlewares))

    Returns:
        List[Middleware]
    """
    out: List[Middleware] = []
    for p in (targets or []):
        p = (p or "").strip()
        if not p:
            continue
        fn = _import_callable(p)
        out.append(fn)  # type: ignore[arg-type]
    return out


# --------------------------------------------------------------------------------------
# Registry construction helpers (capabilities YAML → Registry)
# --------------------------------------------------------------------------------------

def setup_registry(caps_path: str | Path, middlewares: Optional[List[Middleware]] = None) -> Registry:
    """
    Create a Registry, add middlewares, and load capabilities from YAML.
    """
    caps = Path(caps_path).resolve()
    reg = Registry()
    for mw in (middlewares or []):
        reg.add_middleware(mw)
    CapabilitiesLoader(caps).load(reg)
    return reg


def build_spine(caps_path: str | Path, middlewares: Optional[List[Middleware]] = None) -> "Spine":
    """
    Convenience: return a Spine instance wired with capabilities + middlewares.
    """
    reg = setup_registry(caps_path, middlewares=middlewares)
    return Spine(registry=reg)


# --------------------------------------------------------------------------------------
# Spine class (façade + pipeline runner)
# --------------------------------------------------------------------------------------

_MISSING = object()


@dataclass
class Spine:
    """
    Thin façade around the Registry with a small pipeline runner.

    You can construct it in two ways:
      - Spine(caps_path=..., middlewares=[...]) -> builds its own Registry
      - Spine(registry=existing_registry) -> uses the provided Registry
    """
    registry: Optional[Registry] = None
    caps_path: Optional[str | Path] = None
    middlewares: Optional[List[Middleware]] = None

    # ------------------------- construction -------------------------

    def __post_init__(self) -> None:
        if self.registry is None:
            if self.caps_path is None:
                raise TypeError("Spine requires either 'registry' or 'caps_path'")
            self.registry = setup_registry(self.caps_path, middlewares=self.middlewares or [])

    @classmethod
    def from_registry(cls, registry: Registry) -> "Spine":
        return cls(registry=registry)

    @classmethod
    def from_capabilities(
        cls, caps_path: str | Path, middlewares: Optional[List[Middleware]] = None,
    ) -> "Spine":
        return cls(caps_path=caps_path, middlewares=middlewares)

    # ------------------------- simple dispatch ----------------------

    def run(
        self, capability: str, payload: Dict[str, Any] | None = None, context: Dict[str, Any] | None = None,
    ) -> List[Artifact]:
        """Dispatch a single capability with a payload via the Registry."""
        assert self.registry is not None
        env: Envelope = new_envelope(
            intent="run", subject="-", capability=capability, producer="spine",
        )
        task = Task(envelope=env, payload_schema=capability, payload=dict(payload or {}))
        return self.registry.dispatch_task(task, context=dict(context or {}))

    # Compatibility alias for older callers (envelope overrides supported)
    def dispatch_capability(
        self,
        capability: str,
        payload: Dict[str, Any] | None = None,
        context: Dict[str, Any] | None = None,
        *,
        intent: str = "run",
        subject: str = "-",
        producer: str = "spine",
        payload_schema: Optional[str] = None,
    ) -> List[Artifact]:
        """
        Backward-compatible alias for run() that also accepts envelope overrides.
        """
        assert self.registry is not None
        env: Envelope = new_envelope(
            intent=intent, subject=subject, capability=capability, producer=producer,
        )
        task = Task(
            envelope=env, payload_schema=(payload_schema or capability), payload=dict(payload or {}),
        )
        return self.registry.dispatch_task(task, context=dict(context or {}))

    # ------------------------- pipeline runner ----------------------

    _VAR_RX = re.compile(r"\$\{([^{}]+)\}")

    def load_pipeline_and_run(
        self, pipeline_yaml: str | Path, *, variables: Dict[str, Any] | None = None,
    ) -> List[Artifact]:
        """
        Execute a YAML pipeline file with `${...}` substitution and simple conditionals.

        Returns the artifacts from the **last executed step**.
        """
        assert self.registry is not None
        p = Path(pipeline_yaml).resolve()
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict) or "steps" not in data:
            raise ValueError(f"pipeline YAML missing 'steps': {p}")

        defaults: Dict[str, Any] = dict(data.get("defaults") or {})
        vars_in: Dict[str, Any] = dict(defaults)
        vars_in.update(dict(variables or {}))

        # state collects both variables and step outputs (batons)
        state: Dict[str, Any] = {"vars": vars_in}
        last_artifacts: List[Artifact] = []

        for idx, step in enumerate(data.get("steps") or [], 1):
            if not isinstance(step, dict):
                raise ValueError(f"step {idx} must be a mapping")
            step_id: str = str(step.get("id") or f"step{idx}")

            capability: str = str(step.get("capability") or "").strip()
            if not capability:
                raise ValueError(f"step {step_id}: missing capability")

            # when: guard (default True)
            when_expr = step.get("when", True)
            should_run = self._coerce_bool(self._resolve(when_expr, state))
            if not should_run:
                continue

            raw_payload = step.get("payload") or {}
            payload = self._resolve(raw_payload, state)

            # Dispatch
            env = new_envelope(intent="pipeline", subject=step_id, capability=capability, producer="spine.pipeline")
            task = Task(envelope=env, payload_schema=capability, payload=payload)
            arts = self.registry.dispatch_task(task, context={"vars": vars_in, "state": state, "step": step_id})
            last_artifacts = arts

            # Capture a baton-like object to expose as ${.result}
            if len(arts) == 1 and isinstance(arts[0].meta, dict) and "result" in arts[0].meta:
                baton: Any = arts[0].meta["result"]
            else:
                baton = [to_dict(a) for a in arts]
            state[step_id] = {"result": baton}

        return last_artifacts

    # ------------------------- templating ---------------------------

    def _resolve(self, obj: Any, state: Dict[str, Any]) -> Any:
        """
        Deep-resolve ${...} in strings, lists, dicts.

        If a string is exactly a single ${...} token, the resolved value is returned
        *as-is* (can be a non-string like list/dict/bool). Otherwise tokens are stringified.
        """
        if isinstance(obj, dict):
            return {k: self._resolve(v, state) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._resolve(v, state) for v in obj]
        if isinstance(obj, str):
            tokens = list(self._VAR_RX.finditer(obj))
            if not tokens:
                return obj

            # If the entire string is one token, return the evaluated value directly.
            if len(tokens) == 1 and tokens[0].span() == (0, len(obj)):
                expr = tokens[0].group(1)
                return self._eval_expr(expr, state)

            # Otherwise, interpolate all tokens to string
            out = []
            last = 0
            for m in tokens:
                out.append(obj[last:m.start()])
                out.append(self._stringify(self._eval_expr(m.group(1), state)))
                last = m.end()
            out.append(obj[last:])
            return "".join(out)
        return obj

    def _eval_expr(self, expr: str, state: Dict[str, Any]) -> Any:
        """
        Evaluate expression inside ${...}.

        - Ternary: ?:
        - Default: NAME:default
        - Path/Var: foo.bar.baz or VAR
        """
        expr = expr.strip()

        # Handle ternary a?b:c with nesting (split only at top level)
        q_idx = self._top_level_char(expr, "?")
        if q_idx != -1:
            cond = expr[:q_idx].strip()
            rest = expr[q_idx + 1 :]
            c_idx = self._top_level_char(rest, ":")
            if c_idx == -1:
                raise ValueError(f"invalid ternary expression: {expr!r}")
            then_part = rest[:c_idx].strip()
            else_part = rest[c_idx + 1 :].strip()
            cond_val = self._resolve_path_or_var(cond, state, missing_ok=True)
            branch = then_part if self._truthy(cond_val) else else_part
            # Support nested ${...} in branches by resolving as a standalone string
            if branch.startswith("${") and branch.endswith("}"):
                return self._resolve(branch, state)
            return self._parse_literal(branch)

        # Handle default NAME:default (only when there's a single top-level ':' )
        c_idx = self._top_level_char(expr, ":")
        if c_idx != -1:
            name = expr[:c_idx].strip()
            default_raw = expr[c_idx + 1 :].strip()
            val = self._resolve_path_or_var(name, state, missing_ok=True)
            # Treat None/empty-string as missing so defaults work even if blank
            if val is not _MISSING and not (val is None or (isinstance(val, str) and val.strip() == "")):
                return val
            return self._parse_literal(default_raw)

        # Simple name/path
        val = self._resolve_path_or_var(expr, state, missing_ok=False)
        return val

    @staticmethod
    def _parse_literal(text: str) -> Any:
        """
        Parse a YAML literal (so '[]', 'true', '123' become correct Python types).
        If parsing fails, return the raw string.
        """
        try:
            return yaml.safe_load(text)
        except Exception:
            return text

    @staticmethod
    def _stringify(v: Any) -> str:
        if isinstance(v, (dict, list)):
            return yaml.safe_dump(v, sort_keys=False).strip()
        return str(v)

    @staticmethod
    def _truthy(v: Any) -> bool:
        if isinstance(v, str):
            return v.strip().lower() not in {"", "0", "false", "no", "off", "none", "null"}
        return bool(v)

    @staticmethod
    def _coerce_bool(v: Any) -> bool:
        return Spine._truthy(v)

    @staticmethod
    def _top_level_char(s: str, ch: str) -> int:
        """Find index of `ch` not inside ${...} tokens."""
        depth = 0
        i = 0
        while i < len(s):
            if s.startswith("${", i):
                depth += 1
                i += 2
                continue
            if depth and s[i] == "}":
                depth -= 1
                i += 1
                continue
            if depth == 0 and s[i] == ch:
                return i
            i += 1
        return -1

    def _resolve_path_or_var(self, name: str, state: Dict[str, Any], *, missing_ok: bool) -> Any:
        """
        Resolve a dotted path against (1) step outputs, then (2) vars.

        Examples: "fetch.result", "fetch.result.rows", "ITEMS".
        """
        name = name.strip()
        root_key, *rest = name.split(".")

        # First check step results by id
        if root_key in state:
            cur: Any = state[root_key]
        else:
            # look in vars
            vars_map = state.get("vars", {})
            if root_key in vars_map:
                cur = vars_map[root_key]
            else:
                return _MISSING if missing_ok else (_ for _ in ()).throw(KeyError(f"unknown name: {name!r}"))

        for key in rest:
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                cur = None
                break
        return cur


# ---------------------------- static self-test ---------------------------------

if __name__ == "__main__":
    """
    Minimal static tests:
      1) Build a registry from a temp YAML that points to a local provider.
      2) Exercise constructor variants (caps_path vs registry).
      3) Run a two-step pipeline with guards and interpolation.
      4) Verify dispatch_capability envelope overrides.
    Exits non-zero on failure.
    """
    import tempfile

    failures = 0

    # Local echo provider for YAML reference
    def _echo_provider(task, context):
        return [{"kind": "Result", "uri": "spine://result/echo", "sha256": "", "meta": {"result": {"echo": dict(task.payload or {})}}}]

    # 1) Build via caps_path and dispatch a task
    with tempfile.TemporaryDirectory() as td:
        yml = Path(td) / "caps.yml"
        yml.write_text(f"unit.test.echo.v1:\n  target: {__name__}:_echo_provider\n", encoding="utf-8")
        s1 = Spine(caps_path=yml)
        arts = s1.dispatch_capability("unit.test.echo.v1", {"x": 1})
        ok1 = len(arts) == 1 and (arts[0].meta or {}).get("result", {}).get("echo", {}).get("x") == 1
        print("[bootstrap.selftest] run via caps_path:", "OK" if ok1 else "FAIL")
        failures += 0 if ok1 else 1

        # 2) Build via registry
        reg = setup_registry(yml)
        s2 = Spine(registry=reg)
        arts2 = s2.dispatch_capability("unit.test.echo.v1", {"y": 2})
        ok2 = len(arts2) == 1 and (arts2[0].meta or {}).get("result", {}).get("echo", {}).get("y") == 2
        print("[bootstrap.selftest] run via registry:", "OK" if ok2 else "FAIL")
        failures += 0 if ok2 else 1

        # 3) Pipeline: guard + echo
        pipe = Path(td) / "pipe.yml"
        pipe.write_text(
            "\n".join(
                [
                    "steps:",
                    "- id: s1",
                    "  when: ${RUN:true}",
                    "  capability: unit.test.echo.v1",
                    "  payload:",
                    "    msg: ${msg:hello}",
                    "- id: s2",
                    "  capability: unit.test.echo.v1",
                    "  payload:",
                    "    prev: ${s1.result.echo.msg}",
                ]
            ),
            encoding="utf-8",
        )
        arts3 = s1.load_pipeline_and_run(pipe, variables={"msg": "hi"})
        ok3 = len(arts3) == 1 and (arts3[0].meta or {}).get("result", {}).get("echo", {}).get("prev") == "hi"
        print("[bootstrap.selftest] pipeline:", "OK" if ok3 else "FAIL")
        failures += 0 if ok3 else 1

        # 4) Envelope overrides via dispatch_capability
        arts4 = s1.dispatch_capability(
            "unit.test.echo.v1",
            {"z": 3},
            intent="custom",
            subject="subj",
            producer="tester",
            payload_schema="unit.test.echo.v1",
        )
        ok4 = len(arts4) == 1 and (arts[0].meta or {}).get("result", {}).get("echo", {}).get("z") == 3
        print("[bootstrap.selftest] dispatch overrides:", "OK" if ok4 else "FAIL")
        failures += 0 if ok4 else 1

    raise SystemExit(failures)
