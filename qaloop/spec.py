"""Flow-spec loader and validator (schema v1).

A flow spec is a YAML document describing a deterministic browser flow:
setup steps, action steps with assertions, artifact/timeout policy.
See SPEC.md for the human-readable reference.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from itertools import product as _cartesian_product
from typing import Any

import yaml

SPEC_VERSION = 1

_ENV_VAR = re.compile(r"\$(\w+|\{[^}]+\})")

ACTION_OPS = {
    "goto", "click", "dblclick", "fill", "press", "hover", "scroll", "check",
    "uncheck",
    "select", "wait", "wait_ms", "reload", "back", "seed", "script",
    "mock", "upload", "drag", "type", "focus",
}
STEP_META_KEYS = {"name", "expect", "continue_on_fail", "timeout_ms", "retry",
                 "retry_delay_ms",  # issue #95 — delay between retry attempts
                 "on_dialog", "frame", "store"}  # issue #87 — flow variables

ASSERTION_KEYS = {
    "visible", "hidden", "text_contains", "text_matches", "count",
    "mock_calls",
    "network_calls",
    "url_contains", "title_contains", "noop", "console_clean", "js",
    "screenshot_matches", "ax", "dialog",
    "attribute", "value", "focused",  # issue #81 — element state
    "cookie", "storage",  # issue #82 — cookies and web storage
    "request_body",  # issue #83 — opt-in request-body assertions
    "perf",  # issue #84 — Web Vitals performance budgets
    "a11y",  # issue #85 — axe-core accessibility scan
}

# Step-level dialog policy: what the runner does when the page raises a JS
# dialog during the step. "dismiss" preserves Playwright's default behavior.
DIALOG_POLICIES = {"accept", "dismiss"}
DIALOG_TYPES = {"alert", "confirm", "prompt", "beforeunload"}

# Issue #84: metric names supported by the `perf` assertion.
PERF_METRICS = {"lcp_ms", "cls", "inp_ms"}


class SpecError(ValueError):
    """Raised when a flow spec is invalid."""


def envsubst(value: Any, where: str, strict: bool = True,
             skip_names: set | None = None) -> Any:
    """Recursively expand $VAR / ${VAR} in strings.

    strict=True (default): a missing var is an error.
    strict=False: missing vars are left as-is (for `validate` without env).
    skip_names: issue #87 — declared runtime store variables. ${name}
    references to these are left untouched so the runner can interpolate
    them from the run-scoped variable table instead.
    """
    if isinstance(value, str):
        def repl(m: "re.Match[str]") -> str:
            name = m.group(1).strip("{}")
            if skip_names and name in skip_names:
                return m.group(0)
            if name not in os.environ:
                if not strict:
                    return m.group(0)
                raise SpecError(f"{where}: env var ${name} is not set")
            return os.environ[name]

        return _ENV_VAR.sub(repl, value)
    if isinstance(value, dict):
        return {k: envsubst(v, where, strict, skip_names) for k, v in value.items()}
    if isinstance(value, list):
        return [envsubst(v, where, strict, skip_names) for v in value]
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
    # issue #95 — ms to wait between retry attempts; default 0 keeps the
    # back-to-back behavior; > 0 requires retry > 0 in the steps phase
    retry_delay_ms: int = 0
    on_dialog: str = "dismiss"  # accept | dismiss — JS dialog policy for the step
    frame: str | None = None  # issue #80 — iframe/frame scope: frame name
    # or URL substring; selectors resolve via page.frame(name|url) instead of
    # the main frame
    store: dict = field(default_factory=dict)  # issue #87 — flow
    # variables: {name: {js: <expr>}} evaluated after the step; the
    # captured str(result) feeds ${name} interpolation in later steps


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
    matrix: list[dict] = field(default_factory=list)  # issue #88 —
    # data-driven combinations: each dict is one combination's variable
    # map, expanded from the `matrix:` block by _validate_matrix
    tags: list[str] = field(default_factory=list)  # issue #89 —
    # batch selection labels, validated by _validate_tags
    # Issue #83: opt-in request-body recording. Default False keeps the
    # network_log schema and privacy posture unchanged (SPEC.md).
    record_bodies: bool = False
    # Issue #90: opt-in video recording of the run. Default False keeps
    # the privacy-by-default posture (videos are large). Saved as
    # <run_dir>/video.webm (see SPEC.md).
    video: bool = False


# Issue #89: flow tags for batch selection. A tag starts with an
# alphanumeric and may then contain alphanumerics, _, ., - — shell-safe
# and slug-safe.
_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _validate_tags(raw: Any, where: str) -> list[str]:
    """Validate the top-level `tags:` block (issue #89).

    A list of non-empty strings matching ^[A-Za-z0-9][A-Za-z0-9_.-]*$,
    no duplicates. There is no unknown-top-level-key guard, so tags
    must be explicitly validated here — unknown keys are otherwise
    silently ignored.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SpecError(f"{where}: `tags` must be a list of tag strings, "
                        f"got {type(raw).__name__}")
    seen = set()
    for i, tag in enumerate(raw):
        w = f"{where}: tags[{i}]"
        if not isinstance(tag, str) or not tag:
            raise SpecError(
                f"{w}: tag must be a non-empty string, got {tag!r}")
        if not _TAG.match(tag):
            raise SpecError(
                f"{w}: tag {tag!r} must match ^[A-Za-z0-9][A-Za-z0-9_.-]*$")
        if tag in seen:
            raise SpecError(f"{w}: duplicate tag {tag!r}")
        seen.add(tag)
    return list(raw)


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
    Never boots anything itself. Raises SpecError when the file cannot
    be parsed (issue #89: the batch path records this as an INVALID-SPEC
    failure instead of crashing).
    """
    with open(path, encoding="utf-8") as f:
        text = f.read()
    try:
        raw = yaml.safe_load(text)
    except Exception as e:
        raise SpecError(f"spec {path}: cannot parse YAML ({e})")
    where = f"spec {path}"
    if not isinstance(raw, dict):
        raise SpecError(f"{where}: top level must be a mapping")
    name = raw.get("name")
    if not name or not isinstance(name, str):
        raise SpecError(f"{where}: `name` is required")
    return name, _validate_services(raw.get("services"), where)


# Issue #86: include-step composition. Keys an include step may carry
# (`name` is allowed as documentation and ignored); everything else is
# rejected so nothing about the inlining is ambiguous.
_INCLUDE_STEP_KEYS = {"include", "with", "name"}
# Keys a fragment file may carry. setup/teardown/services/target are
# rejected fail-closed: a fragment is a step list, and anything else
# would be silently dropped.
_FRAGMENT_KEYS = {"name", "description", "steps"}
# Allowed `with:` value types: scalars only, stringified at compose time.
_WITH_SCALARS = (str, int, float, bool)


def _substitute_with(value: Any, with_map: dict[str, str], where: str) -> Any:
    """Substitute ${name} / $name in string values from the include `with:` map.

    Only names present in with_map are substituted (longest name first, so
    ${username} wins over ${user}). Anything else is left untouched so the
    later envsubst pass treats it as an environment variable, exactly as
    today — this is the seam with issue #87 (runtime capture via store),
    which is explicitly out of scope here: this substitution is
    compose-time only and never touches runtime interpolation.
    """
    if not with_map:
        return value
    alt = "|".join(re.escape(n) for n in sorted(with_map, key=len, reverse=True))
    pat = re.compile(r"\$(?:\{(" + alt + r")\}|(" + alt + r"))")

    def repl(m: "re.Match[str]") -> str:
        return with_map[m.group(1) or m.group(2)]

    if isinstance(value, str):
        return pat.sub(repl, value)
    if isinstance(value, dict):
        return {k: _substitute_with(v, with_map, where) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_with(v, with_map, where) for v in value]
    return value


def _resolve_includes(raw_steps: Any, including_path: str,
                      stack: tuple[str, ...], depth: int = 0) -> list:
    """Expand include steps in a RAW step list (before _parse_steps).

    Returns a flat list of (step, depth) pairs: `depth` is the number of
    include levels the step has passed through (0 = the flow's own steps).
    A parent include's `with:` substitution is applied only to the
    fragment's own steps (depth+1), never to steps from a nested include
    (deeper) — lexical scoping per include level. load_spec flattens the
    pairs after the substitution pass.
    """
    if not isinstance(raw_steps, list):
        return raw_steps  # not a list: _parse_steps raises the real error
    out: list = []
    base_dir = os.path.dirname(os.path.abspath(including_path))
    for i, step in enumerate(raw_steps):
        if not isinstance(step, dict) or "include" not in step:
            out.append((step, depth))
            continue
        where = f"include step {i} in {including_path}"
        extra = [k for k in step if k not in _INCLUDE_STEP_KEYS]
        if extra:
            raise SpecError(
                f"{where}: include takes only include|with|name, got {extra} "
                f"(include cannot be combined with an action op or expect)")
        inc = step.get("include")
        if not isinstance(inc, str) or not inc:
            raise SpecError(
                f"{where}: `include` must be a non-empty string, got {inc!r}")
        with_map = step.get("with") or {}
        if not isinstance(with_map, dict):
            raise SpecError(
                f"{where}: `with` must be a mapping, got "
                f"{type(with_map).__name__}")
        bad_wk = [k for k in with_map if not isinstance(k, str)]
        if bad_wk:
            raise SpecError(
                f"{where}: `with` keys must be strings, got {bad_wk}")
        bad_wv = [(k, v) for k, v in with_map.items()
                  if not isinstance(v, _WITH_SCALARS)]
        if bad_wv:
            raise SpecError(
                f"{where}: `with` values must be scalars (str/int/float/bool), "
                f"got {bad_wv}")
        # Resolve relative to the INCLUDING file's directory (same
        # convention as screenshot baselines and service cwds).
        frag_path = os.path.normpath(os.path.join(base_dir, inc))
        frag_abs = os.path.abspath(frag_path)
        if frag_abs in stack:
            cycle = " -> ".join(os.path.relpath(p) for p in
                                list(stack) + [frag_abs])
            raise SpecError(f"include cycle detected: {cycle}")
        if not os.path.isfile(frag_abs):
            raise SpecError(
                f"{where}: fragment file not found: {frag_abs}")
        with open(frag_abs, encoding="utf-8") as f:
            frag = yaml.safe_load(f)
        fwhere = f"fragment {frag_abs}"
        if not isinstance(frag, dict):
            raise SpecError(f"{fwhere}: fragment must be a YAML mapping")
        fextra = [k for k in frag if k not in _FRAGMENT_KEYS]
        if fextra:
            raise SpecError(
                f"{fwhere}: fragment allows only name|description|steps, "
                f"got {fextra} (setup/teardown/services/target are not "
                f"supported in fragments)")
        fsteps = frag.get("steps")
        if not isinstance(fsteps, list):
            raise SpecError(
                f"{fwhere}: fragment `steps` must be a list of steps")
        resolved = _resolve_includes(fsteps, frag_abs, stack + (frag_abs,),
                                     depth + 1)
        str_map = {k: str(v) for k, v in with_map.items()}
        for s, d in resolved:
            out.append((_substitute_with(s, str_map, where)
                        if d == depth + 1 else s, d))
    return out


def _flatten_includes(resolved: list) -> list:
    """Drop the depth tags from _resolve_includes output."""
    if not isinstance(resolved, list):
        return resolved
    return [s for s, _ in resolved]


# Issue #87: flow-variable names (also the runtime ${name} reference
# shape). Matches envsubst's \w for names starting with a letter.
_STORE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_VAR_REF = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


def _is_matrix_scalar(value: Any) -> bool:
    """Values a matrix combination may carry: JSON scalars (incl. bool)."""
    return value is None or isinstance(value, (str, int, float, bool))


def expand_matrix(matrix: Any) -> list[dict]:
    """Normalize a `matrix:` block to a list of combination variable maps.

    Two forms (issue #88):
    - list of maps: each map is one combination, values are scalars
      stringified via str() (same rule as #87 `store` captures);
    - a single map of name -> non-empty list of scalars: expanded as the
      cross product in key insertion order (deterministic).

    Raises SpecError on: non-list/non-map, empty, values that aren't
    scalar lists/maps, empty value lists, and names outside
    [A-Za-z_][A-Za-z0-9_]*. Name collisions with `store` steps are
    checked by _validate_matrix, which knows the declared store names.
    """
    where = "matrix"
    if not isinstance(matrix, (list, dict)) or not matrix:
        raise SpecError(
            f"{where}: must be a non-empty list of variable maps or a "
            f"non-empty map of variable name to value list, got {matrix!r}")
    if isinstance(matrix, dict):
        names = list(matrix.keys())
        for name in names:
            if not isinstance(name, str) or not _STORE_NAME.match(name):
                raise SpecError(
                    f"{where}: invalid variable name {name!r} "
                    f"(must match [A-Za-z_][A-Za-z0-9_]*)")
        value_lists = []
        for name in names:
            values = matrix[name]
            if not isinstance(values, list) or not values:
                raise SpecError(
                    f"{where}.{name}: must be a non-empty list of scalar "
                    f"values, got {values!r}")
            for v in values:
                if not _is_matrix_scalar(v):
                    raise SpecError(
                        f"{where}.{name}: values must be scalars "
                        f"(stringified via str()), got {v!r}")
            value_lists.append([str(v) for v in values])
        return [dict(zip(names, prod))
                for prod in _cartesian_product(*value_lists)]
    combos = []
    for i, entry in enumerate(matrix):
        if not isinstance(entry, dict) or not entry:
            raise SpecError(
                f"{where}[{i}]: each combination must be a non-empty "
                f"mapping of variable name to scalar value, got {entry!r}")
        combo = {}
        for name, value in entry.items():
            if not isinstance(name, str) or not _STORE_NAME.match(name):
                raise SpecError(
                    f"{where}[{i}]: invalid variable name {name!r} "
                    f"(must match [A-Za-z_][A-Za-z0-9_]*)")
            if not _is_matrix_scalar(value):
                raise SpecError(
                    f"{where}[{i}].{name}: values must be scalars "
                    f"(stringified via str()), got {value!r}")
            combo[name] = str(value)
        combos.append(combo)
    return combos


def _validate_matrix(raw_matrix: Any, where: str, store_names: set) -> list[dict]:
    """Validate `matrix:` and expand it; also reject names colliding with
    `store` steps (a name is seeded by the matrix OR captured — never
    both, or the runtime table would be ambiguous)."""
    if raw_matrix is None:
        return []
    combos = expand_matrix(raw_matrix)
    for combo in combos:
        clash = set(combo) & store_names
        if clash:
            raise SpecError(
                f"{where}: matrix variable(s) {sorted(clash)} also declared "
                f"by a `store` step — a variable is seeded by the matrix or "
                f"captured by `store`, not both")
    return combos


def _collect_store_names(raw_steps: Any) -> set:
    """Pre-pass over RAW (pre-envsubst) steps: declared store variable names.

    Runs before env substitution so ${name} runtime references survive
    load-time $ENV expansion untouched. Defensive: malformed store
    entries are ignored here — _parse_step raises the real error.
    """
    names = set()
    if not isinstance(raw_steps, list):
        return names
    for s in raw_steps:
        if isinstance(s, dict):
            store = s.get("store")
            if isinstance(store, dict):
                names.update(k for k in store if isinstance(k, str))
    return names


def _find_var_refs(value: Any) -> set:
    """Collect ${name}/$name references in a value (strings, dicts, lists)."""
    refs = set()
    if isinstance(value, str):
        for m in _VAR_REF.finditer(value):
            refs.add(m.group(1) or m.group(2))
    elif isinstance(value, dict):
        for v in value.values():
            refs |= _find_var_refs(v)
    elif isinstance(value, list):
        for v in value:
            refs |= _find_var_refs(v)
    return refs


def _check_variable_ordering(setup: list, steps: list, teardown: list,
                             declared: set, where: str,
                             matrix_names: set | None = None) -> None:
    """Load-time use-before-store check (issue #87).

    A ${name} reference in a step's params where no earlier step (in
    execution order: setup, steps, teardown) declares `store` for that
    name can never be captured at runtime, so it is a SpecError.
    References to names that are neither declared store variables nor
    environment variables were already rejected by envsubst in strict
    mode; in non-strict mode they are skipped here (the env is unknown).
    Issue #88: matrix variable names are exempt — the matrix seeds them
    in the run-scoped table before any step runs.
    """
    matrix_names = matrix_names or set()
    ordered = ([("setup", s) for s in setup] + [("steps", s) for s in steps]
               + [("teardown", s) for s in teardown])
    first_store: dict = {}  # name -> (global index, phase, step index)
    for i, (phase, s) in enumerate(ordered):
        for name in s.store:
            first_store.setdefault(name, (i, phase, s.index))
    for i, (phase, s) in enumerate(ordered):
        for ref in sorted(_find_var_refs(s.params)):
            if ref in matrix_names:
                continue  # seeded by the matrix before the run starts
            if ref not in declared:
                continue
            pos = first_store.get(ref)
            if pos is None or pos[0] >= i:
                # pos is None: declared somewhere the pre-pass saw but no
                # parsed step carries it (unreachable in practice); pos[0]
                # >= i covers same-step self-reference (store evaluates
                # after the step, so it cannot feed its own params).
                detail = (f"first stored by {pos[1]}[{pos[2]}]"
                          if pos is not None else "never stored")
                raise SpecError(
                    f"{where}: {phase}[{s.index}]: variable ${{{ref}}} is "
                    f"used before any step stores it ({detail}) — declare "
                    f"`store` for it in an earlier step")


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
    elif op == "type":
        # Issue #78: per-keystroke typing with an optional inter-keystroke
        # pause. text may be the empty string (clear-typing); delay_ms, when
        # given, must be a non-negative integer (milliseconds).
        if (not isinstance(params, dict)
                or not isinstance(params.get("target"), str)
                or not params["target"]
                or not isinstance(params.get("text"), str)):
            raise SpecError(f"{where}: type needs {{target, text}}")
        delay = params.get("delay_ms")
        if delay is not None and (not isinstance(delay, int) or delay < 0):
            raise SpecError(
                f"{where}: type delay_ms must be a non-negative integer")
    elif op == "focus":
        if (not isinstance(params, dict) or not isinstance(params.get("target"), str)
                or not params["target"]):
            raise SpecError(f"{where}: focus needs {{target}}")
    elif op == "hover":
        if (not isinstance(params, dict) or not isinstance(params.get("target"), str)
                or not params["target"]):
            raise SpecError(f"{where}: hover needs {{target}}")
    elif op == "upload":
        if (not isinstance(params, dict)
                or not isinstance(params.get("target"), str)
                or not params["target"]
                or not isinstance(params.get("path"), str)
                or not params["path"]):
            raise SpecError(f"{where}: upload needs {{target, path}}")
    elif op == "drag":
        if (not isinstance(params, dict)
                or not isinstance(params.get("target"), str)
                or not params["target"]):
            raise SpecError(f"{where}: drag needs {{target, to}}")
        to = params.get("to")
        if isinstance(to, str):
            if not to:
                raise SpecError(f"{where}: drag needs {{target, to}}")
        elif isinstance(to, dict):
            x, y = to.get("x"), to.get("y")
            if not isinstance(x, int) or not isinstance(y, int):
                raise SpecError(
                    f"{where}: drag 'to' coords need {{x: N, y: N}} integers")
        else:
            raise SpecError(
                f"{where}: drag 'to' must be a selector string or "
                f"{{x: N, y: N}}")
    elif op == "scroll":
        if not isinstance(params, dict):
            raise SpecError(f"{where}: scroll needs a mapping")
        if not any(k in params for k in ("target", "direction", "pixels")):
            raise SpecError(
                f"{where}: scroll needs at least one of target, direction, pixels")
        target = params.get("target")
        if target is not None and (not isinstance(target, str) or not target):
            raise SpecError(f"{where}: scroll target must be a non-empty string")
        direction = params.get("direction")
        if direction is not None and direction not in {"up", "down", "left", "right"}:
            raise SpecError(
                f"{where}: scroll direction must be up|down|left|right")
        pixels = params.get("pixels")
        if pixels is not None and (not isinstance(pixels, int) or pixels < 0):
            raise SpecError(f"{where}: scroll pixels must be a non-negative integer")
    elif op == "select":
        if not isinstance(params, dict) or "target" not in params or "value" not in params:
            raise SpecError(f"{where}: select needs {{target, value}}")
    elif op == "wait":
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise SpecError(f"{where}: wait needs a mapping")
        # Four disjoint forms (Playwright's wait_for_selector does not accept
        # load states): target-ful waits watch a selector; targetless waits
        # watch a page load state the runner already implements via
        # wait_for_load_state; js waits poll a JS expression until truthy
        # (issue #79); response waits watch for a network response whose URL
        # contains the substring (issue #79). The js/response forms take no
        # target/state/text, and js and response never combine.
        js = params.get("js")
        response = params.get("response")
        if js is not None and response is not None:
            raise SpecError(f"{where}: wait: js and response are mutually exclusive")
        if js is not None:
            if not isinstance(js, str) or not js:
                raise SpecError(f"{where}: wait.js must be a non-empty string")
            for k in ("target", "state", "text"):
                if params.get(k) is not None:
                    raise SpecError(f"{where}: wait.js cannot be combined with {k}")
        elif response is not None:
            if not isinstance(response, str) or not response:
                raise SpecError(
                    f"{where}: wait.response must be a non-empty string (URL substring)")
            for k in ("target", "state", "text"):
                if params.get(k) is not None:
                    raise SpecError(f"{where}: wait.response cannot be combined with {k}")
        elif params.get("target"):
            state = params.get("state", "visible")
            if state not in {"visible", "hidden", "attached", "detached"}:
                raise SpecError(f"{where}: wait with target: state must be visible|hidden|attached|detached")
        else:
            state = params.get("state", "load")
            if state not in {"load", "domcontentloaded", "networkidle"}:
                raise SpecError(f"{where}: wait without target: state must be load|domcontentloaded|networkidle")
        # timeout_ms applies to all four forms.
        timeout_ms = params.get("timeout_ms")
        # bools are ints in Python; timeout_ms: true is always a typo
        if (timeout_ms is not None
                and (isinstance(timeout_ms, bool)
                     or not isinstance(timeout_ms, int)
                     or timeout_ms < 0)):
            raise SpecError(f"{where}: wait.timeout_ms must be a non-negative int")
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
        if "delay_ms" in params:
            d = params["delay_ms"]
            # bools are ints in Python; delay_ms: true is always a typo
            if isinstance(d, bool) or not isinstance(d, int) or d < 0:
                raise SpecError(f"{where}: mock.delay_ms must be an int >= 0")


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
        elif key == "mock_calls":
            if (not isinstance(val, dict) or not isinstance(val.get("url"), str)
                    or not val["url"]
                    or not any(k in val for k in ("equals", "gte", "lte"))):
                raise SpecError(
                    f"{where}: assertion mock_calls needs {{url, equals|gte|lte}}")
        elif key == "network_calls":
            if (not isinstance(val, dict) or not isinstance(val.get("url"), str)
                    or not val["url"]
                    or not any(k in val for k in ("equals", "gte", "lte"))):
                raise SpecError(
                    f"{where}: assertion network_calls needs {{url, equals|gte|lte}}")
            if "status" in val:
                st = val["status"]
                if (not isinstance(st, dict) or len(st) != 1
                        or not any(k in st for k in ("equals", "gte", "lte"))):
                    raise SpecError(
                        f"{where}: network_calls status must be "
                        f"{{equals|gte|lte: int}}")
                sv = next(iter(st.values()))
                # bools are ints in Python; status: true is always a typo
                if isinstance(sv, bool) or not isinstance(sv, int) or sv < 0:
                    raise SpecError(
                        f"{where}: network_calls status must be "
                        f"a non-negative int, got {sv!r}")
        elif key == "count":
            if (not isinstance(val, dict) or "selector" not in val
                    or not any(k in val for k in ("equals", "gte", "lte"))):
                raise SpecError(
                    f"{where}: assertion count needs {{selector, equals|gte|lte}}")
        elif key == "attribute":
            # Issue #81: element attribute value. Exactly one of
            # equals|contains (same exactly-one guard as mock_calls'
            # equals|gte|lte); a missing attribute never matches at runtime.
            if (not isinstance(val, dict)
                    or not isinstance(val.get("selector"), str)
                    or not val["selector"]
                    or not isinstance(val.get("name"), str)
                    or not val["name"]):
                raise SpecError(
                    f"{where}: assertion attribute needs "
                    f"{{selector, name, equals|contains}}")
            if (sum(k in val for k in ("equals", "contains")) != 1
                    or not isinstance(val.get("equals",
                                              val.get("contains")), str)):
                raise SpecError(
                    f"{where}: assertion attribute needs exactly one of "
                    f"equals|contains, as a string")
        elif key == "value":
            # Issue #81: input value (after fill/type).
            if (not isinstance(val, dict)
                    or not isinstance(val.get("selector"), str)
                    or not val["selector"]
                    or not isinstance(val.get("equals"), str)):
                raise SpecError(
                    f"{where}: assertion value needs "
                    f"{{selector, equals}} (equals must be a string)")
        elif key == "focused":
            # Issue #81: document.activeElement === the element.
            if (not isinstance(val, dict)
                    or not isinstance(val.get("selector"), str)
                    or not val["selector"]):
                raise SpecError(
                    f"{where}: assertion focused needs {{selector}}")
        elif key == "cookie":
            # Issue #82: cookie value. At most one of equals|contains (never
            # both); neither means a presence check (the cookie is set). A
            # missing cookie never matches at runtime.
            if (not isinstance(val, dict)
                    or not isinstance(val.get("name"), str)
                    or not val["name"]):
                raise SpecError(
                    f"{where}: assertion cookie needs {{name, equals?|contains?}}")
            if (sum(k in val for k in ("equals", "contains")) > 1
                    or any(k in val and not isinstance(val[k], str)
                           for k in ("equals", "contains"))):
                raise SpecError(
                    f"{where}: assertion cookie takes at most one of "
                    f"equals|contains, as a string")
        elif key == "storage":
            # Issue #82: web storage value. kind is local|session; key is a
            # non-empty string; equals (optional, a string) narrows to a
            # value check, otherwise presence of the key. A missing key
            # (getItem -> null) never matches at runtime.
            if (not isinstance(val, dict)
                    or val.get("kind") not in ("local", "session")
                    or not isinstance(val.get("key"), str)
                    or not val["key"]):
                raise SpecError(
                    f"{where}: assertion storage needs "
                    f"{{kind: local|session, key, equals?}}")
            if "equals" in val and not isinstance(val["equals"], str):
                raise SpecError(
                    f"{where}: assertion storage equals must be a string")
        elif key == "perf":
            # Issue #84: Web Vitals performance budgets. A non-empty
            # mapping of metric name to exactly {lte: budget}; budgets are
            # upper bounds, so only the lte comparator is allowed (same
            # exactly-one guard as attribute/cookie). The budget is a
            # non-negative number (same guard as network_calls status).
            if not isinstance(val, dict) or not val:
                raise SpecError(
                    f"{where}: assertion perf needs a non-empty mapping "
                    f"of metric -> {{lte: budget}}")
            for metric, budget in val.items():
                if metric not in PERF_METRICS:
                    raise SpecError(
                        f"{where}: assertion perf has unknown metric "
                        f"{metric!r} (known: {sorted(PERF_METRICS)})")
                if (not isinstance(budget, dict) or len(budget) != 1
                        or "lte" not in budget):
                    raise SpecError(
                        f"{where}: assertion perf metric {metric!r} needs "
                        f"exactly {{lte: budget}}")
                n = budget["lte"]
                # bools are numbers in Python; a bool budget is always a typo
                if isinstance(n, bool) or not isinstance(n, (int, float)) \
                        or n < 0:
                    raise SpecError(
                        f"{where}: assertion perf metric {metric!r} budget "
                        f"must be a non-negative number, got {n!r}")
        elif key == "a11y":
            # Issue #85: axe-core accessibility violation audit. A
            # non-empty mapping with required violations: {lte: budget}
            # (only lte: budgets are upper bounds, like perf) and an
            # optional rules: [axe rule ids] scope. The budget is a
            # non-negative number (same guard as perf budgets).
            if not isinstance(val, dict) or not val:
                raise SpecError(
                    f"{where}: assertion a11y needs a non-empty mapping "
                    f"with violations: {{lte: budget}} "
                    f"(optional rules: [...])")
            unknown_a11y = [k for k in val
                            if k not in {"violations", "rules"}]
            if unknown_a11y:
                raise SpecError(
                    f"{where}: assertion a11y takes only "
                    f"violations|rules, got {unknown_a11y}")
            budget = val.get("violations")
            if (not isinstance(budget, dict) or len(budget) != 1
                    or "lte" not in budget):
                raise SpecError(
                    f"{where}: assertion a11y needs "
                    f"violations: {{lte: budget}}")
            n = budget["lte"]
            # bools are numbers in Python; a bool budget is always a typo
            if isinstance(n, bool) or not isinstance(n, (int, float)) \
                    or n < 0:
                raise SpecError(
                    f"{where}: assertion a11y violations budget must be "
                    f"a non-negative number, got {n!r}")
            if "rules" in val:
                rules = val["rules"]
                if (not isinstance(rules, list) or not rules
                        or not all(isinstance(r, str) and r
                                   for r in rules)):
                    raise SpecError(
                        f"{where}: assertion a11y rules must be a "
                        f"non-empty list of axe rule id strings")
        if key == "request_body":
            # Issue #83: opt-in request-body assertion. Exactly one of
            # contains|equals (same exactly-one guard as attribute's
            # equals|contains); matched against bodies recorded when the
            # flow opts into `record_bodies: true`.
            if (not isinstance(val, dict)
                    or not isinstance(val.get("url"), str)
                    or not val["url"]
                    or sum(k in val for k in ("equals", "contains")) != 1
                    or not isinstance(val.get("equals",
                                              val.get("contains")), str)):
                raise SpecError(
                    f"{where}: assertion request_body needs "
                    f"{{url, exactly one of equals|contains (a string)}}")
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
        elif key == "dialog":
            if not isinstance(val, dict) or not val:
                raise SpecError(
                    f"{where}: assertion dialog needs a mapping like "
                    f"{{type: confirm, text: 'Are you sure?'}}")
            unknown_dk = [k for k in val if k not in {"type", "text"}]
            if unknown_dk:
                raise SpecError(
                    f"{where}: dialog assertion takes only type|text, "
                    f"got {unknown_dk}")
            if "type" in val and val["type"] not in DIALOG_TYPES:
                raise SpecError(
                    f"{where}: dialog type must be "
                    f"{'|'.join(sorted(DIALOG_TYPES))}, got {val['type']!r}")
            if "text" in val and (not isinstance(val["text"], str)
                                  or not val["text"]):
                raise SpecError(f"{where}: dialog text must be a non-empty string")
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
    retry_delay_ms = raw.get("retry_delay_ms", 0)
    if (isinstance(retry_delay_ms, bool)
            or not isinstance(retry_delay_ms, int) or retry_delay_ms < 0):
        raise SpecError(
            f"{where}: retry_delay_ms must be an integer >= 0, "
            f"got {retry_delay_ms!r}")
    # Issue #95: a delay with no retries (or outside the steps phase) would
    # be silently inert — fail fast, mirroring the retry > 0 phase rule.
    if retry_delay_ms > 0 and (phase != "steps" or retry == 0):
        raise SpecError(
            f"{where}: retry_delay_ms > 0 requires retry > 0 in the steps "
            f"phase (got retry={retry}, phase '{phase}'; a delay with no "
            "retries to separate is inert)")
    on_dialog = raw.get("on_dialog", "dismiss")
    if on_dialog not in DIALOG_POLICIES:
        raise SpecError(
            f"{where}: on_dialog must be accept|dismiss, got {on_dialog!r}")
    # Issue #87: step-level variable capture. Only the js source is
    # supported in this slice; text/attribute sources are explicitly
    # later (the issue), so anything else fails closed.
    store = raw.get("store") or {}
    if not isinstance(store, dict):
        raise SpecError(
            f"{where}: store must be a mapping of variable name -> source, "
            f"got {type(store).__name__}")
    for vname, src in store.items():
        if not isinstance(vname, str) or not _STORE_NAME.match(vname):
            raise SpecError(
                f"{where}: store variable names must match "
                f"[A-Za-z_][A-Za-z0-9_]*, got {vname!r}")
        if not isinstance(src, dict):
            raise SpecError(
                f"{where}: store '{vname}' source must be a mapping, got "
                f"{type(src).__name__}")
        if set(src) != {"js"}:
            raise SpecError(
                f"{where}: store '{vname}' supports only the js source "
                f"(text/attribute capture is not yet supported), got "
                f"{sorted(src)}")
        js = src["js"]
        if not isinstance(js, str) or not js:
            raise SpecError(
                f"{where}: store '{vname}' js must be a non-empty string")
    frame = raw.get("frame")
    if frame is not None:
        # Issue #80: frame must be a non-empty string (frame name or URL
        # substring). A frame can't navigate or register mocks, so frame:
        # combined with the page-level ops fails fast at spec time.
        if not isinstance(frame, str) or not frame:
            raise SpecError(
                f"{where}: frame must be a non-empty string (frame name "
                f"or URL substring), got {frame!r}")
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
    if frame is not None and op in {"goto", "seed", "mock", "script"}:
        raise SpecError(
            f"{where}: frame: cannot be combined with {op} "
            f"(page-level op — navigates, seeds, mocks or evaluates "
            f"on the main page only)")
    expect_items = _validate_expect(expect, where)
    return Step(
        index=index, phase=phase,
        name=name or f"{op or 'expect'} #{index}",
        op=op, params=params, expect=expect, expect_items=expect_items,
        continue_on_fail=continue_on_fail, timeout_ms=timeout_ms,
        retry=retry, retry_delay_ms=retry_delay_ms, on_dialog=on_dialog,
        frame=frame, store=store, raw=raw,
    )


def _parse_steps(raw: Any, phase: str, default_timeout_ms: int,
               strict_env: bool = True,
               skip_names: set | None = None) -> list[Step]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SpecError(f"{phase}: must be a list of steps")
    return [_parse_step(envsubst(s, f"{phase}[{i}]", strict_env, skip_names),
                        i, phase, default_timeout_ms)
            for i, s in enumerate(raw)]


def load_spec(path: str, strict_env: bool = True) -> FlowSpec:
    """Load and validate a flow-spec YAML file. Raises SpecError on problems."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    try:
        raw = yaml.safe_load(text)
    except Exception as e:
        # Issue #89: unparsable YAML is an invalid spec, not a crash —
        # the batch path records it as an INVALID-SPEC failure.
        raise SpecError(f"spec {path}: cannot parse YAML ({e})")
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
    # Issue #88: `target:` is parsed after the matrix is validated (see
    # below), so matrix ${name} refs in the target survive load-time
    # $ENV expansion untouched.
    target_raw = raw.get("target", "")
    viewport = raw.get("viewport") or {"width": 1280, "height": 800}
    artifacts = {"screenshot": "per-step", "ax_snapshot": "on-failure", "trace": True}
    artifacts.update(raw.get("artifacts") or {})
    if artifacts.get("screenshot") not in {"per-step", "on-failure", "none"}:
        raise SpecError(f"{where}: artifacts.screenshot must be per-step|on-failure|none")
    # Issue #83: opt-in request-body recording. Must be a real bool;
    # `record_bodies: "yes"` is a typo, not consent, so it is rejected.
    record_bodies = raw.get("record_bodies", False)
    if not isinstance(record_bodies, bool):
        raise SpecError(
            f"{where}: record_bodies must be a bool, got {record_bodies!r}")
    # Issue #90: opt-in video recording. Must be a real bool (there is no
    # unknown-top-level-key guard, so validate explicitly).
    video = raw.get("video", False)
    if not isinstance(video, bool):
        raise SpecError(f"{where}: video must be a bool, got {video!r}")
    # Issue #89: flow tags for batch selection (validated explicitly —
    # there is no unknown-top-level-key guard).
    tags = _validate_tags(raw.get("tags"), where)
    # Issue #86: expand include steps on the RAW step lists, before
    # _parse_steps — so include/with entries never reach _parse_step's
    # unknown-keys guard, and any ${var} the with: map did not substitute
    # is still envsubst'd (strict) like today.
    abs_path = os.path.abspath(path)
    setup_raw = _flatten_includes(
        _resolve_includes(raw.get("setup"), path, (abs_path,)))
    steps_raw = _flatten_includes(
        _resolve_includes(raw.get("steps"), path, (abs_path,)))
    teardown_raw = _flatten_includes(
        _resolve_includes(raw.get("teardown"), path, (abs_path,)))
    # Issue #87: collect declared store variable names across all phases
    # BEFORE env substitution, so ${name} runtime references in step
    # params survive load-time $ENV expansion untouched; a stored name
    # is also exempt when it shadows an environment variable (the
    # runtime table wins).
    store_names = (_collect_store_names(setup_raw)
                   | _collect_store_names(steps_raw)
                   | _collect_store_names(teardown_raw))
    # Issue #88: expand and validate `matrix:`; matrix names join the
    # declared set (skip_names) so ${name} refs survive load-time $ENV
    # expansion in step params AND in target:.
    matrix = _validate_matrix(raw.get("matrix"), where, store_names)
    matrix_names = {name for combo in matrix for name in combo}
    declared_names = store_names | matrix_names
    target = envsubst(target_raw, where, strict_env, matrix_names)
    if not target or not isinstance(target, str):
        raise SpecError(f"{where}: `target` is required (base URL, $VAR allowed)")
    setup = _parse_steps(setup_raw, "setup", default_step_ms, strict_env,
                         declared_names)
    steps = _parse_steps(steps_raw, "steps", default_step_ms, strict_env,
                         declared_names)
    teardown = _parse_steps(teardown_raw, "teardown", default_step_ms,
                            strict_env, declared_names)
    _check_variable_ordering(setup, steps, teardown, declared_names, where,
                             matrix_names)
    return FlowSpec(
        name=name,
        description=str(raw.get("description") or ""),
        target=target,
        viewport={"width": int(viewport.get("width", 1280)),
                  "height": int(viewport.get("height", 800))},
        setup=setup,
        steps=steps,
        teardown=teardown,
        artifacts=artifacts,
        timeouts={k: int(v) for k, v in timeouts.items()},
        investigate_on_failure=bool(raw.get("investigate_on_failure", True)),
        max_investigation_actions=int(raw.get("max_investigation_actions", 20)),
        source_path=path,
        services=_validate_services(raw.get("services"), where),
        record_bodies=record_bodies,
        video=video,
        matrix=matrix,
        tags=tags,
    )
