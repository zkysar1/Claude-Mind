#!/usr/bin/env python3
"""Consolidation health snapshot — writer for the consolidation_health WM slot.

Three consumers read this slot every iteration:
  .claude/skills/aspirations-evolve/SKILL.md:395 — gap-analysis gate
  .claude/skills/aspirations-select/SKILL.md:247 — near-complete/stalled bias
  .claude/skills/create-aspiration/SKILL.md:125  — consolidation-gate

Before this script, the slot was read but never written — consolidation
gates silently no-op'd (verify-learning signal-lifecycle-gate finding
2026-04-22). This script is the single writer, invoked from
aspirations-precheck Phase 0.5 every iteration.

Emitted schema:
  {
    "active_count":   int,   # aspirations with status=="active"
    "avg_completion": float, # mean completion_ratio across active (0.0 if none)
    "near_complete":  int,   # count where completion_ratio >= 0.75
    "stalled":        int,   # count where completion_ratio < 0.25
    "computed_at":    "ISO-local timestamp"
  }

Completion ratio per aspiration:
  ratio = completed_goals / total_goals (0.0 when total_goals == 0).
  Recurring goals are excluded from both counts by aspirations.py
  recompute_progress — they are perpetual and never "complete".

Input sources: both world and agent aspiration queues via aspirations.py.
Output: JSON to stdout (inspectable) AND piped into wm-set.sh
        consolidation_health when --write is passed.

Exit codes:
  0 — snapshot computed (and written if --write). Empty queues are valid.
  1 — infrastructure error (wm-set failure when --write is set).

Fail-open boundary: MIND_AGENT being unset is NOT handled here — the only
caller is aspirations-precheck Phase 0.5, which runs under a bound agent.
The shell wrapper line in that phase has `|| echo WARN` as the single
fail-open boundary (rb-347: one boundary per path). Duplicating it here
would hide upstream agent-binding bugs.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import _rt  # canonical Python -> daemon client (post-cutover; see _rt.py)

# Default thresholds — overridden by core/config/aspirations.yaml
# `consolidation_health.near_complete_threshold` and `.stalled_threshold` via
# _load_thresholds() below. Kept as module constants so callers that import
# this module without invoking _load_thresholds() still see sensible defaults.
# : centralized in config to match intent_satisfaction / productivity_gate
# pattern and eliminate drift risk across three consumer sites.
NEAR_COMPLETE_THRESHOLD = 0.75
STALLED_THRESHOLD = 0.25


def _load_thresholds() -> tuple[float, float]:
    """Read near_complete + stalled thresholds from core/config/aspirations.yaml.
    Fail-open: returns the module constants on any parse/path error. Called
    once at snapshot compute time; no caching — the file is tiny and changes
    are rare, so a per-call read costs nothing meaningful."""
    near, stalled = NEAR_COMPLETE_THRESHOLD, STALLED_THRESHOLD
    try:
        import yaml  # optional dep — fail-open if missing
    except ImportError:
        return near, stalled
    cfg_path = SCRIPT_DIR.parent / "config" / "aspirations.yaml"
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return near, stalled
    if not isinstance(data, dict):
        return near, stalled
    block = data.get("consolidation_health") or {}
    if not isinstance(block, dict):
        return near, stalled
    n = block.get("near_complete_threshold")
    s = block.get("stalled_threshold")
    # Validate: both in [0,1] and near > stalled. Invalid config falls open
    # to module defaults rather than surfacing as snapshot-time crashes.
    try:
        nf = float(n) if n is not None else near
        sf = float(s) if s is not None else stalled
        if 0.0 <= sf < nf <= 1.0:
            return nf, sf
    except (TypeError, ValueError):
        pass
    return near, stalled


def _completion_ratio(asp: dict) -> float:
    progress = asp.get("progress") or {}
    total = progress.get("total_goals") or 0
    completed = progress.get("completed_goals") or 0
    if total <= 0:
        return 0.0
    return completed / total


def _tolerant_decode(source: str, raw: str) -> list:
    """-tolerant decode for the daemon's aspirations_read body.

    Background: g-115-766 documented an intermittent failure mode where the
    daemon's aggregate JSON response can arrive as VALID JSON prefix +
    trailing garbage (stale-code window during reload). The previous
    `json.loads(raw or "[]")` path treated such bodies as wholly invalid
    and silently collapsed `active_count` to 0 — which the three downstream
    consumers (aspirations-evolve gap gate, aspirations-select near-complete
    bias, create-aspiration consolidation-gate) read as "portfolio is
    empty," firing wrong-direction decisions.

    guard-383 (N>=2-source aggregator): `compute_snapshot` merges
    `_load_active("world") + _load_active("agent")` into the single
    `consolidation_health` WM slot the three gates treat as COMPLETE. A
    per-source error that returns [] would write a complete-looking LIE
    (e.g. agent-only count presented as the whole portfolio). guard-383
    therefore mandates: a source ERROR is FATAL (sys.exit(1)) — the single
    fail-open boundary is the caller's shell wrapper `|| echo WARN`
    (documented in the module docstring, rb-347), never inside the
    aggregator. A genuinely-empty source is NOT an error.

    Contract (g-115-775 trace + guard-383 / rb-774 pre-apply consultation):
      - Empty body: return []. A genuinely empty queue is a valid state,
        NOT a source error; guard-383 does not apply. Distinct path so a
        real corruption never masquerades as an empty portfolio.
      - JSON-prefix (+ optional trailing garbage): parse via
        json.JSONDecoder().raw_decode (stops at the first valid JSON
        value) and return it. A recoverable body is NOT a source error —
        this is the g-115-766 recovery branch, guard-383-neutral.
      - Unrecoverable body (raw_decode raises): source error → emit ONE
        stderr diagnostic (body prefix truncated to 120, newlines escaped)
        then sys.exit(1). Do NOT return [] (guard-383 action_hint #1).
      - Non-list aggregate (valid JSON but not a list — e.g. a daemon
        error-envelope `{...}` recovered from a g-115-766 object-prefix):
        ALSO a source error → stderr diagnostic + sys.exit(1). Returning
        [] here was the residual silent-loss fresh-eyes flagged
        (msg-20260516-070416-alpha-1156); guard-383 makes it fatal, which
        resolves that finding correctly (loud > silent-[]).
    """
    stripped = (raw or "").lstrip()
    if not stripped:
        return []  # genuinely empty queue — valid state, not a source error
    try:
        obj, _consumed = json.JSONDecoder().raw_decode(stripped)
    except json.JSONDecodeError as exc:
        body_prefix = stripped[:120].replace("\n", "\\n")
        print(
            f"consolidation-health: {source} JSONDecodeError ({exc}); body prefix: {body_prefix!r}",
            file=sys.stderr,
        )
        sys.exit(1)  # guard-383: source error is fatal; fail-open is the wrapper
    if not isinstance(obj, list):
        body_prefix = stripped[:120].replace("\n", "\\n")
        print(
            f"consolidation-health: {source} non-list aggregate (type={type(obj).__name__}); "
            f"body prefix: {body_prefix!r}",
            file=sys.stderr,
        )
        sys.exit(1)  # guard-383: a non-list source body is corrupt, not empty
    return obj


def _load_active(source: str) -> list:
    """Read active aspirations for one source ('world' or 'agent').

    Uses the daemon via _rt (aspirations.py read CLI was deleted in the
    2026-05-14 cutover; _rt is the canonical Python -> daemon client).
    Decode path is g-115-766-tolerant via _tolerant_decode — see that
    helper for the corruption-mode contract.

    guard-383 (action_hint #2): an RtError IS a per-source failure. It is
    fatal (sys.exit(1)), symmetric with _tolerant_decode's corrupt/non-list
    branches — NOT a silent `return []`. The single fail-open boundary is
    the caller's shell wrapper `|| echo WARN` (rb-347).
    """
    try:
        raw = _rt.aspirations_read(source=source, active=True)
    except _rt.RtError as e:
        print(f"consolidation-health: {source} read failed: {e.body or e}", file=sys.stderr)
        sys.exit(1)  # guard-383: source error is fatal; fail-open is the wrapper
    return _tolerant_decode(source, raw)


def compute_snapshot() -> dict:
    # Agent-bound required: the snapshot merges world + agent queues.
    # Running unbound would silently return a world-only count that LIES
    # about being a portfolio snapshot. Fail hard here and let the shell
    # wrapper's `|| echo WARN` be the one fail-open boundary (rb-347).
    if not os.environ.get("MIND_AGENT"):
        print("consolidation-health: ERROR — MIND_AGENT not set; cannot merge agent queue into snapshot", file=sys.stderr)
        sys.exit(1)
    all_active = _load_active("world") + _load_active("agent")
    ratios = [_completion_ratio(a) for a in all_active]
    active_count = len(all_active)
    avg = sum(ratios) / active_count if active_count else 0.0
    near_t, stalled_t = _load_thresholds()
    near = sum(1 for r in ratios if r >= near_t)
    stalled = sum(1 for r in ratios if r < stalled_t)
    return {
        "active_count": active_count,
        "avg_completion": round(avg, 4),
        "near_complete": near,
        "stalled": stalled,
        "computed_at": _dt.datetime.now().isoformat(timespec="seconds"),
    }


def _write_slot(payload: dict) -> int:
    """Pipe the JSON into wm.py set consolidation_health. Returns exit code.

    Invokes wm.py directly via sys.executable rather than shelling through
    wm-set.sh. Background: MSYS Git Bash on Windows cannot resolve Windows-
    form paths (C:/..., /c/..., cygpath-converted) when invoked via Python
    subprocess.run — every form returns exit 127 "No such file or directory"
    (g-115-172 root cause, 2026-04-24, rb-495). The .sh wrapper only exists
    to source _paths.sh + _platform.sh and exec python3; wm.py self-boot-
    straps via _paths.py Python module, so direct invocation skips the
    landmine entirely. Same pattern as create-blocker.py (see comment there
    referencing cargo-cult-detector.py:157-173).
    Env: PYTHONIOENCODING=utf-8 preserves the cp1252→utf-8 shim that
    _platform.sh applied (needed for em-dashes / smart quotes in stdin).
    """
    wm_py = SCRIPT_DIR / "wm.py"
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, str(wm_py), "set", "consolidation_health"],
        input=json.dumps(payload),
        capture_output=True, text=True, check=False,
        env=env,
    )
    if proc.returncode != 0:
        print(f"consolidation-health: wm.py set failed exit={proc.returncode} stderr={proc.stderr.strip()[-200:]}", file=sys.stderr)
    return proc.returncode


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true",
                    help="Write payload to WM slot consolidation_health via wm-set.sh (default: print only)")
    args = ap.parse_args()

    snap = compute_snapshot()
    print(json.dumps(snap, ensure_ascii=False))

    if args.write:
        if _write_slot(snap) != 0:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
