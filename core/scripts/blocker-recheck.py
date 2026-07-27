#!/usr/bin/env python3
"""Blocker Recheck — re-examine aged blockers against the capability gate.

For every blocker older than --max-age-hours that routed to `[user]` (or
`[agent, user]`), re-run capability-gate.py against the original failure_reason.
If the gate now matches an agent-provisionable capability that was overlooked
at creation time, this script can auto-clear the blocker and write an Investigate
goal so the retrieval lapse is learned from instead of buried.

Called by aspirations-precheck Phase 0.5b.0.5 (Capability Recheck Sweep).
Reads working memory via _rt.wm_read (daemon client); writes via wm.py set
(still alive). Investigate goals filed via _rt.aspirations_add_goal (daemon
client). Dry-run by default; pass --apply to actually clear blockers and
create Investigate goals.

Exit codes: always 0 (reporting tool). Use the JSON output's `actions_taken`
field to determine what changed.
"""

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from _dt import parse_naive_iso  # noqa: E402  (shared tzinfo-stripping naive-ISO parse, )
import _rt  # canonical Python -> daemon client (post-cutover; see _rt.py)

# Blocker types that are STRUCTURALLY ineligible for auto-clearance by this
# script. No keyword-match score can substitute for the human action these
# represent — SSH trust rotation, credential issuance, and physical hardware
# actions cannot be agent-provisioned regardless of how strongly a capability
# keyword appears to match. See guard-146 (security-trust exclusion) and
# session-47 post-mortem for the incident that surfaced this gap.
HUMAN_ONLY_BLOCKER_TYPES = {
    "security-trust",
    "credentials-required",
    "physical-hardware",
    "user_action",
}


