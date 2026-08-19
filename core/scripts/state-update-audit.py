#!/usr/bin/env python3
"""State-update audit — Tier 1a hot-path extraction.

Replaces aspirations-state-update/SKILL.md Steps 8.8, 8.85, 8.9, 8.10, 8.11
(lines 549-694): pure arithmetic + subprocess calls to existing scripts
(meta-impk.sh, meta-backpressure.sh, meta-set.sh, experience-read.sh,
experience-update-field.sh, evolution-log-append.sh, meta-dead-ends.sh,
meta-generations.sh, board-post.sh).

Plan: ~/.claude/plans/i-had-one-agent-luminous-reddy.md (Tier 1a #5).

Subcommands:
  velocity          — Step 8.8 learning_value computation + snapshot
  backpressure      — Step 8.85 monitor regression check + rollback
  temporal-credit   — Step 8.9 propagate γ-discounted credit to enablers
  relative-advantage — Step 8.10 advantage against category mean
  execution-feedback — Step 8.11 board post (takes LLM-judged scores)
  run-all           — invoke velocity/backpressure/temporal/advantage in order

IN-SCOPE: pure arithmetic, subprocess dispatch.
OUT-OF-SCOPE: clarity/scope/verify/friction scores (LLM-judged, passed as args).

Exit codes: 0=clean, 1=flags raised (rollbacks fired), 2=input error.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from _paths import AGENT_DIR, PROJECT_ROOT, CORE_ROOT, META_DIR  # type: ignore
from _fileops import log_script_decision  # type: ignore

try:
    import yaml  # type: ignore
except ImportError:
    print("ERROR: PyYAML not installed", file=sys.stderr)
    sys.exit(2)


def _run(argv, input_text=None, timeout=30):
    """Run a shell command. Returns (stdout, stderr, returncode)."""
    from _runtime_bash import BASH as bash  # rb-1472: bin-first, honors MIND_SHELL, clean-PATH-safe
    # POSIX-slash the script path. Git Bash / MSYS on Windows mangles
    # backslashes in argv (e.g. `C:\Zak\...` → `C:Zak...`), resulting in
    # "No such file or directory" for every helper .sh call. as_posix()
    # is cross-platform — native bash also accepts forward slashes.
    script_path = (Path(CORE_ROOT) / "scripts" / argv[0]).as_posix()
    full = [bash, script_path] + list(argv[1:])
    try:
        p = subprocess.run(
            full, input=input_text, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        return p.stdout, p.stderr, p.returncode
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return "", str(e), -1


# ─────────────────────────── Step 8.8 velocity ───────────────────────────

def _close_key(goal_id):
    """Idempotency token naming THIS goal's close, or "" when underivable.

    `{goal_id}:{lastAchievedAt or completed_at}`, read from the GOAL RECORD.

    Three candidate sources were measured against the live store and rejected
    (g-115-4542), and they stay rejected:

      - goal_id ALONE is wrong. Measured on 7,814 entries: 2,066 repeat rows
        across 332 goals, dominated by asp-001 recurring cadence closes
        (g-001-01 n=205). Every one is a real close and real learning.
      - A TIME WINDOW is wrong. The store is fleet-shared, so consecutive
        snapshots for DIFFERENT goals land as little as 1s apart (375 pairs
        under 60s), while legitimate same-goal recurring re-closes reach down
        to 31s. The populations overlap; no threshold separates them.
      - completed_at ALONE is wrong, and this is the half that matters:
        recurring goals never get one. They cycle back to `pending` and advance
        `lastAchievedAt`/`achievedCount` instead, so keying on completed_at
        would mint the SAME key for every cadence close and destroy the 2,066
        rows above.

    THE COALESCE IS WHAT MAKES IT WORK, and the order is load-bearing.
    `lastAchievedAt` first: it exists only on the recurring shape and advances
    exactly once per close (daemon `complete-by`, aspirations_write.py — it
    stamps `lastAchievedAt = now` and `achievedCount += 1` in the same block).
    `completed_at` second: the one-shot shape, which closes once. Measured on
    the live store 2026-08-10 — recurring g-001-01 carries lastAchievedAt +
    achievedCount=83 and NO completed_at; one-shot g-115-4542 carries
    completed_at and neither of the other two. The two shapes are disjoint, so
    the coalesce is a shape selector, not a fallback chain.

    WHY NOT THE ITERATION CHECKPOINT (the original source, g-115-4542, replaced
    by g-115-5549): it named `selected_at` from
    `agents/<agent>/session/iteration-checkpoint.json` — a single box-local slot
    written at claim time. Any interleaving between claim and close leaves it
    naming an EARLIER goal, the `cp.get("goal_id") != goal_id` guard fires, and
    the key is silently "". Measured 2026-08-09 on cc-04: of the closes eligible
    after that fix landed, NONE carried a key — including the fix's own close 11
    seconds later, where the checkpoint still held g-001-01 from 43 minutes
    prior. The protection was present in the code and inert at runtime.
    `aspirations-claim.sh`'s own comment already described that anchor as
    unreliable and silently fail-open; it was not retrieved when the source was
    picked. The goal record has no such slot: it is the thing being closed.

    This costs one daemon round-trip per close, which the checkpoint source
    avoided. That trade is the point — a local read that is wrong is not cheaper
    than a remote read that is right.

    FAIL-OPEN on every uncertainty (query failure, unparseable payload, no
    matching record, neither field present): returns "", the flag is omitted,
    and the append is unconditional exactly as before. A wrong key would
    suppress a legitimate row, which is strictly worse than the double-count
    this guards against. Unlike the checkpoint source, a failure here is
    OBSERVABLE — cmd_velocity emits `impk_close_key_underivable`, because the
    defect this replaced was invisible precisely where it was wrong.
    """
    if not goal_id:
        return ""
    out, _err, rc = _run(
        ["aspirations-query.sh", "--goal-field", "id", goal_id, "--full"])
    if rc != 0:
        return ""
    try:
        rows = json.loads(out or "[]")
    except (json.JSONDecodeError, ValueError, TypeError):
        return ""
    if not isinstance(rows, list):
        return ""
    # The raw store key is `id`; the query endpoint ALSO projects `goal_id`.
    # Match either — keying on one alone would break silently if the projection
    # changed, and this function's failure mode is a suppressed real row.
    rec = next((r for r in rows
                if isinstance(r, dict)
                and goal_id in (r.get("id"), r.get("goal_id"))), None)
    if rec is None:
        return ""
    stamp = str(rec.get("lastAchievedAt") or rec.get("completed_at") or "").strip()
    return f"{goal_id}:{stamp}" if stamp else ""


def compute_learning_value(tree_updated, artifacts_count, encoding_score, findings_count):
    """4-component weighted learning_value per SKILL.md line 561.

    None inputs (measurement sentinels, g-115-2441) coerce to 0 for the
    weighted sum — callers decide separately whether the goal was MEASURED
    at all (see cmd_velocity unmeasured-skip)."""
    tree_v = 1.0 if tree_updated else 0.0
    artifacts_v = min(1.0, (artifacts_count or 0) * 0.2)
    enc_v = float(encoding_score or 0.0)
    findings_v = min(1.0, (findings_count or 0) * 0.25)
    return round(
        tree_v * 0.3 + artifacts_v * 0.3 + enc_v * 0.2 + findings_v * 0.2, 4,
    )


def cmd_velocity(args):
    # : UNMEASURED closes must not poison the imp@k series. When the
    # caller passed NO quality input at all (no --tree-updated, and all three
    # value args at their None sentinels), the close was never measured —
    # recording learning_value=0.0 injects a false zero that drags every
    # rolling window down (observed: 0.0 for unambiguously deep goals
    # / beside flagged siblings at 0.6-0.79; pre-fix
    # history was 206/206 dead-zero). Skip the snapshot instead — a gap is
    # honest, a fabricated zero is not. An EXPLICIT `--encoding-score 0.0`
    # (ritual outcome) still records a real zero.
    measured = bool(args.tree_updated) or any(
        v is not None for v in (
            args.artifacts_count, args.encoding_score, args.findings_count,
        )
    )
    lv = compute_learning_value(
        args.tree_updated, args.artifacts_count,
        args.encoding_score, args.findings_count,
    )
    if not measured:
        return {
            "subcommand": "velocity",
            "summary": ("velocity: UNMEASURED (no quality flags passed) — "
                        "imp@k snapshot skipped; pass the § STATE-UPDATE "
                        "quality flags to record learning_value (g-115-2441)"),
            "flags": ["velocity_unmeasured_skipped"],
            "learning_value": None,
            "active_change_ids": "",
            "impk_rc": None,
            "impk_stderr": "",
        }
    # Read active backpressure monitors for credit assignment tags
    bp_stdout, _e, bp_rc = _run(["meta-backpressure.sh", "status"])
    active_change_ids = ""
    if bp_rc == 0 and bp_stdout.strip():
        try:
            bp = json.loads(bp_stdout)
            active_change_ids = ",".join(
                m.get("meta_change_id", "") for m in bp.get("active_monitors", [])
            )
        except json.JSONDecodeError:
            pass

    # Guard: meta-impk.sh snapshot requires a non-empty --goal-id. When
    # velocity is invoked without --goal (e.g. bare run-all smoke tests or
    # callers that legitimately have no goal context), argparse leaves
    # args.goal as None which subprocess converts to "" → "required: --goal-id".
    # Skip the snapshot cleanly instead of crashing: surface a diagnostic
    # flag so callers can distinguish "no goal" from "snapshot broken".
    if not args.goal:
        return {
            "subcommand": "velocity",
            "summary": f"velocity: learning_value={lv} (no goal — snapshot skipped)",
            "flags": ["impk_snapshot_skipped_no_goal"],
            "learning_value": lv,
            "active_change_ids": active_change_ids,
            "impk_rc": None,
            "impk_stderr": "",
        }

    impk_argv = [
        "meta-impk.sh", "snapshot",
        "--goal-id", args.goal,
        "--learning-value", str(lv),
        "--category", args.category or "uncategorized",
        "--active-changes", active_change_ids,
    ]
    # : name THIS close so a second snapshot for it is suppressed
    # rather than double-weighted. Derived here (not passed by the caller) so
    # the hand-typed post-hoc recovery command printed by iteration-close.sh
    # gets the protection without needing a new flag — that command is the
    # documented double-write path, and it is typed at exactly the moment it is
    # easiest to run twice. Empty -> flag omitted -> unchanged legacy behaviour.
    close_key = _close_key(args.goal)
    if close_key:
        impk_argv += ["--close-key", close_key]
    if args.exploration:
        impk_argv.append("--exploration-mode")
    impk_out, impk_err, impk_rc = _run(impk_argv)

    # OBSERVABILITY ( fresh-eyes). The daemon reports
    # `duplicate_suppressed` at rc=0 — deliberately, since flagging a correctly
    # suppressed duplicate as an error would manufacture a false alarm on the
    # very recovery path this fix protects — but rc was the ONLY signal read
    # here and impk_out was captured and DISCARDED. So a dedup that fires and a
    # `_close_key` that has silently degraded to "" produced byte-identical
    # telemetry: nothing distinguished "the guard held" from "the guard was
    # never reached" (guard-1760's report-what-you-RAN gap; guard-2352's
    # recorder-only-on-the-fire-path gap, here with a recorder on NEITHER
    # path). This flag is informational — HARD_FAIL_FLAGS above is a denylist,
    # so it never drives a non-zero exit. Gated on close_key so the legacy
    # no-key path keeps its exact prior behaviour, and fail-open so an
    # unreadable payload adds no flag rather than breaking the audit record.
    impk_flags = [] if impk_rc == 0 else ["impk_snapshot_failed"]
    if impk_rc == 0 and close_key:
        try:
            if json.loads(impk_out or "{}").get("status") == "duplicate_suppressed":
                impk_flags.append("impk_duplicate_suppressed")
        except (json.JSONDecodeError, ValueError, TypeError, AttributeError):
            pass
    # : THE OTHER HALF OF THE OBSERVABILITY GAP. The block above says
    # "the guard held"; nothing said "the guard was never reached". The previous
    # source failed silently for 100% of real closes and the only ledger that
    # existed counted an ABSENT checkpoint — the mode actually observed (a
    # checkpoint PRESENT but naming a different goal) was recorded nowhere at
    # all, which is why an inert guard read as a working one for a full day.
    # Informational, like its sibling: HARD_FAIL_FLAGS is a denylist and this is
    # not in it, so an underivable key never fails the audit. Gated on args.goal
    # so a goal-less legacy invocation stays byte-identical.
    if args.goal and not close_key:
        impk_flags.append("impk_close_key_underivable")

    return {
        "subcommand": "velocity",
        "summary": f"velocity: learning_value={lv} (tree={int(args.tree_updated)},"
                   f" artifacts={args.artifacts_count}, encoding={args.encoding_score},"
                   f" findings={args.findings_count})",
        "flags": impk_flags,
        "learning_value": lv,
        "active_change_ids": active_change_ids,
        "impk_rc": impk_rc,
        "impk_stderr": impk_err.strip() if impk_rc != 0 else "",
    }


# ─────────────────────────── Step 8.85 backpressure ───────────────────────────

def cmd_backpressure(args):
    bp_out, bp_err, bp_rc = _run([
        "meta-backpressure.sh", "check",
        "--learning-value", str(args.learning_value),
    ])
    if bp_rc != 0:
        return {
            "subcommand": "backpressure",
            "summary": f"backpressure: check failed (rc={bp_rc})",
            "flags": ["check_failed"],
            "stderr": bp_err.strip(),
        }
    try:
        bp = json.loads(bp_out) if bp_out.strip() else {}
    except json.JSONDecodeError:
        return {
            "subcommand": "backpressure",
            "summary": "backpressure: malformed JSON from meta-backpressure.sh",
            "flags": ["bad_output"],
        }

    rollbacks_applied = []
    for action in bp.get("rollback_actions", []):
        reason = action.get("reason", "regression detected")
        # rollback_to is None when the monitored change ADDED the key from an
        # absent state (old_value null). Reverting an add-from-absent = opt the
        # criterion back out — but the applied value MUST stay type-safe.
        # Lineage of this line:
        #   str(None) -> the literal STRING "None" (mc-081, 2026-07-18:
        #     weights.opportunity_boost "None" crashed goal-selector
        #     load_weights float() fleet-wide on this box).
        #   "null"   -> a real YAML null: no crash, but load_weights emits a
        #     loud non-numeric WARNING every selection AND the key stays
        #     present, so the restore->re-null thrash recurs (mc-081/083 = 2
        #     rollbacks = dead-end; rb-4159 / zeta ).
        #   "0.0"    -> a NUMERIC floor (this fix, ). load_weights
        #     KEEPS 0.0 (isinstance int/float) and the weighted sum adds
        #     raw*0.0 = 0, so the criterion opts out IDENTICALLY to an absent
        #     key (the sum iterates `for k in WEIGHTS`) — but with NO warning,
        #     and every future monitor now records a numeric old_value, never
        #     None again: the thrash is broken at the root. This is the
        #     "numeric-floor" arm of zeta's "delete-key/numeric-floor" fix;
        #     delete-key is unavailable (meta-set is daemon-only, no delete op).
        rollback_to = action.get("rollback_to")
        # ALSO map the literal strings "None"/"null": monitors created while
        # the bug was live (e.g. mc-082's, old_value "None") already carry the
        # corrupt string and would round-trip it forever.
        if rollback_to is None or rollback_to in ("None", "null"):
            rollback_arg = "0.0"
        else:
            rollback_arg = str(rollback_to)
        _out, _err, rc = _run([
            "meta-set.sh", action.get("strategy_file", ""),
            action.get("field", ""), rollback_arg,
            "--reason", f"BACKPRESSURE ROLLBACK: {reason}",
        ])
        if rc == 0:
            rollbacks_applied.append(action.get("field"))
            # Evolution log append
            log_payload = json.dumps({
                "date": "",
                "event": "backpressure_rollback",
                "details": (
                    f"{action.get('meta_change_id','')}: "
                    f"{action.get('field','')} reverted from "
                    f"{action.get('failed_value','')} to {action.get('rollback_to','')}"
                ),
                "trigger_reason": "regression detected",
            })
            _run(["evolution-log-append.sh"], input_text=log_payload)

    dead_ends = []
    for cand in bp.get("dead_end_candidates", []):
        failed = cand.get("failed_values", [])
        payload = json.dumps({
            "strategy_file": cand.get("strategy_file", ""),
            "field": cand.get("field", ""),
            "value_range": [min(failed) if failed else None, max(failed) if failed else None],
            "evidence": cand.get("evidence"),
            "failure_pattern": f"Rolled back {cand.get('rollback_count', 0)} times",
            "category": "meta_weight",
        })
        _out, _err, rc = _run(["meta-dead-ends.sh", "add"], input_text=payload)
        if rc == 0:
            dead_ends.append(cand.get("field"))

    # Generation update (fail-open)
    _run(["meta-generations.sh", "update", "--learning-value", str(args.learning_value)])

    graduated = list(bp.get("graduated", []))

    flags = []
    if rollbacks_applied:
        flags.append("rollbacks_applied")
    if dead_ends:
        flags.append("dead_ends_registered")
    if graduated:
        flags.append("graduations")

    summary = (
        f"backpressure: {len(rollbacks_applied)} rollback(s), "
        f"{len(dead_ends)} dead-end(s), {len(graduated)} graduated"
    )
    return {
        "subcommand": "backpressure",
        "summary": summary,
        "flags": flags,
        "rollbacks_applied": rollbacks_applied,
        "dead_ends": dead_ends,
        "graduated": graduated,
    }


# ─────────────────────────── Step 8.9 temporal credit ───────────────────────────

def cmd_temporal_credit(args):
    exp_out, _e, exp_rc = _run(["experience-read.sh", "--goal", args.goal])
    if exp_rc != 0 or not exp_out.strip():
        return {
            "subcommand": "temporal-credit",
            "summary": "temporal-credit: no experience record",
            "flags": [],
            "propagated": [],
        }
    try:
        exp = json.loads(exp_out)
    except json.JSONDecodeError:
        return {
            "subcommand": "temporal-credit",
            "summary": "temporal-credit: experience JSON parse failed",
            "flags": ["bad_experience_json"],
            "propagated": [],
        }

    # experience-read.sh returns a LIST when there's no exact-id match
    # (multi-result fallback) or an empty array when nothing found.
    # cmd_temporal_credit needs a single dict. Guard defensively: if the
    # shape isn't a dict, treat as "no experience" and short-circuit the
    # credit-propagation.  surfaced this while smoke-testing the
    #  fix with a synthetic --goal that had no experience record.
    if not isinstance(exp, dict):
        return {
            "subcommand": "temporal-credit",
            "summary": f"temporal-credit: no single experience record (got {type(exp).__name__})",
            "flags": [],
            "propagated": [],
        }

    enabled_by = exp.get("enabled_by") or []
    if not enabled_by:
        return {
            "subcommand": "temporal-credit",
            "summary": "temporal-credit: no enablers",
            "flags": [],
            "propagated": [],
        }

    gamma = 0.9
    propagated = []
    for enabler in enabled_by:
        dist = enabler.get("temporal_distance", 0)
        credit = args.learning_value * (gamma ** dist)
        if credit <= 0.01:
            continue
        enabler_id = enabler.get("experience_id")
        enabler_read, _e, rc = _run(["experience-read.sh", "--id", enabler_id])
        current = 0.0
        if rc == 0 and enabler_read.strip():
            try:
                current = float(json.loads(enabler_read).get("temporal_credit") or 0.0)
            except (json.JSONDecodeError, ValueError, TypeError):
                pass
        new_credit = round(current + credit, 4)
        _out, _err, upd_rc = _run([
            "experience-update-field.sh", enabler_id,
            "temporal_credit", str(new_credit),
        ])
        if upd_rc == 0:
            propagated.append({
                "experience_id": enabler_id,
                "credit_added": round(credit, 4),
                "new_total": new_credit,
                "distance": dist,
            })

    return {
        "subcommand": "temporal-credit",
        "summary": f"temporal-credit: propagated to {len(propagated)} enabler(s)",
        "flags": [],
        "propagated": propagated,
    }


# ─────────────────────────── Step 8.10 relative advantage ───────────────────────────

def cmd_relative_advantage(args):
    if META_DIR is None:
        return {
            "subcommand": "relative-advantage",
            "summary": "relative-advantage: META_DIR unresolved",
            "flags": ["meta_dir_missing"],
        }
    path = Path(META_DIR) / "improvement-velocity.yaml"
    if not path.exists():
        return {
            "subcommand": "relative-advantage",
            "summary": "relative-advantage: no velocity data yet",
            "flags": [],
        }
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:  # noqa: BLE001
        return {
            "subcommand": "relative-advantage",
            "summary": f"relative-advantage: yaml parse failed: {e}",
            "flags": ["velocity_yaml_parse_failed"],
        }

    entries = data.get("entries") or []
    same_cat = [e for e in entries if e.get("category") == args.category][-10:]
    if len(same_cat) < 3:
        return {
            "subcommand": "relative-advantage",
            "summary": f"relative-advantage: insufficient history (n={len(same_cat)})",
            "flags": [],
        }

    vals = [float(e.get("learning_value") or 0.0) for e in same_cat]
    mean = sum(vals) / len(vals)
    adv = round(args.learning_value - mean, 4)

    if args.experience_id:
        _run([
            "experience-update-field.sh", args.experience_id,
            "relative_advantage", str(adv),
        ])

    flags = []
    if adv > 0.1:
        flags.append("above_mean")
    elif adv < -0.1:
        flags.append("below_mean")

    summary = f"relative-advantage: {adv:+.4f} (mean={mean:.4f}, n={len(same_cat)})"
    return {
        "subcommand": "relative-advantage",
        "summary": summary,
        "flags": flags,
        "relative_advantage": adv,
        "category_mean": round(mean, 4),
        "sample_size": len(same_cat),
    }


# ─────────────────────────── Step 8.11 execution feedback ───────────────────────────

def cmd_execution_feedback(args):
    """Post structured cross-agent execution feedback. LLM supplies scores."""
    if not args.created_by or args.created_by in ("unknown", os.environ.get("MIND_AGENT", "")):
        return {
            "subcommand": "execution-feedback",
            "summary": "execution-feedback: skipped (own goal or unknown author)",
            "flags": [],
        }
    payload = {
        "goal_id": args.goal,
        "created_by": args.created_by,
        "executed_by": os.environ.get("MIND_AGENT", "unknown"),
        "clarity": args.clarity,
        "scope_accuracy": args.scope,
        "verification_quality": args.verify,
        "friction": args.friction,
        "notes": args.notes or "",
    }
    _out, _err, rc = _run([
        "board-post.sh", "--channel", "feedback",
        "--type", "execution-feedback",
        "--tags", f"{args.goal},created_by:{args.created_by}",
    ], input_text=json.dumps(payload))

    summary = (
        f"execution-feedback: clarity={args.clarity} scope={args.scope} "
        f"verify={args.verify} friction={args.friction}"
    )
    return {
        "subcommand": "execution-feedback",
        "summary": summary,
        "flags": [] if rc == 0 else ["board_post_failed"],
        "payload": payload,
    }


# ─────────────────────────── run-all ───────────────────────────

def cmd_run_all(args):
    """Chain velocity → backpressure → temporal-credit → relative-advantage.

    Skips execution-feedback (requires LLM-judged scores, caller invokes it).
    Short-circuits on outcome_class == "routine" per SKILL.md gate.
    """
    if args.outcome_class == "routine":
        return {
            "subcommand": "run-all",
            "summary": "run-all: skipped (outcome_class=routine)",
            "flags": [],
        }
    r1 = cmd_velocity(args)
    if "velocity_unmeasured_skipped" in r1.get("flags", []):
        # : no quality inputs → no measurement. Backpressure,
        # temporal-credit, and relative-advantage all consume the measured
        # learning_value; feeding them a fabricated 0.0 recreates the same
        # false-zero poison one layer down (backpressure could trip rollback
        # monitors on a phantom velocity collapse). Skip the whole cascade.
        return {
            "subcommand": "run-all",
            "summary": ("run-all: UNMEASURED (no quality flags) — velocity "
                        "cascade skipped; pass § STATE-UPDATE quality flags "
                        "(g-115-2441)"),
            "flags": ["velocity:velocity_unmeasured_skipped"],
            "results": {"velocity": r1},
        }
    lv = r1.get("learning_value", 0.0)
    bp_args = argparse.Namespace(learning_value=lv)
    r2 = cmd_backpressure(bp_args)
    tc_args = argparse.Namespace(goal=args.goal, learning_value=lv)
    r3 = cmd_temporal_credit(tc_args)
    ra_args = argparse.Namespace(
        category=args.category, learning_value=lv, experience_id=args.experience_id,
    )
    r4 = cmd_relative_advantage(ra_args)

    all_flags = []
    for name, r in (("velocity", r1), ("backpressure", r2),
                    ("temporal-credit", r3), ("relative-advantage", r4)):
        all_flags.extend(f"{name}:{f}" for f in r.get("flags", []))
    summary = f"run-all: lv={lv}, {len(all_flags)} flag(s)"
    return {
        "subcommand": "run-all",
        "summary": summary,
        "flags": all_flags,
        "results": {"velocity": r1, "backpressure": r2,
                    "temporal_credit": r3, "relative_advantage": r4},
    }


DISPATCH = {
    "velocity": cmd_velocity,
    "backpressure": cmd_backpressure,
    "temporal-credit": cmd_temporal_credit,
    "relative-advantage": cmd_relative_advantage,
    "execution-feedback": cmd_execution_feedback,
    "run-all": cmd_run_all,
}


# Flags that mean the audit could NOT complete — only these drive a non-zero exit
# (iteration-close.sh WARNs on rc!=0). Every OTHER flag is an informational finding
# (above_mean/below_mean, backpressure events — often positive) or a best-effort/
# transient telemetry-write failure: impk_snapshot_failed = a write_conflict that
# survived the daemon's own RMW retry (meta_impk.locked_rmw, ) — a
# "safe-to-retry 409" (rb-2639) that records on a later, less-contended close;
# board_post_failed and impk_snapshot_skipped_no_goal are likewise non-fatal.
# Pre-partition, `exit(1 if flags else 0)` fired a spurious "state-update-audit.sh
# failed (non-fatal)" WARN inside iteration-close on nearly every deep close — any
# goal above/below its category mean, or any transient snapshot write_conflict —
# while the audit's core computation succeeded (; the read/exit-side
# complement to 's write-side retry). Do NOT add a client-side snapshot
# retry here: conflict-retry is owned by the daemon endpoint (single writer).
HARD_FAIL_FLAGS = {
    "check_failed",                # a sub-check raised / returned non-zero
    "bad_output",                  # a sub-check's stdout was unparseable
    "bad_experience_json",         # temporal-credit could not parse the experience record
    "meta_dir_missing",            # relative-advantage has no meta dir to read
    "velocity_yaml_parse_failed",  # improvement-velocity.yaml is corrupt
}


def _has_hard_failure(flags):
    """True if any flag names a condition that PREVENTED the audit from completing.
    Flags are '<sub>:<name>' at the run-all level and bare '<name>' when a
    subcommand runs standalone — compare the suffix so both forms match."""
    for f in flags or []:
        if f.rsplit(":", 1)[-1] in HARD_FAIL_FLAGS:
            return True
    return False


def main():
    p = argparse.ArgumentParser(description="State-update audit (Tier 1a)")
    p.add_argument("subcommand", choices=list(DISPATCH.keys()))
    p.add_argument("--goal", type=str, default="", help="Goal id")
    p.add_argument("--outcome-class", type=str, default="deep")
    p.add_argument("--category", type=str, default="uncategorized")
    p.add_argument("--experience-id", type=str, default=None)
    p.add_argument("--tree-updated", action="store_true")
    # None defaults are MEASUREMENT sentinels (): cmd_velocity treats
    # "no quality input passed at all" as UNMEASURED and skips the imp@k
    # snapshot instead of recording a false learning_value=0.0. An explicit
    # `--encoding-score 0.0` (ritual outcome) still records a real zero.
    p.add_argument("--artifacts-count", type=int, default=None)
    p.add_argument("--encoding-score", type=float, default=None)
    p.add_argument("--findings-count", type=int, default=None)
    p.add_argument("--exploration", action="store_true")
    p.add_argument("--learning-value", type=float, default=0.0,
                   help="For backpressure/temporal-credit when called standalone")
    p.add_argument("--created-by", type=str, default=None)
    p.add_argument("--clarity", type=int, default=None)
    p.add_argument("--scope", type=int, default=None)
    p.add_argument("--verify", type=int, default=None)
    p.add_argument("--friction", type=str, default=None,
                   choices=[None, "low", "medium", "high"])
    p.add_argument("--notes", type=str, default=None)
    args = p.parse_args()

    result = DISPATCH[args.subcommand](args)
    log_script_decision("state-update-audit", {
        "subcommand": args.subcommand,
        "flags": result.get("flags", []),
    })
    print(json.dumps(result, ensure_ascii=False, default=str))
    sys.exit(1 if _has_hard_failure(result.get("flags")) else 0)


if __name__ == "__main__":
    main()
