"""Semantic evaluation: does the change actually make sense?

`qaloop verify` tells you the flow passed. `evaluate` tells you whether the
change *behind* the flow makes sense: given the claimed behavior, the code
diff, and the flow's evidence, is the behavior semantically right — or did
the assertions just happen to pass?

    python3 -m qaloop.cli evaluate \\
        --claim "The profile editor now saves the display name" \\
        --run runs/<run-id> \\
        --diff <git-diff-file-or-range>

Writes EVALUATION.md + evaluation.json into the run dir and appends a
cost ledger entry. Uses the same OpenAI-compatible model config as
investigator/perform (QALOOP_MODEL_* env vars).

Verdicts:
    MAKES_SENSE          — evidence supports the claim; behavior is right
    DOES_NOT_MAKE_SENSE  — evidence contradicts the claim or shows the
                           behavior is wrong despite passing assertions
    INSUFFICIENT_EVIDENCE — the run doesn't prove the claim either way
    BLOCKED              — the run never produced usable evidence
                           (flow failed, service failed to boot, etc.)

Confidence is coupled to the verdict before anything is written: low
confidence (or anything unparseable, which defaults to low) downgrades
MAKES_SENSE to INSUFFICIENT_EVIDENCE, and high-confidence MAKES_SENSE needs
at least 2 non-empty evidence bullets. The raw confidence is kept as
confidence_raw in evaluation.json for audit.

Before calibration, the verdict is derived from rubric scores: the judge
scores four fixed dimensions (claim_diff_fit, evidence_exercises_claim,
no_contradictions, state_supports_claim) 0/1/2, and _apply_rubric turns the
scores into the verdict via fatal rules and a sum threshold — the model's
raw verdict is advisory (except BLOCKED, which is kept as-is). The raw
verdict is kept as verdict_raw in evaluation.json for audit.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request

VERDICTS = ("MAKES_SENSE", "DOES_NOT_MAKE_SENSE",
            "INSUFFICIENT_EVIDENCE", "BLOCKED")

# Bounds keep the model call small and the cost predictable.
_MAX_DIFF = 12000
_MAX_AX = 6000
_MAX_REPORT = 4000
_MAX_TEXT = 1500

# Rubric dimensions the judge scores 0/1/2. Fixed order: 0 = fails,
# 1 = partial/unclear, 2 = solid. The scores drive the final verdict via
# _apply_rubric; the model's verdict field is advisory (except BLOCKED).
RUBRIC = {
    "claim_diff_fit": "the diff plausibly implements the claim",
    "evidence_exercises_claim": "the flow evidence actually exercises the"
                                " claimed behavior (not a vacuous pass)",
    "no_contradictions": "nothing in the evidence contradicts the claim",
    "state_supports_claim": "the final browser state, logs, and service"
                             " output support the claim",
}


def _model_config() -> dict:
    return {
        "base_url": os.environ.get("QALOOP_MODEL_BASE_URL",
                                   "https://api.openai.com/v1").rstrip("/"),
        "name": os.environ.get("QALOOP_MODEL_NAME", "gpt-4o-mini"),
        "api_key": os.environ.get("QALOOP_MODEL_API_KEY", ""),
        "price_in": float(os.environ.get("QALOOP_MODEL_PRICE_IN", "0.15")),
        "price_out": float(os.environ.get("QALOOP_MODEL_PRICE_OUT", "0.60")),
    }


def _chat(cfg: dict, messages: list[dict], timeout_s: int = 90) -> tuple[str, dict]:
    body = json.dumps({
        "model": cfg["name"], "messages": messages,
        "temperature": 0.2, "max_tokens": 1200,
    }).encode()
    req = urllib.request.Request(
        cfg["base_url"] + "/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg['api_key']}"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        data = json.load(resp)
    choice = data["choices"][0]["message"]
    return choice.get("content") or "", data.get("usage", {})


def _read(path: str, limit: int) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return ""
    if len(text) > limit:
        text = text[:limit] + f"\n…[truncated {len(text) - limit} chars]"
    return text


def _diff_stat(diff: str) -> str:
    files = set(re.findall(r"^diff --git a/(.*?) b/", diff, re.M))
    added = len(re.findall(r"^\+(?!\+)", diff, re.M))
    removed = len(re.findall(r"^-(?!-)", diff, re.M))
    return f"{len(files)} files, +{added}/-{removed}"


def collect_evidence(run_dir: str, diff: str) -> dict:
    """Assemble everything the judge needs from a finished run dir."""
    run = {}
    run_path = os.path.join(run_dir, "run.json")
    try:
        with open(run_path, encoding="utf-8") as f:
            run = json.load(f)
    except OSError:
        pass

    steps = []
    for s in run.get("steps", []):
        ax_text = ""
        ax_ref = s.get("ax_snapshot")
        if ax_ref:
            ax_path = os.path.join(run_dir, "steps", ax_ref) \
                if not os.path.isabs(ax_ref) else ax_ref
            if not os.path.exists(ax_path):
                ax_path = os.path.join(run_dir, ax_ref)
            ax_text = _read(ax_path, _MAX_AX) if os.path.exists(ax_path) else ""
        steps.append({
            "index": s.get("index"), "phase": s.get("phase"),
            "name": s.get("name"), "op": s.get("op"),
            "status": s.get("status"), "duration_ms": s.get("duration_ms"),
            "error": (s.get("error") or "")[:500],
            "assertions": s.get("assertions") or [],
            "ax_snapshot": ax_text,
        })

    services: dict[str, str] = {}
    svc_dir = os.path.join(run_dir, "services")
    if os.path.isdir(svc_dir):
        for name in sorted(os.listdir(svc_dir)):
            if name.endswith(".log"):
                services[name[:-4]] = _read(os.path.join(svc_dir, name), 800)

    return {
        "flow_name": run.get("flow_name", ""),
        "target": run.get("target", ""),
        "status": run.get("status", ""),
        "failed_step": run.get("failed_step"),
        "error": (run.get("error") or "")[:_MAX_TEXT],
        "steps": steps,
        "console_errors": (run.get("console_errors") or [])[:10],
        "page_errors": [str(e)[:300] for e in (run.get("page_errors") or [])[:10]],
        "failed_requests": (run.get("failed_requests") or [])[:10],
        "bad_responses": (run.get("bad_responses") or [])[:10],
        "services": services,
        "report": _read(os.path.join(run_dir, "REPORT.md"), _MAX_REPORT),
        "diff_stat": _diff_stat(diff),
        "diff": diff[:_MAX_DIFF] + (
            f"\n…[truncated {len(diff) - _MAX_DIFF} chars]"
            if len(diff) > _MAX_DIFF else ""),
    }


_SYSTEM = """You are a semantic QA judge. A developer claims a code change does
something; a qaloop browser flow then ran against real services. Your job is
NOT to check whether assertions passed — it is to judge whether the claimed
behavior actually makes sense given the diff and the observed evidence.

