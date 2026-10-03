"""Flow-spec loader and validator (schema v1).

A flow spec is a YAML document describing a deterministic browser flow:
setup steps, action steps with assertions, artifact/timeout policy.
See SPEC.md for the human-readable reference.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

SPEC_VERSION = 1

_ENV_VAR = re.compile(r"\$(\w+|\{[^}]+\})")

ACTION_OPS = {
    "goto", "click", "dblclick", "fill", "press", "check", "uncheck",
    "select", "wait", "wait_ms", "reload", "back", "seed", "script",
    "mock",
}
STEP_META_KEYS = {"name", "expect", "continue_on_fail", "timeout_ms", "retry"}

ASSERTION_KEYS = {
    "visible", "hidden", "text_contains", "text_matches", "count",
    "url_contains", "title_contains", "noop", "console_clean", "js",
    "screenshot_matches", "ax",
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
    retry: int = 0  # extra attempts after a failure (steps phase only)


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
    services: list[dict] = field(default_factory=list)


_SERVICE_NAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")


def _validate_services(raw: Any, where: str) -> list[dict]:
    """Validate the top-level `services:` block.

    Each service boots a real process before the flow runs:
      services:
        - name: api
          command: "python3 app.py --port 8000"
          cwd: ../myapp              # optional, relative to the flow file
          env: {PORT: "8000"}         # optional
          wait: {http: "http://127.0.0.1:8000/health"}  # or {port: 8000} or {log_contains: "ready"}
          timeout_s: 60               # optional, default 90
    After boot, QALOOP_SERVICE_<NAME>_URL (and _PORT when a port is known)
    are exported, so target/steps can use ${QALOOP_SERVICE_API_URL}.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SpecError(f"{where}: `services` must be a list")
    out = []
    for i, s in enumerate(raw):
        w = f"{where}: services[{i}]"
        if not isinstance(s, dict):
            raise SpecError(f"{w}: service must be a mapping")
        name = s.get("name")
        if not isinstance(name, str) or not _SERVICE_NAME.match(name):
            raise SpecError(f"{w}: service `name` must be an identifier "
                            f"[a-zA-Z][a-zA-Z0-9_]*")
        if not isinstance(s.get("command"), str) or not s["command"]:
            raise SpecError(f"{w}: service `command` is required (string)")
        wait = s.get("wait")
        if not isinstance(wait, dict) or len(wait) != 1 or \
                next(iter(wait)) not in {"port", "http", "log_contains"}:
            raise SpecError(f"{w}: service `wait` must be exactly one of "
                            f"{{port, http, log_contains}}")
        timeout_s = s.get("timeout_s", 90)
        if not isinstance(timeout_s, (int, float)) or timeout_s <= 0:
            raise SpecError(f"{w}: service `timeout_s` must be positive")
        url = s.get("url")
        if url is not None and (not isinstance(url, str) or not url):
            raise SpecError(f"{w}: service `url` must be a non-empty string")
        out.append({
            "name": name,
            "command": s["command"],
            "cwd": s.get("cwd"),
            "env": dict(s.get("env") or {}),
            "wait": wait,
            "timeout_s": float(timeout_s),
            "url": url,
        })
    names = [s["name"] for s in out]
    if len(set(names)) != len(names):
        raise SpecError(f"{where}: duplicate service names {names}")
    return out


def load_services_light(path: str) -> tuple[str, list[dict]]:
    """Parse just the flow name + services block, without env substitution.

    Used to boot services BEFORE the full spec load, so that service URLs
    are available as ${QALOOP_SERVICE_<NAME>_URL} during env substitution.
    Never boots anything itself.
    """
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    where = f"spec {path}"
    if not isinstance(raw, dict):
        raise SpecError(f"{where}: top level must be a mapping")
    name = raw.get("name")
    if not name or not isinstance(name, str):
        raise SpecError(f"{where}: `name` is required")
    return name, _validate_services(raw.get("services"), where)


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
    elif op == "mock":
        # Network interception: canned responses for matching requests.
        # Register in setup before the goto that triggers the requests.
        if not isinstance(params, dict) or "url" not in params:
            raise SpecError(f"{where}: mock needs {{url, ...}}")
        if not isinstance(params["url"], str) or not params["url"]:
            raise SpecError(f"{where}: mock.url must be a non-empty string (glob ok)")
        body_keys = [k for k in ("json", "body", "path") if k in params]
        if len(body_keys) != 1:
            raise SpecError(
                f"{where}: mock needs exactly one of {{json, body, path}} "
                f"(got {body_keys or 'none'})")
        if "status" in params:
            s = params["status"]
            if not isinstance(s, int) or not 100 <= s <= 599:
                raise SpecError(f"{where}: mock.status must be an HTTP status int")
        if "method" in params and params["method"] not in {
                "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
            raise SpecError(f"{where}: mock.method must be an HTTP method")
        if "times" in params:
            t = params["times"]
            if not isinstance(t, int) or t < 1:
                raise SpecError(f"{where}: mock.times must be a positive int")


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
        elif key == "screenshot_matches":
            if not isinstance(val, dict) or "baseline" not in val:
                raise SpecError(
                    f"{where}: assertion screenshot_matches needs "
                    f"{{baseline, selector?, max_diff?}}")
            if not isinstance(val["baseline"], str) or not val["baseline"]:
                raise SpecError(
                    f"{where}: screenshot_matches.baseline must be a path string")
            if "selector" in val and (not isinstance(val["selector"], str)
                                      or not val["selector"]):
                raise SpecError(
                    f"{where}: screenshot_matches.selector must be "
                    f"a non-empty string")
            md = val.get("max_diff", 0.02)
            if not isinstance(md, (int, float)) or not 0 <= md <= 1:
                raise SpecError(
                    f"{where}: screenshot_matches.max_diff must be 0..1")
        elif key == "ax":
            if not isinstance(val, dict) or "role" not in val:
                raise SpecError(
                    f"{where}: assertion ax needs {{role, name?, state?}}")
            if not isinstance(val["role"], str) or not val["role"]:
                raise SpecError(f"{where}: ax.role must be an ARIA role string")
            if val.get("state", "visible") not in {"visible", "hidden", "attached"}:
                raise SpecError(f"{where}: ax.state must be visible|hidden|attached")
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
    retry = raw.get("retry", 0)
    if isinstance(retry, bool) or not isinstance(retry, int) or retry < 0:
        raise SpecError(
            f"{where}: retry must be an integer >= 0, got {retry!r}")
    if retry > 0 and phase != "steps":
        raise SpecError(
            f"{where}: retry > 0 is only allowed in the steps phase "
            f"(got phase '{phase}'; mocks register once, "
            f"re-registration semantics are undefined)")
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
        continue_on_fail=continue_on_fail, timeout_ms=timeout_ms,
        retry=retry, raw=raw,
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
        services=_validate_services(raw.get("services"), where),
    )
