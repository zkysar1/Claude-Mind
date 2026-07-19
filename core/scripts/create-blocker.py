#!/usr/bin/env python3
"""create-blocker.py — Tier 1a hot-path extraction.

Replaces the deterministic orchestration of aspirations-execute/SKILL.md
CREATE_BLOCKER protocol (core/config/create-blocker-protocol-digest.md).

Wraps four existing gates + stores into a single call:
  1. wm-read known_blockers → dedup against existing blocker
  2. blocker-create-gate.sh → structural correctness (probe, evidence, schema, infra)
  3. conclusion-record.sh   → judgment-quality audit (fail-quiet)
  4. capability-gate.sh     → participant correctness
  5. aspirations-add-goal.sh → create Unblock goal (HIGH priority)
  6. wm-set known_blockers  → persist blocker entry
  7. cascade: return list of same-skill pending goals to mark affected

Left to the caller (LLM):
  - Notification call (forged-skill resolution, natural language)
  - Journal entry (prose composition)

Plan: ~/.claude/plans/i-had-one-agent-luminous-reddy.md (Tier 1a #4).

Exit codes:
  0 = blocker created (or appended to existing)
  1 = structural gate blocked (blocker-create-gate exit 1)
  2 = capability gate blocked (capability-gate exit 1)
  3 = input error
  4 = goal-creation failure

JSON stdout always — flags array signals caller action.
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from _paths import AGENT_DIR, PROJECT_ROOT, CORE_ROOT  # type: ignore
from _fileops import log_script_decision  # type: ignore
from _override_helpers import apply_override_all, audit_bulk_override  # type: ignore
import _rt  # canonical Python -> daemon client (post-cutover; see _rt.py)
# gates.blocker_ref is the single source of truth for BLOCKER_REF_TYPES and
# BLOCKER_REF_TTL_HOURS — the daemon-safe primitives module (DECISIONS.md
# §38b / PR 7e/2). aspirations.py delegates to it and no longer re-exports
# either constant, so importing from aspirations fails post-cutover.
# Import at module top — any import error should fail the script at
# startup, not deep inside main() after partial I/O.
# DO NOT reintroduce local copies of either constant.
from gates.blocker_ref import BLOCKER_REF_TYPES, BLOCKER_REF_TTL_HOURS  # type: ignore


# ---------------------------------------------------------------------------
# Subprocess invocations for gates (blocker-create-gate, capability-gate,
# conclusion-record) still use _run_py via sys.executable — those CLI
# subcommands are alive. wm.py read and aspirations.py add-goal were deleted
# in the 2026-05-14 cutover and are now reached via _rt (daemon client).
# wm.py set and aspirations.py update-goal remain alive and use _run_py.
# ---------------------------------------------------------------------------

def _run_py(py_rel_path, args, stdin_payload=None, timeout=30):
    """Invoke core/scripts/<py_rel_path> via sys.executable. Returns (rc, stdout, stderr)."""
    py_path = Path(CORE_ROOT) / "scripts" / py_rel_path
    if not py_path.exists():
        return 127, "", f"script not found: {py_path}"
    cmd = [sys.executable, str(py_path), *args]
    try:
        p = subprocess.run(
            cmd, input=stdin_payload, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s invoking {py_rel_path}"


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def _slug(skill_name):
    import re
    return re.sub(r"[^a-z0-9]+", "-", (skill_name or "unknown").lower()).strip("-")


def _read_known_blockers():
    """Read the known_blockers WM slot via the daemon.

    _rt.wm_read returns the raw JSON string the deleted wm.py read CLI used
    to print — json.dumps(value) with no wrapper, so the parsed payload is
    already the list. If the daemon ever wraps it, fail loud — don't add
    speculative unwrapping fallbacks here.
    """
    # wm.py read CLI was deleted in the 2026-05-14 cutover; _rt.wm_read is
    # the canonical Python -> daemon replacement. Daemon-only: no CLI fallback.
    try:
        out = _rt.wm_read(slot="known_blockers", as_json=True)
    except _rt.RtError as e:
        # Daemon unreachable or slot missing. Fail loud on stderr so the
        # blocker flow surfaces the cause instead of silently deduping against [].
        print(f"wm_read known_blockers failed: {e}", file=sys.stderr)
        return []
    if not out or not out.strip() or out.strip() == "null":
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError as e:
        print(f"wm_read returned non-JSON: {e}", file=sys.stderr)
        return []
    if not isinstance(data, list):
        print(f"wm_read known_blockers returned {type(data).__name__}, expected list",
              file=sys.stderr)
        return []
    return data


def _write_known_blockers(blockers):
    payload = json.dumps(blockers, ensure_ascii=False, default=str)
    rc, _, err = _run_py("wm.py", ["set", "known_blockers"], stdin_payload=payload)
    return rc == 0, err


def _set_goal_blocker_ref(source, goal_id, blocker_ref):
    """Mirror the WM known_blockers entry's blocker_ref onto the source goal record.

    Without this, a goal whose status is set to "blocked" by CREATE_BLOCKER ends up
    with no blocker_ref of its own — only the WM known_blockers list carries the
    structured metadata. The quiescence gate's C2 check requires every blocked
    goal to carry blocker_ref directly (g-274-23 was fixed manually 2026-05-09;
    this propagation prevents recurrence).

    Fail-soft: a non-zero return is reported via stderr at the call site but
    does NOT abort blocker creation. The WM entry remains the authoritative
    record; the goal-record copy is a redundancy for the gate's structural check.
    Returns (ok, err).
    """
    if not blocker_ref:
        return False, "no blocker_ref to propagate"
    payload = json.dumps(blocker_ref, ensure_ascii=False, default=str)
    rc, _, err = _run_py(
        "aspirations.py",
        ["--source", source, "update-goal", goal_id, "blocker_ref", payload],
    )
    return rc == 0, err


def _find_existing(blockers, failure_skill):
    """Match blockers on affected_skills ∋ failure_skill, no resolution."""
    for b in blockers:
        if b.get("resolution"):
            continue
        skills = b.get("affected_skills") or []
        if failure_skill in skills:
            return b
    return None


def main():
    p = argparse.ArgumentParser(description="CREATE_BLOCKER orchestrator (Tier 1a)")
    p.add_argument("--failure-skill", required=True,
                   help="Skill that failed (e.g., efs-ssh, aws-exec)")
    p.add_argument("--failure-reason", required=True,
                   help="Short reason (<=80 chars)")
    p.add_argument("--goal-id", required=True, help="Goal that triggered the blocker")
    p.add_argument("--aspiration-id", required=True,
                   help="Parent aspiration for the new Unblock goal")
    p.add_argument("--blocker-type", default="infrastructure",
                   choices=list(BLOCKER_REF_TYPES))
    # --external-id is the observable identifier the next wake-cycle can probe.
    # Required whenever --blocker-type is partner-response or external-service
    # (those types cite a specific board msg ID or probe ID). Optional but
    # recommended for all other types — when present, the goal's blocker_ref
    # is emitted so the quiescence gate (Change 2) can evaluate eligibility
    # without falling back to narrative defer_reason parsing.
    p.add_argument("--external-id", default=None,
                   help="Observable identifier (board msg ID, probe ID, etc.) "
                        "tied to this blocker. Required for partner-response and "
                        "external-service types.")
    p.add_argument("--state-hash", default=None,
                   help="Optional snapshot hash of the external state at blocker "
                        "creation, for wake-miss detection by the quiescence gate.")
    p.add_argument("--evidence", type=str, default="[]",
                   help="JSON array of evidence entries for blocker-create-gate")
    p.add_argument("--probe-command", type=str, default=None,
                   help="Exact probe command (required by blocker-create-gate)")
    p.add_argument("--schema-probe-evidence", type=str, default=None,
                   help="JSON object for statistical negations")
    p.add_argument("--infra-health-check", type=str, default=None,
                   help="JSON object for infrastructure blockers")
    p.add_argument("--credential-source-enumeration", type=str, default=None,
                   help="JSON array for credentials-required blockers: one "
                        "{source, identity, probed, denied} per credential source "
                        "the runtime could resolve (env pair, default chain, stored "
                        "profile, instance role). Required by blocker-create-gate "
                        "check 5 (guard-1160) — >=2 distinct sources, each "
                        "action-probed and denied.")
    p.add_argument("--diagnostic-context", type=str, default="{}",
                   help="JSON object for blocker record")
    p.add_argument("--intended-participants", default="agent",
                   choices=["agent", "user", "hybrid"])
    p.add_argument("--override-blocker-gate", type=str, default=None)
    p.add_argument("--override-agent-match", type=str, default=None)
    p.add_argument("--override-all", type=str, default=None,
                   help="Bulk override: fans the SAME justification into "
                        "--override-blocker-gate and --override-agent-match if "
                        "they're not individually set. Per-gate flags WIN. "
                        "Records to world/override-bypass-ledger.jsonl.")
    p.add_argument("--capability-evidence", type=str, default=None,
                   help="JSON array for capability-gate --evidence")
    p.add_argument("--source", default="aspirations-execute")
    p.add_argument("--reverify-minutes", type=int, default=30)
    p.add_argument("--dry-run", action="store_true",
                   help="Skip persistence; return what would happen")
    args = p.parse_args()

    # Phase 4 bulk override: --override-all fans into the per-gate slots
    # below before any gate is invoked. Per-gate flags WIN if specified.
    _ovr_token, _ovr_filled = apply_override_all(
        args, ["override_blocker_gate", "override_agent_match"])

    if AGENT_DIR is None:
        print(json.dumps({"error": "no agent bound (MIND_AGENT not set)",
                          "flags": ["no_agent"]}))
        sys.exit(3)

    # Validate external_id requirement for types that must cite one.
    # partner-response and external-service have no meaning without an
    # observable ID — the quiescence gate cannot probe them on wake.
    if args.blocker_type in ("partner-response", "external-service") and not args.external_id:
        print(json.dumps({
            "error": (f"--external-id is required for blocker-type "
                      f"{args.blocker_type!r}. Partner-response and external-service "
                      f"blockers must cite an observable ID (board msg or probe ID)."),
            "flags": ["input_error"],
        }))
        sys.exit(3)

    # -------- Step 1: dedup against existing blocker ----------------------
    blockers = _read_known_blockers()
    existing = _find_existing(blockers, args.failure_skill)
    if existing:
        # Append affected goal, update diagnostic context
        affected = existing.setdefault("affected_goals", [])
        if args.goal_id not in affected:
            affected.append(args.goal_id)
        try:
            new_ctx = json.loads(args.diagnostic_context) or {}
        except json.JSONDecodeError:
            new_ctx = {}
        existing.setdefault("diagnostic_context", {}).update(new_ctx)
        existing["last_affected_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        if not args.dry_run:
            ok, err = _write_known_blockers(blockers)
            if not ok:
                print(json.dumps({"error": f"wm-set failed: {err}", "flags": ["wm_set_failed"]}))
                sys.exit(4)
            # : propagate blocker_ref onto the newly-affected goal
            # so the quiescence gate's C2 check (every blocked goal carries
            # blocker_ref) sees structured metadata without scanning WM.
            # Pre-fix: only the WM known_blockers entry got blocker_ref.
            existing_ref = existing.get("blocker_ref")
            if existing_ref:
                ref_ok, ref_err = _set_goal_blocker_ref(
                    args.source, args.goal_id, existing_ref)
                if not ref_ok:
                    print(f"[create-blocker] WARN: blocker_ref not set on goal "
                          f"{args.goal_id}: {ref_err.strip()}", file=sys.stderr)
        print(json.dumps({
            "summary": f"appended goal {args.goal_id} to existing blocker {existing.get('id')}",
            "flags": ["existing_blocker_appended"],
            "blocker_id": existing.get("id"),
            "unblocking_goal_id": existing.get("unblocking_goal"),
            "new_goal_created": False,
        }, ensure_ascii=False, default=str))
        sys.exit(0)

    # -------- Step 2.55: blocker-create-gate ------------------------------
    try:
        evidence_arr = json.loads(args.evidence)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"--evidence not valid JSON: {e}",
                          "flags": ["input_error"]}))
        sys.exit(3)

    blocker_json = {
        "type": args.blocker_type,
        "affected_skills": [args.failure_skill],
        "failure_reason": args.failure_reason,
        "evidence": evidence_arr,
    }
    if args.schema_probe_evidence:
        try:
            blocker_json["schema_probe_evidence"] = json.loads(args.schema_probe_evidence)
        except json.JSONDecodeError:
            pass
    if args.infra_health_check:
        try:
            blocker_json["infra_health_check"] = json.loads(args.infra_health_check)
        except json.JSONDecodeError:
            pass
    if args.credential_source_enumeration:
        try:
            blocker_json["credential_source_enumeration"] = json.loads(
                args.credential_source_enumeration)
        except json.JSONDecodeError:
            pass

    gate_args = ["--output", "json"]
    if args.probe_command:
        gate_args += ["--probe-command", args.probe_command]
    if args.override_blocker_gate:
        gate_args += ["--override-blocker-gate", args.override_blocker_gate]

    rc, out, err = _run_py(
        "blocker-create-gate.py", gate_args,
        stdin_payload=json.dumps(blocker_json))
    if rc == 1:
        print(json.dumps({
            "summary": "blocker-create-gate blocked",
            "flags": ["structural_gate_blocked"],
            "gate_output": out,
            "gate_stderr": err,
            "remediation": ("retry with canonical probe / second signal / schema probe / "
                            "infra-health, or pass --override-blocker-gate"),
        }, ensure_ascii=False, default=str))
        sys.exit(1)
    if rc != 0 and rc != 1:
        # Gate script error (not a block). Fail-open — proceed with audit warning.
        gate_warning = f"blocker-create-gate rc={rc}: {err.strip()}"
    else:
        gate_warning = None

    # -------- Step 2.56: conclusion-record (fail-quiet) -------------------
    _run_py(
        "conclusion-record.py",
        ["--blocks-goals", args.goal_id,
         "--reverify-minutes", str(args.reverify_minutes)],
        stdin_payload=json.dumps(blocker_json),
        timeout=10,
    )  # fail-quiet per digest — the blocker itself still persists downstream

    # -------- Step 2.6: capability-gate -----------------------------------
    cap_args = [
        "--failure-reason", args.failure_reason,
        "--intended-participants", args.intended_participants,
        "--output", "json",
    ]
    if args.capability_evidence:
        cap_args += ["--evidence", args.capability_evidence]
    if args.override_agent_match:
        cap_args += ["--override-agent-match", args.override_agent_match]

    rc, out, err = _run_py("capability-gate.py", cap_args)
    if rc == 1:
        print(json.dumps({
            "summary": "capability-gate blocked",
            "flags": ["capability_gate_blocked"],
            "gate_output": out,
            "gate_stderr": err,
            "remediation": ("revise --intended-participants, or pass "
                            "--capability-evidence or --override-agent-match"),
        }, ensure_ascii=False, default=str))
        sys.exit(2)

    # -------- Step 3: create Unblock goal --------------------------------
    title_reason = args.failure_reason
    if len(title_reason) > 50:
        title_reason = title_reason[:47] + "..."

    participants_list = {
        "agent": ["agent"],
        "user": ["user"],
        "hybrid": ["agent", "user"],
    }[args.intended_participants]

    goal_payload = {
        "title": f"Unblock: {title_reason}",
        "description": (
            f"Blocker from skill '{args.failure_skill}' while executing {args.goal_id}.\n"
            f"Failure reason: {args.failure_reason}.\n"
            f"Diagnostic context: {args.diagnostic_context}\n"
            f"Evidence: {len(evidence_arr)} item(s) passed blocker-create-gate."
        ),
        "priority": "HIGH",
        "skill": None,
        "participants": participants_list,
        "category": "unblock",
        # origin-signal gate (core/scripts/origin-signal-gate.py): Unblock
        # goals cite the failing goal that triggered CREATE_BLOCKER. Without
        # this field the gate blocks the goal filing — and CREATE_BLOCKER is
        # the ONLY path that responds to unfixable infrastructure failure,
        # so a blocked gate here silently strands blocker goals.
        "origin_signal": f"unblock:{args.goal_id}",
    }

    if args.dry_run:
        print(json.dumps({
            "summary": "dry-run: would create blocker + unblocking goal",
            "flags": ["dry_run"],
            "blocker_json": blocker_json,
            "goal_payload": goal_payload,
            "gate_warning": gate_warning,
        }, ensure_ascii=False, default=str))
        sys.exit(0)

    # aspirations.py add-goal CLI was deleted in the 2026-05-14 cutover;
    # _rt.aspirations_add_goal is the canonical Python -> daemon replacement.
    # Daemon-only: no CLI fallback.
    try:
        add_result = _rt.aspirations_add_goal(
            args.aspiration_id, goal_payload, source=args.source)
    except _rt.RtError as e:
        print(json.dumps({
            "summary": "aspirations-add-goal failed",
            "flags": ["goal_creation_failed"],
            "detail": (e.body or str(e)).strip()[:400],
        }, ensure_ascii=False, default=str))
        sys.exit(4)

    new_goal_id = add_result.get("goal_id") or add_result.get("id")

    # -------- Step 4: build blocker entry ---------------------------------
    blocker_id = f"infra-{_slug(args.failure_skill)}-{_today()}"
    try:
        diag = json.loads(args.diagnostic_context)
    except json.JSONDecodeError:
        diag = {"raw": args.diagnostic_context}

    # BLOCKER_REF_TYPES / BLOCKER_REF_TTL_HOURS / timedelta imported at module top.
    _now = datetime.now()
    # Synthesize an external_id for types that didn't get one — the affected
    # goal+skill pair is still observable (blocker-recheck re-probes it).
    external_id = args.external_id or f"{args.failure_skill}:{args.goal_id}"
    blocker_ref = {
        "type": args.blocker_type,
        "external_id": external_id,
        "state_hash": args.state_hash,
        "created_at": _now.strftime("%Y-%m-%dT%H:%M:%S"),
        "expires_at": (_now + timedelta(hours=BLOCKER_REF_TTL_HOURS[args.blocker_type]))
                      .strftime("%Y-%m-%dT%H:%M:%S"),
    }

    new_blocker = {
        "id": blocker_id,
        "type": args.blocker_type,
        "affected_skills": [args.failure_skill],
        "affected_goals": [args.goal_id],
        "unblocking_goal": new_goal_id,
        "failure_reason": args.failure_reason,
        "diagnostic_context": diag,
        "resolution": None,
        "created_at": _now.strftime("%Y-%m-%dT%H:%M:%S"),
        # blocker_ref travels with the known_blockers WM entry so the
        # quiescence gate can read structured metadata without a second
        # JSONL scan. Same schema as goal.blocker_ref (see goal-schemas.md).
        "blocker_ref": blocker_ref,
    }
    blockers.append(new_blocker)

    # -------- Step 7: persist via wm-set ---------------------------------
    ok, err = _write_known_blockers(blockers)
    if not ok:
        print(json.dumps({
            "summary": "goal created but wm-set failed",
            "flags": ["wm_set_failed", "partial_persist"],
            "blocker_id": blocker_id,
            "unblocking_goal_id": new_goal_id,
            "wm_error": err,
        }, ensure_ascii=False, default=str))
        sys.exit(4)

    # : propagate blocker_ref onto the source goal record so the
    # quiescence gate's C2 check (every blocked goal carries blocker_ref)
    # sees structured metadata directly on the goal — not just on the WM
    # known_blockers entry. Pre-fix:  was fixed manually 2026-05-09.
    ref_ok, ref_err = _set_goal_blocker_ref(args.source, args.goal_id, blocker_ref)
    if not ref_ok:
        print(f"[create-blocker] WARN: blocker_ref not set on goal "
              f"{args.goal_id}: {ref_err.strip()}", file=sys.stderr)

    # -------- Step 5: cascade — return affected goals for caller ----------
    # Caller scans aspirations-compact for same-skill pending goals and
    # appends to this blocker's affected_goals in a second wm-set call.
    # We do NOT auto-cascade here to keep the script side-effects tight.

    result = {
        "summary": f"blocker {blocker_id} created; unblock goal {new_goal_id}",
        "flags": (["created"] + (["gate_warning"] if gate_warning else [])),
        "blocker_id": blocker_id,
        "unblocking_goal_id": new_goal_id,
        "new_goal_created": True,
        "blocker_type": args.blocker_type,
        "affected_skills": [args.failure_skill],
        "participants": participants_list,
        "blocker_ref": blocker_ref,
        "gate_warning": gate_warning,
        "next_steps_for_llm": [
            "cascade: scan aspirations-compact for pending goals with skill="
            + args.failure_skill + " and append to this blocker's affected_goals",
            "notify user: invoke forged skill matching 'notify the user' with "
            "subject='Blocked: " + args.failure_skill + "' and blocker diagnostic summary",
            "journal: append blocker-creation entry with cascade chain and diagnostic",
        ],
    }
    log_script_decision("create-blocker", {
        "outcome": "created",
        "blocker_id": blocker_id,
        "unblocking_goal_id": new_goal_id,
        "blocker_type": args.blocker_type,
        "failure_skill": args.failure_skill,
        "gate_warning": bool(gate_warning),
    })
    audit_bulk_override(_ovr_token, getattr(args, "override_all", None),
                        _ovr_filled,
                        context={"caller": "create-blocker.py:main",
                                 "blocker_id": blocker_id,
                                 "unblocking_goal_id": new_goal_id,
                                 "blocker_type": args.blocker_type,
                                 "failure_skill": args.failure_skill})
    print(json.dumps(result, ensure_ascii=False, default=str))
    sys.exit(0)


if __name__ == "__main__":
    main()