Think like a skeptical senior engineer:
- Does the diff plausibly implement the claim, or does it only look related?
- Does the flow evidence actually exercise the claimed behavior, or does it
  pass vacuously (wrong page, mocked data hiding the real path, assertions
  that can't fail)?
- Do the final browser state (accessibility tree), console/network errors,
  and service logs support or contradict the claim?

Respond with a single JSON object, no prose outside it:
{
  "verdict": "MAKES_SENSE" | "DOES_NOT_MAKE_SENSE" |
             "INSUFFICIENT_EVIDENCE" | "BLOCKED",
  "confidence": "high" | "medium" | "low",
  "rationale": "2-4 sentences: what the evidence shows and why the verdict follows",
  "evidence": ["short bullet quotes: file:line or step name -> what it shows"],
  "risks": ["what could still be wrong or uncovered"],
  "scores": {"claim_diff_fit": 0|1|2, "evidence_exercises_claim": 0|1|2,
             "no_contradictions": 0|1|2, "state_supports_claim": 0|1|2}
}

Rubric — score each dimension 0/1/2 (0 = fails, 1 = partial/unclear,
2 = solid):
- claim_diff_fit: does the diff plausibly implement the claim?
- evidence_exercises_claim: does the flow evidence actually exercise the
  claimed behavior, or does it pass vacuously (wrong page, mocked data
  hiding the real path, assertions that can't fail)?
- no_contradictions: does anything in the evidence contradict the claim
  (error toasts, failed requests, wrong final state, log errors)?
- state_supports_claim: do the final browser state (accessibility tree),
  console/network output, and service logs support the claim?

Score honestly: a failing dimension scores 0 even when the verdict field
above says otherwise — the scores, not the verdict, drive the final
verdict.

Verdict guidance:
- MAKES_SENSE: the diff implements the claim AND the flow evidence shows the
  behavior working (right page, right data, no contradicting errors).
- DOES_NOT_MAKE_SENSE: the diff contradicts the claim, or the evidence shows
  wrong behavior despite passing assertions (e.g. asserted the wrong element,
  error toast visible, request failed).
- INSUFFICIENT_EVIDENCE: the run passed but doesn't really test the claim
  (too shallow, wrong surface, key step skipped).
- BLOCKED: the run never produced usable evidence (flow failed, service
  failed to boot, page never loaded)."""


def _user_message(claim: str, ev: dict) -> str:
    parts = [f"CLAIM: {claim}\n"]
    parts.append(f"DIFF ({ev['diff_stat']}):\n{ev['diff']}\n")
    parts.append(
        f"FLOW: {ev['flow_name']} target={ev['target']} status={ev['status']} "
        f"failed_step={ev['failed_step']} error={ev['error'] or 'none'}")
    for s in ev["steps"]:
        parts.append(
            f"\nSTEP {s['index']} [{s['phase']}] {s['name']} ({s['op']}): "
            f"{s['status']}"
            + (f" error={s['error']}" if s["error"] else "")
            + (f" assertions={json.dumps(s['assertions'])}"
               if s["assertions"] else ""))
        if s["ax_snapshot"]:
            parts.append(f"  AX:\n{s['ax_snapshot']}")
    if ev["console_errors"]:
        parts.append(f"\nCONSOLE ERRORS: {json.dumps(ev['console_errors'])}")
    if ev["page_errors"]:
        parts.append(f"\nPAGE ERRORS: {ev['page_errors']}")
    if ev["failed_requests"]:
        parts.append(f"\nFAILED REQUESTS: {json.dumps(ev['failed_requests'])}")
    if ev["bad_responses"]:
        parts.append(f"\nBAD RESPONSES: {json.dumps(ev['bad_responses'])}")
    for name, log in ev["services"].items():
        parts.append(f"\nSERVICE {name} LOG (tail):\n{log}")
    if ev["report"]:
        parts.append(f"\nRUN REPORT:\n{ev['report']}")
    return "\n".join(parts)


def _normalize_confidence(raw) -> str:
    """Accept only high|medium|low (case-insensitive); anything else -> low."""
    text = str(raw or "").strip().lower()
    return text if text in ("high", "medium", "low") else "low"


def _calibrate_verdict(verdict: dict) -> dict:
    """Couple confidence to the verdict before it ships.

    A low-confidence MAKES_SENSE is not credible, and neither is a
    high-confidence MAKES_SENSE that cites no evidence; both downgrade to
    INSUFFICIENT_EVIDENCE with the rationale appended. DOES_NOT_MAKE_SENSE
    keeps its verdict at any confidence (a contradiction is a contradiction)
    — low confidence there is surfaced prominently in EVALUATION.md instead.
    INSUFFICIENT_EVIDENCE and BLOCKED are never reclassified.
    """
    if verdict.get("verdict") != "MAKES_SENSE":
        return verdict
    conf = verdict.get("confidence", "low")
    bullets = [e for e in verdict.get("evidence", [])
               if isinstance(e, str) and e.strip()]
    reason = None
    if conf == "low":
        reason = (" Downgrade: the judge returned MAKES_SENSE with low"
                  " confidence, so the evidence does not credibly support"
                  " the claim.")
    elif conf == "high" and len(bullets) < 2:
        reason = (" Downgrade: the judge returned MAKES_SENSE with high"
                  " confidence but cited fewer than 2 non-empty evidence"
                  " bullets, so the confident verdict is not credible.")
    if reason is not None:
        verdict["verdict"] = "INSUFFICIENT_EVIDENCE"
        verdict["rationale"] = (verdict.get("rationale") or "") + reason
    return verdict


def _extract_verdict(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("model did not return a JSON verdict")
    obj = json.loads(text[start:end + 1])
    verdict = str(obj.get("verdict", "")).strip().upper()
    if verdict not in VERDICTS:
        raise ValueError(f"unknown verdict {verdict!r}")
    obj["verdict"] = verdict
    obj["verdict_raw"] = verdict  # set before any reclassification
    obj["confidence_raw"] = obj.get("confidence")
    obj["confidence"] = _normalize_confidence(obj.get("confidence"))
    raw_scores = obj.get("scores")
    provided = isinstance(raw_scores, dict)
    scores = {}
    for dim in RUBRIC:
        v = raw_scores.get(dim) if provided else None
        # bool is an int subclass: exclude it so true/false normalize to 0
        scores[dim] = v if isinstance(v, int) and not isinstance(v, bool) \
            and v in (0, 1, 2) else 0
    obj["scores"] = scores
    obj["_scores_provided"] = provided  # internal; stripped by _apply_rubric
    obj.setdefault("rationale", "")
    obj.setdefault("evidence", [])
    obj.setdefault("risks", [])
    return obj


def _apply_rubric(verdict: dict) -> dict:
    """Derive the final verdict from the rubric scores (runs before
    calibration, so calibration sees the rubric-derived verdict).

    Fatal rules: a 0 in claim_diff_fit (the diff doesn't implement the
    claim) or in no_contradictions (the evidence contradicts the claim)
    yields DOES_NOT_MAKE_SENSE; a 0 in evidence_exercises_claim or
    state_supports_claim yields INSUFFICIENT_EVIDENCE. Otherwise every
    dimension scored 1-2: a total of at least 6 of 8 yields MAKES_SENSE,
    anything less yields INSUFFICIENT_EVIDENCE.

    A raw BLOCKED verdict is kept as-is — the run produced no evidence to
    score. When the model supplied no scores object at all there is nothing
    to derive from, so the raw verdict stands (the all-zero normalized
    scores are still recorded for audit). The internal _scores_provided
    flag is stripped in every path so it never reaches evaluation.json.
    """
    out = dict(verdict)
    provided = out.pop("_scores_provided", False)
    raw = out.get("verdict_raw", out.get("verdict"))
    if raw == "BLOCKED" or not provided:
        return out
    scores = out.get("scores") or {}
    if scores.get("claim_diff_fit") == 0 \
            or scores.get("no_contradictions") == 0:
        out["verdict"] = "DOES_NOT_MAKE_SENSE"
    elif scores.get("evidence_exercises_claim") == 0 \
            or scores.get("state_supports_claim") == 0:
        out["verdict"] = "INSUFFICIENT_EVIDENCE"
    elif sum(scores.get(dim, 0) for dim in RUBRIC) >= 6:
        out["verdict"] = "MAKES_SENSE"
    else:
        out["verdict"] = "INSUFFICIENT_EVIDENCE"
    return out


def evaluate(claim: str, run_dir: str, diff: str,
             model_cfg: dict | None = None) -> dict:
    """Run the semantic judge. Returns the verdict dict + cost info."""
    cfg = model_cfg or _model_config()
    if not cfg["api_key"]:
        raise RuntimeError(
            "evaluate needs a model: set QALOOP_MODEL_API_KEY "
            "(and QALOOP_MODEL_BASE_URL / QALOOP_MODEL_NAME)")
    ev = collect_evidence(run_dir, diff)
    text, usage = _chat(cfg, [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": _user_message(claim, ev)},
    ])
    verdict = _extract_verdict(text)
    verdict = _apply_rubric(verdict)
    verdict = _calibrate_verdict(verdict)
    tin = int(usage.get("prompt_tokens") or 0)
    tout = int(usage.get("completion_tokens") or 0)
    cost = tin / 1e6 * cfg["price_in"] + tout / 1e6 * cfg["price_out"]
    verdict["_cost"] = {"tokens_in": tin, "tokens_out": tout,
                       "cost_usd_est": round(cost, 6),
                       "model": cfg["name"]}
    return verdict


def write_evaluation(run_dir: str, claim: str, verdict: dict) -> tuple[str, str]:
    """Write EVALUATION.md + evaluation.json into the run dir."""
    cost = verdict.get("_cost", {})
    body = verdict.copy()
    body.pop("_cost", None)
    json_path = os.path.join(run_dir, "evaluation.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"claim": claim, "verdict": body,
                   "cost": cost}, f, indent=2, ensure_ascii=False)

    md_path = os.path.join(run_dir, "EVALUATION.md")
    lines = ["# Semantic evaluation", "",
             f"**Claim:** {claim}", "",
             f"**Verdict:** `{verdict['verdict']}` "
             f"(confidence: {verdict.get('confidence', '?')})", ""]
    scores = verdict.get("scores")
    if scores:
        lines += ["| Dimension | Score |", "| --- | --- |"]
        for dim in RUBRIC:
            lines += [f"| {dim} | {scores.get(dim, 0)} |"]
        lines += [""]
    if (verdict.get("verdict") == "DOES_NOT_MAKE_SENSE"
            and verdict.get("confidence") == "low"):
        lines += ["> [!WARNING] **Low-confidence verdict:** the judge was"
                  " unsure; treat this result as provisional and re-verify"
                  " before acting on it.", ""]
    lines += ["## Rationale", "", verdict.get("rationale", ""), ""]
    if verdict.get("evidence"):
        lines += ["## Evidence", ""]
        lines += [f"- {e}" for e in verdict["evidence"]] + [""]
    if verdict.get("risks"):
        lines += ["## Risks / missing coverage", ""]
        lines += [f"- {r}" for r in verdict["risks"]] + [""]
    if cost:
        lines += [f"_Model: {cost.get('model')}, "
                  f"tokens {cost.get('tokens_in')}/{cost.get('tokens_out')}, "
                  f"est. ${cost.get('cost_usd_est'):.6f}_", ""]
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return md_path, json_path
