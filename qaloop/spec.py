"""Flow-spec loader and validator (schema v1).

A flow spec is a YAML document describing a deterministic browser flow:
setup steps, action steps with assertions, artifact/timeout policy.
See SPEC.md for the human-readable reference.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

import yaml

SPEC_VERSION = 1

_ENV_VAR = re.compile(r"\$(\w+|\{[^}]+\})")

ACTION_OPS = {
    "goto", "click", "dblclick", "fill", "press", "check", "uncheck",
    "select", "wait", "wait_ms", "reload", "back", "seed", "script",
}
STEP_META_KEYS = {"name", "expect", "continue_on_fail", "timeout_ms"}

ASSERTION_KEYS = {
    "visible", "hidden", "text_contains", "text_matches", "count",
    "url_contains", "title_contains", "noop", "console_clean", "js",
}


class SpecError(ValueError):
    """Raised when a flow spec is invalid."""


def envsubst(value: Any, where: str, strict: bool = True) -> Any:
    """Recursively expand $VAR / ${VAR} in strings.

    strict=True (default): a missing var is an error.
    strict=False: missing vars are left as-is (for `validate` without env).
    """
    if isinstance(value, str):
        def repl(m: "re.Match[str]") -> str:
            name = m.group(1).strip("{}")
            if name not in os.environ:
                if not strict:
                    return m.group(0)
                raise SpecError(f"{where}: env var ${name} is not set")
            return os.environ[name]

        return _ENV_VAR.sub(repl, value)
    if isinstance(value, dict):
        return {k: envsubst(v, where, strict) for k, v in value.items()}
    if isinstance(value, list):
        return [envsubst(v, where, strict) for v in value]
    return value


@dataclass
class Step:
    index: int
    phase: str  # setup | steps | teardown
    name: str
    op: str | None  # action op, or None for expect-only steps
    params: Any
    expect: dict[str, Any]
    expect_items: list[tuple[str, Any]]  # normalized (key, params), in order
    continue_on_fail: bool
    timeout_ms: int | None
    raw: dict


@dataclass
class FlowSpec:
    name: str
    description: str
    target: str
    viewport: dict[str, int]
    setup: list[Step]
    steps: list[Step]
    teardown: list[Step]
    artifacts: dict[str, Any]
    timeouts: dict[str, int]
    investigate_on_failure: bool
    max_investigation_actions: int
    source_path: str


def _validate_op_params(op: str | None, params: Any, where: str) -> None:
    if op is None:
        return
    if op in {"click", "dblclick", "check", "uncheck"}:
        if not isinstance(params, str) or not params:
            raise SpecError(f"{where}: {op} needs a selector string")
    elif op == "goto":
        if not isinstance(params, str) or not params:
            raise SpecError(f"{where}: goto needs a URL string")
    elif op == "fill":
        if not isinstance(params, dict) or "target" not in params or "text" not in params:
            raise SpecError(f"{where}: fill needs {{target, text}}")
    elif op == "press":
        if not isinstance(params, dict) or "target" not in params or "key" not in params:
            raise SpecError(f"{where}: press needs {{target, key}}")
    elif op == "select":
        if not isinstance(params, dict) or "target" not in params or "value" not in params:
            raise SpecError(f"{where}: select needs {{target, value}}")
    elif op == "wait":
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise SpecError(f"{where}: wait needs a mapping")
        state = params.get("state", "visible")
        if state not in {"visible", "hidden", "attached", "detached"}:
            raise SpecError(f"{where}: wait state must be visible|hidden|attached|detached")
    elif op == "wait_ms":
        if not isinstance(params, int) or params < 0:
            raise SpecError(f"{where}: wait_ms needs a non-negative integer")
    elif op in {"reload", "back"}:
        if params not in (True, None):
            raise SpecError(f"{where}: {op} takes no params (use `{op}: true`)")
    elif op == "seed":
        if not isinstance(params, dict) or not any(k in params for k in ("http", "js")):
            raise SpecError(f"{where}: seed needs {{http: ...}} or {{js: ...}}")
        if "http" in params:
            http = params["http"]
            if not isinstance(http, dict) or "url" not in http:
                raise SpecError(f"{where}: seed.http needs {{url, method?, json?}}")
    elif op == "script":
        if not isinstance(params, dict) or "js" not in params:
            raise SpecError(f"{where}: script needs {{js: ...}}")


def _validate_expect(expect: dict[str, Any] | list, where: str) -> list[tuple[str, Any]]:
    """Validate and normalize expect to an ordered list of (assertion, params).

    Accepts a mapping, or a list of single-key mappings (for repeated
    assertion types, e.g. two text_contains).
    """
    if isinstance(expect, dict):
        items = list(expect.items())
    elif isinstance(expect, list):
        items = []
        for i, item in enumerate(expect):
            if not isinstance(item, dict) or len(item) != 1:
                raise SpecError(
                    f"{where}: expect list items must be single-key mappings "
                    f"(item {i})")
            items.append(next(iter(item.items())))
    else:
        raise SpecError(f"{where}: expect must be a mapping or a list of mappings")
    for key, val in items:
        if key not in ASSERTION_KEYS:
            raise SpecError(
                f"{where}: unknown assertion '{key}' (known: {sorted(ASSERTION_KEYS)})")
        if key in {"visible", "hidden"}:
            if not isinstance(val, str) or not val:
                raise SpecError(f"{where}: assertion {key} needs a selector string")
        elif key in {"text_contains", "text_matches"}:
            if (not isinstance(val, dict) or "selector" not in val
                    or ("text" not in val and "pattern" not in val)):
                raise SpecError(
                    f"{where}: assertion {key} needs {{selector, text|pattern}}")
        elif key == "count":
            if (not isinstance(val, dict) or "selector" not in val
                    or not any(k in val for k in ("equals", "gte", "lte"))):
                raise SpecError(
                    f"{where}: assertion count needs {{selector, equals|gte|lte}}")
        elif key in {"url_contains", "title_contains"}:
            if not isinstance(val, str):
                raise SpecError(f"{where}: assertion {key} needs a string")
        elif key in {"noop", "console_clean"}:
            if val is not True:
                raise SpecError(f"{where}: assertion {key} must be `true`")
        elif key == "js":
            if (not isinstance(val, dict) or "script" not in val
                    or "contains" not in val):
                raise SpecError(
                    f"{where}: assertion js needs {{script, contains}}")
    return items


def _parse_step(raw: Any, index: int, phase: str, default_timeout_ms: int) -> Step:
    where = f"{phase}[{index}]"
    if not isinstance(raw, dict):
        raise SpecError(f"{where}: step must be a mapping, got {type(raw).__name__}")
    name = raw.get("name", "")
    if not isinstance(name, str):
        raise SpecError(f"{where}: name must be a string")
    expect = raw.get("expect") or {}
    if not isinstance(expect, (dict, list)):
        raise SpecError(f"{where}: expect must be a mapping or list of assertions")
    continue_on_fail = bool(raw.get("continue_on_fail", False))
    timeout_ms = raw.get("timeout_ms", default_timeout_ms)
    op_keys = [k for k in raw if k in ACTION_OPS]
    unknown = [k for k in raw if k not in ACTION_OPS and k not in STEP_META_KEYS]
    if unknown:
        raise SpecError(
            f"{where}: unknown keys {unknown} (known ops: {sorted(ACTION_OPS)})")
    if len(op_keys) > 1:
        raise SpecError(f"{where}: at most one action op per step, got {op_keys}")
    op = op_keys[0] if op_keys else None
    if op is None and not expect:
        raise SpecError(f"{where}: step has no action op and no expect — nothing to do")
    params = raw[op] if op else None
    _validate_op_params(op, params, where)
    expect_items = _validate_expect(expect, where)
    return Step(
        index=index, phase=phase,
        name=name or f"{op or 'expect'} #{index}",
        op=op, params=params, expect=expect, expect_items=expect_items,
        continue_on_fail=continue_on_fail, timeout_ms=timeout_ms, raw=raw,
    )


def _parse_steps(raw: Any, phase: str, default_timeout_ms: int,
               strict_env: bool = True) -> list[Step]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SpecError(f"{phase}: must be a list of steps")
    return [_parse_step(envsubst(s, f"{phase}[{i}]", strict_env), i, phase, default_timeout_ms)
            for i, s in enumerate(raw)]


def load_spec(path: str, strict_env: bool = True) -> FlowSpec:
    """Load and validate a flow-spec YAML file. Raises SpecError on problems."""
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    where = f"spec {path}"
    if not isinstance(raw, dict):
        raise SpecError(f"{where}: top level must be a mapping")
    version = raw.get("version", 1)
    if version != SPEC_VERSION:
        raise SpecError(f"{where}: unsupported version {version} (want {SPEC_VERSION})")
    name = raw.get("name")
    if not name or not isinstance(name, str):
        raise SpecError(f"{where}: `name` is required")
    timeouts = {"step_ms": 15000, "run_ms": 300000}
    timeouts.update(raw.get("timeouts") or {})
    default_step_ms = int(timeouts["step_ms"])
    target = envsubst(raw.get("target", ""), where, strict_env)
    if not target or not isinstance(target, str):
        raise SpecError(f"{where}: `target` is required (base URL, $VAR allowed)")
    viewport = raw.get("viewport") or {"width": 1280, "height": 800}
    artifacts = {"screenshot": "per-step", "ax_snapshot": "on-failure", "trace": True}
    artifacts.update(raw.get("artifacts") or {})
    if artifacts.get("screenshot") not in {"per-step", "on-failure", "none"}:
        raise SpecError(f"{where}: artifacts.screenshot must be per-step|on-failure|none")
    return FlowSpec(
        name=name,
        description=str(raw.get("description") or ""),
        target=target,
        viewport={"width": int(viewport.get("width", 1280)),
                  "height": int(viewport.get("height", 800))},
        setup=_parse_steps(raw.get("setup"), "setup", default_step_ms, strict_env),
        steps=_parse_steps(raw.get("steps"), "steps", default_step_ms, strict_env),
        teardown=_parse_steps(raw.get("teardown"), "teardown", default_step_ms, strict_env),
        artifacts=artifacts,
        timeouts={k: int(v) for k, v in timeouts.items()},
        investigate_on_failure=bool(raw.get("investigate_on_failure", True)),
        max_investigation_actions=int(raw.get("max_investigation_actions", 20)),
        source_path=path,
    )