def _run(argv, input_text=None) -> tuple:
    """Run a subprocess. Return (returncode, stdout, stderr)."""
    result = subprocess.run(
        argv,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode, result.stdout, result.stderr


def _py(args: list, input_text=None) -> tuple:
    """Run a core/scripts/*.py helper via the CURRENT Python interpreter.

    INVARIANT: uses sys.executable directly, not a bash subprocess. On Windows
    the parent process is reached via the python3 shim which execs `py`; the
    resulting interpreter's sys.executable points at the real Python binary,
    so child subprocesses bypass the shim cleanly. Shelling through bash for
    these helpers was unreliable because `bash` from Python can resolve to
    WSL bash.exe (different PATH, no python3 shim). Do not reintroduce.
    """
    return _run([sys.executable] + args, input_text=input_text)


def _tolerant_decode(slot, raw):
    """-tolerant decode for the wm_read('known_blockers') body.

    Thin wrapper around `_rt.tolerant_decode_aggregate` (extracted via
    g-115-949; 5th-site migration g-115-1057). Sister sites:
    consolidation-health.py, defer-recheck.py, parent-supersession-sweep.py,
    precondition-defer-recheck.py — all delegate to the same shared helper.

    One pre-delegation early-return preserves wm-slot-specific semantics:
    `wm_read` returns the literal string `"null"` when the slot was set to
    JSON null. That is the canonical empty-slot serialization (NOT a source
    error or a corrupt aggregate), and must map to None so the caller's
    `if data is None: return []` collapses it to an empty blocker list.
    `_rt.tolerant_decode_aggregate` parses "null" as Python None and
    correctly classifies it as a non-dict-and-non-list aggregate (fatal);
    the early-return below short-circuits that path for THIS slot only,
    keeping the daemon-aggregator's strict contract intact for all other
    bodies.

    See _rt.tolerant_decode_aggregate for the full guard-383 contract
    (raw_decode recovery, JSONDecodeError fatal, non-aggregate fatal). The
    fail-open boundary is the caller's shell wrapper (rb-347), never
    inside this aggregator.
    """
    if (raw or "").lstrip() == "null":
        return None  # canonical wm-slot empty-state literal
    return _rt.tolerant_decode_aggregate(f"blocker-recheck: {slot}", raw)


def _wm_read_blockers() -> list:
    """Read the known_blockers slot from working memory.

    Uses the daemon via _rt (wm.py read CLI was deleted in the
    2026-05-14 cutover; _rt.wm_read is the canonical Python -> daemon
    client). Daemon-only: no CLI fallback.

    Parse path is g-115-766-tolerant via `_tolerant_decode` — see that
    helper for the contract. Applied via g-115-797-A2 (bravo audit
    catalog row 3) — replaces the prior silent-collapse
    `except json.JSONDecodeError: return []` that would hide corruption
    behind a "no blockers to recheck" no-op and freeze every aged blocker
    indefinitely (blocker recovery loop never re-evaluates).

    RtError handling — guard-383 fatal symmetry (rb-987):
    `_wm_read_blockers()` feeds the blocker-recheck loop at line 184.
    Per guard-383, a silent `return []` on per-source error writes a
    complete-looking lie to consumers (zero blockers instead of "wm
    unreachable"). The exemplar consolidation-health.py corrected this
    in commit 28a3b7a; A3 sibling (precondition-defer-recheck.py) and
    A4 (parent-supersession-sweep.py) followed the corrected pattern.
    A2 matches the corrected exemplar / A3 / A4 — NOT A1
    (defer-recheck.py)'s pre-correction silent-return.
    The single fail-open boundary is the caller's shell wrapper
    `|| echo WARN` (rb-347), never inside this reader.
    """
    try:
        out = _rt.wm_read(slot="known_blockers", as_json=True)
    except _rt.RtError as e:
        print(f"[blocker-recheck] known_blockers wm_read failed: {e.body or e}",
              file=sys.stderr)
        sys.exit(1)  # guard-383: source error fatal — single fail-open boundary is wrapper
    data = _tolerant_decode("known_blockers", out)
    if data is None:
        return []
    return data if isinstance(data, list) else []


def _wm_set_blockers(blockers: list) -> bool:
    rc, _, _ = _py(
        [str(SCRIPT_DIR / "wm.py"), "set", "known_blockers"],
        input_text=json.dumps(blockers),
    )
    return rc == 0


def _age_hours(detected_at):
    """Compute hours since detected_at. Returns None if missing or unparsable.

    Broad except is intentional: detected_at may be any JSON-loadable value
    (dict, list, int, None). If we cannot determine age, the caller skips
    the blocker — we refuse to act on uncertain state. JSON-safe: None
    serializes cleanly; float('inf') does not.
    """
    if not detected_at:
        return None
    try:
        t = parse_naive_iso(detected_at)
    except Exception:
        return None
    # parse_naive_iso RETURNS None for an unparsable value rather than raising,
    # so the except above never fires for e.g. a dict/list/int stamp — and the
    # subtraction below then raised an uncaught TypeError, aborting the entire
    # sweep instead of skipping the one bad blocker. That contradicted this
    # function's own contract ("if we cannot determine age, the caller skips
    # the blocker"). Latent while detected_at was universally absent (the
    # `if not detected_at` early return caught every real call); reachable once
    #  made the created_at alias readable. Surfaced by
    # test_reader_returns_none_when_both_absent.
    if t is None:
        return None
    return (dt.datetime.now() - t).total_seconds() / 3600.0


def _blocker_id(b: dict) -> str | None:
    """Blocker identity, tolerating the legacy key ().

    `blocker_id` is the documented schema key (handoff-working-memory.md:152)
    and is what infra-health.py and every other reader use. create-blocker.py
    historically wrote `id` instead, so blockers already in agents' working
    memories carry only that. Without the alias, an aged legacy blocker that
    reaches the recheck reports blocker_id=null in its detail record and its
    failure_reason fallback collapses to the empty string -- the recheck runs
    but its output cannot be traced back to a blocker. Bounded migration shim;
    removable once fleet blockers have cycled.
    """
    return b.get("blocker_id") or b.get("id")


def _run_gate(failure_reason: str, intended: str) -> dict:
    rc, out, err = _py([
        str(SCRIPT_DIR / "capability-gate.py"),
        "--failure-reason", failure_reason,
        "--intended-participants", intended,
        "--output", "json",
    ])
    try:
        return json.loads(out)
    except Exception:
        # Gate invocation broken — visible on stderr so this doesn't rot silently.
        print(f"[blocker-recheck] capability-gate invocation failed (rc={rc}): "
              f"{err.strip() or out.strip() or '(no output)'}", file=sys.stderr)
        return {"match_count": 0, "would_block": False, "error": "gate invocation failed"}


def _add_investigate_goal(aspiration_id: str, blocker: dict, gate_result: dict) -> str:
    """Create an Investigate goal so the retrieval lapse gets learned from."""
    match = (gate_result.get("matches") or [{}])[0]
    matched_skill = match.get("skill") or (match.get("row") or "")[:60]
    matched_kw = match.get("matched_keyword", "")
    title = f"Investigate: capability '{matched_skill}' missed at blocker creation"
    description = (
        f"The capability gate re-examined blocker {blocker.get('blocker_id')} "
        f"after it aged past threshold and found an agent-provisionable "
        f"capability that was overlooked at creation time.\n\n"
        f"Matched capability: {matched_skill} (keyword: {matched_kw})\n"
        f"Original failure_reason: {blocker.get('reason') or blocker.get('diagnostic_context', {}).get('failure_reason', '(unknown)')}\n\n"
        f"Analyze: why did the CREATE_BLOCKER Step 2.5 capability scan miss this? "
        f"Was the failure_reason wording ambiguous? Was the capability registry "
        f"stale at that time? Extract a guardrail or update the rule if pattern-recurring."
    )
    goal_record = {
        "title": title,
        "description": description,
        "priority": "MEDIUM",
        "participants": ["agent"],
        "category": "framework-maintenance",
        "tags": ["capability-miss", "retrieval-lapse", "learning"],
        # origin-signal-gate: the aged blocker IS the triggering signal.
        "origin_signal": f"investigate:blocker-{blocker.get('blocker_id') or 'unknown'}",
    }
    # aspirations.py add-goal CLI was deleted in the 2026-05-14 cutover;
    # _rt.aspirations_add_goal is the canonical Python -> daemon replacement.
    # Daemon-only: no CLI fallback.
    try:
        result = _rt.aspirations_add_goal(aspiration_id, goal_record, source="world")
    except _rt.RtError as e:
        return f"<add-goal-failed:{(e.body or str(e)).strip() or 'no detail'}>"
    goal_id = result.get("goal_id") or result.get("id")
    return (goal_id or "<unknown-id>")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Re-examine aged blockers against the capability gate."
    )
    ap.add_argument("--max-age-hours", type=float, default=4.0,
                    help="Blockers older than this are rechecked. Default 4h.")
    ap.add_argument("--apply", action="store_true",
                    help="Actually clear blockers + create Investigate goals. Default: dry-run.")
    ap.add_argument("--investigate-aspiration", default="asp-115",
                    help="World-level aspiration ID to add Investigate goals under. "
                         "Default: asp-115 (Recurring Infrastructure Monitoring). "
                         "Must exist in world/aspirations.jsonl (not an agent-local queue).")
    args = ap.parse_args(argv)

    blockers = _wm_read_blockers()
    report = {
        "agent": os.environ.get("MIND_AGENT", ""),
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "total_blockers": len(blockers),
        "rechecked": 0,
        "matches_found": 0,
        "cleared": 0,
        "investigate_goals_created": [],
        "actions_taken": "dry-run" if not args.apply else "apply",
        "details": [],
    }

    updated = []
    for b in blockers:
        # Only re-examine blockers that went to user (or hybrid) and are unresolved
        if not isinstance(b, dict):
            updated.append(b)
            continue
        if b.get("resolution"):
            updated.append(b)
            continue
        # Structural type filter — human-only blocker categories NEVER auto-clear.
        # A security-trust rotation, credential issuance, or hardware action
        # cannot be agent-provisioned regardless of keyword-match score. The
        # correct path for these is to surface them via pending-question
        # re-raise, not to clear them programmatically.
        if b.get("type") in HUMAN_ONLY_BLOCKER_TYPES:
            updated.append(b)
            continue
        participants = b.get("participants") or []
        # Recheck any blocker that went to [user], [agent,user], or has no
        # participants field (older/legacy blockers). Pure [agent] blockers
        # are already agent-routed so there's no retrieval lapse to catch.
        is_user_routed = (participants == ["user"]
                          or set(participants) == {"agent", "user"}
                          or not participants)
        if not is_user_routed:
            updated.append(b)
            continue

        # LEGACY-SHAPE TOLERANCE (). `detected_at` is the documented
        # schema key and is now emitted by both writers, but blockers created
        # BEFORE that fix are live in agents' working memories carrying only
        # create-blocker.py's `created_at`. Without this alias they stay
        # permanently unreachable: age is None -> `continue` at EVERY age, so
        # the recheck sweep silently reports rechecked=0 forever rather than
        # "not yet aged". Same shape as unblock-intake-probe.py:469. The two
        # keys are aliases for one fact (both stamped at record creation), not
        # competing sources -- this is a bounded migration shim, removable once
        # fleet blockers have cycled, NOT a permanent fallback chain.
        age = _age_hours(b.get("detected_at") or b.get("created_at"))
        # age is None if BOTH keys are missing or unparsable — skip rather
        # than guess. age_hours below threshold — not yet aged, also skip.
        if age is None or age < args.max_age_hours:
            updated.append(b)
            continue

        report["rechecked"] += 1
        failure_reason = (
            b.get("reason")                       # infra-health.py streak alerts
            # create-blocker.py stores the narrative at TOP LEVEL as
            # `failure_reason`, not under diagnostic_context (which is
            # caller-supplied JSON and carries the key only by luck). This
            # rung was missing, so for every canonically-created blocker the
            # chain fell through to the ID STRING and fed *that* to the
            # capability gate below -- a meaningless verdict on an
            # identifier rather than a re-probe of the actual failure.
            # Unreachable until  fixed the age filter above; fixing
            # only the filter would have made the sweep run and still decide
            # on garbage input.
            or b.get("failure_reason")
            or (b.get("diagnostic_context") or {}).get("failure_reason")
            or _blocker_id(b) or ""
        )
        gate = _run_gate(failure_reason, "user")
        first_match = (gate.get("matches") or [{}])[0] if gate.get("matches") else {}
        top_match = first_match.get("skill") or (first_match.get("row") or "")[:80] or None
        detail = {
            "blocker_id": _blocker_id(b),
            "age_hours": round(age, 1),
            "match_count": gate.get("match_count", 0),
            "would_block": gate.get("would_block", False),
            "top_match": top_match,
            "matched_keyword": first_match.get("matched_keyword"),
        }

        if gate.get("would_block"):
            report["matches_found"] += 1
            if args.apply:
                # INVARIANT: create the Investigate goal FIRST, THEN clear the
                # blocker. If reversed, a failed add-goal silently loses both
                # the blocker (so the user never sees the issue again) AND
                # the learning signal (no goal spawned to analyze the lapse).
                # Do not reorder.
                asp_id = args.investigate_aspiration
                goal_id = _add_investigate_goal(asp_id, b, gate)
                if goal_id.startswith("<add-goal-failed"):
                    detail["action"] = f"add-goal failed, blocker NOT cleared ({goal_id})"
                else:
                    top = (gate.get("matches") or [{}])[0]
                    b["resolution"] = {
                        "method": "capability-gate-recheck",
                        "cleared_at": dt.datetime.now().isoformat(timespec="seconds"),
                        "matched_capability": top.get("skill") or (top.get("row") or "")[:80] or None,
                        "note": "Blocker auto-cleared: capability-gate matched an agent-provisionable skill that was overlooked at creation time.",
                        "investigate_goal": goal_id,
                    }
                    report["cleared"] += 1
                    report["investigate_goals_created"].append({
                        "blocker_id": _blocker_id(b),
                        "goal_id": goal_id,
                        "aspiration_id": asp_id,
                    })
                    detail["action"] = "cleared + investigate goal created"
            else:
                detail["action"] = "would clear (dry-run)"
        else:
            detail["action"] = "legitimate user-routing; leaving as-is"

        report["details"].append(detail)
        updated.append(b)

    if args.apply and report["cleared"] > 0:
        _wm_set_blockers(updated)
        # Wake-on-signal (): tells interruptible-sleep.sh to exit 2 and
        # break backoff early when at least one blocker clears. Non-blocking —
        # the signal is purely advisory; if the script is missing or fails the
        # normal backoff timer still fires.
        # Windows path-separator fix ( audit): invoke via bash with
        # .as_posix() — direct .sh execution fails on Windows (no shebang
        # follow), AND a Windows-backslash path would be stripped by bash's
        # escape interpretation. Wrapped in except Exception: pass so the
        # advisory signal never blocks the script.
        try:
            from _runtime_bash import BASH  # rb-1472: not bare "bash"
            subprocess.run(
                [BASH,
                 (SCRIPT_DIR / "session-signal-set.sh").as_posix(),
                 "blocker-cleared"],
                check=False,
                timeout=5,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
