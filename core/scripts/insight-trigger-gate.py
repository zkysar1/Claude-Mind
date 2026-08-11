#!/usr/bin/env python3
"""Insight-Trigger Gate — bash-enforced follow-through on cross-agent findings.

Converts the aspirations-select Phase 2.07 LLM-discretionary `insight_trigger`
scan into a deterministic gate. **Invocation (g-115-758 truth-up 2026-05-16):**
fires from /fresh-eyes-code SKILL.md Phase 5 (event-driven on each
fresh-eyes-code invocation that publishes severity:invalidates|constrains
findings). This gate is NOT wired into aspirations-precheck — the periodic
cadence path is owned by the sibling `insight-trigger-sweep.py` (g-115-754,
recurring 1h) which covers any-severity findings with the
`requires_action_by`/`action_type` tag shape and origin_signal
`insight_trigger:<msg_id>`. The previous docstring (pre-2026-05-16) claimed
"Fires from aspirations-precheck Phase 0.75" — that wiring was never
landed. The split is intentional: event-triggered on fresh-eyes (this
gate, narrow severity) + periodic sweep (broader severity, idempotent).

Severity → action map (asp-248 / g-248-03):
  invalidates — create Investigate goal for each affected goal-id
  constrains  — append constraint note to the affected goal's description
  enables     — no mandatory action (already informational in the selector's
                directive_boost scoring)
  informs     — no mandatory action (log-only)

A finding counts as "processed" when this agent has either:
  1. Replied to it on the board via board-post.sh --reply-to, AND
  2. Logged an action entry to <agent>/session/insight-actions.jsonl

Skips findings authored by this agent (self-triggers don't apply).

Reads world/board/findings.jsonl directly (avoids the WSL-bash CRLF subprocess
bug documented in goal-duplication-gate.py).

Exit codes:
  0 = completed (0 or more actions taken)
  1 = framework error (unwritable logs, missing scripts, bad schema)
  2 = dry-run with would-act items (only when --dry-run supplied)

Arguments:
  --since-hours N    (default 24) — scan window
  --dry-run          don't write goals / post replies; print what WOULD happen
  --limit N          (default 10) — cap actions per invocation to avoid runaway
  --output json|human
"""

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import _rt  # canonical Python -> daemon client (post-cutover; see _rt.py)

from _fileops import locked_append_jsonl  # noqa: E402
from _paths import agent_dir as _agent_dir  # noqa: E402
from _paths import enumerate_agent_confs as _enumerate_agent_confs  # noqa: E402
from _dt import parse_naive_iso  # noqa: E402  (shared tzinfo-stripping naive-ISO parse, /)
from _paths import ENVIRONMENT_ID  # noqa: E402  ( world identity)
from peer_surface import routing_tag_targets_agent  # noqa: E402  ()


#  / rb-1150: terminal statuses that mean "no Investigate needed
# - target already resolved". Mirrors insight-trigger-sweep.py ()
# and unblock-parent-status-sweep.py:112 (rb-908 lineage). Audit-time vs
# apply-time staleness gap: a finding posted to the board at T0 with
# `affects:<g-id>` can sit in the queue until /fresh-eyes-code fires this
# gate at T+N hours; by then the target may have closed. Probing at filing
# time avoids spawning duplicate Investigate work.
#  (zeta allowlist audit D1): synced to the SSOT
# aspirations.TERMINAL_GOAL_STATUSES -- previously drifted (missing
# expired+decomposed, carried a bogus archived that is not a valid goal
# status). Effect of the drift: expired/decomposed targets were mis-read as
# non-terminal and still spawned Investigate work. Parity enforced by
# tests/test_terminal_goal_states_parity.py.
TERMINAL_GOAL_STATES = {"completed", "skipped", "expired", "decomposed", "superseded"}


def _read_local_paths_conf(agent_name):
    conf = _agent_dir(agent_name) / "local-paths.conf"
    out = {}
    if not conf.is_file():
        return out
    for line in conf.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _world_dir():
    agent = os.environ.get("MIND_AGENT", "").strip()
    conf = _read_local_paths_conf(agent) if agent else {}
    wp = conf.get("WORLD_PATH")
    if not wp:
        return None
    # Route through _absolutize so a drive-letter WORLD_PATH is
    # absolutized correctly even on POSIX-flavored Python ().
    from _paths import _absolutize
    return _absolutize(wp)


def _parse_tag_value(tags, prefix):
    for t in tags:
        if isinstance(t, str) and t.startswith(prefix):
            return t[len(prefix):]
    return None


def _parse_all_tag_values(tags, prefix):
    out = []
    for t in tags:
        if isinstance(t, str) and t.startswith(prefix):
            out.append(t[len(prefix):])
    return out


def _actions_log_path():
    """Per-agent session log of insight-trigger actions taken."""
    agent = os.environ.get("MIND_AGENT", "").strip()
    if not agent:
        return None
    return _agent_dir(agent) / "session" / "insight-actions.jsonl"


def _already_processed(finding_id):
    """Has this agent already logged an action for this finding?"""
    log = _actions_log_path()
    if log is None or not log.exists():
        return False
    try:
        with open(log, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("finding_id") == finding_id:
                    return True
    except Exception:
        return False
    return False


def _already_filed_in_aspirations(msg_id):
    """Cross-aspiration dedup: has a goal with origin_signal pointing at this
    msg_id already been filed under either the gate's OR the sweep's format?

    Asymmetric-dedup fix per rb-942 + g-115-758: this gate writes
    `origin_signal=board_post:<msg_id>` for Investigate goals it auto-files.
    The sibling `insight-trigger-sweep.py` (g-115-754, recurring 1h) writes
    `origin_signal=insight_trigger:<msg_id>` for the same finding-type. The
    aspirations.py duplication-gate does EXACT-STRING match on origin_signal,
    so different formats sail past the gate and a duplicate Investigate lands.

    This helper scans world + per-agent aspirations.jsonl for goals whose
    origin_signal matches EITHER format and returns True so `_collect_triggers`
    can drop the trigger before `_act_on_trigger` files anything.

    Fail-open: if either queue is unreadable, return False and let the
    downstream aspirations.py duplication-gate be the final backstop.
    """
    if not msg_id:
        return False
    candidates = {
        "board_post:{}".format(msg_id),
        "insight_trigger:{}".format(msg_id),
    }
    world = _world_dir()
    agent = os.environ.get("MIND_AGENT", "").strip()
    queues = []
    if world is not None:
        queues.append(world / "aspirations.jsonl")
    if agent:
        queues.append(_agent_dir(agent) / "aspirations.jsonl")
    for qp in queues:
        try:
            if not qp.exists():
                continue
            with open(qp, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    for g in rec.get("goals") or []:
                        if g.get("origin_signal") in candidates:
                            return True
        except Exception:
            # Fail-open per rb-942: aspirations.py duplication-gate is the
            # final backstop on the exact-string side; we just want to catch
            # the format-asymmetry case here.
            continue
    return False


def _load_findings(since_hours):
    # DO NOT route this through `bash core/scripts/board-read.sh` — the
    # WSL-bash CRLF subprocess bug (rb-350) produces empty output and silently
    # bypasses the whole gate. Read findings.jsonl directly.
    world = _world_dir()
    if world is None:
        return None, "no WORLD_PATH"
    fp = world / "board" / "findings.jsonl"
    if not fp.exists():
        return [], None
    cutoff = dt.datetime.now() - dt.timedelta(hours=since_hours)
    results = []
    try:
        with open(fp, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ts = rec.get("timestamp") or rec.get("posted_at") or rec.get("created")
                if ts:
                    rec_time = parse_naive_iso(ts)
                    if rec_time is not None and rec_time < cutoff:
                        continue
                results.append(rec)
    except Exception as e:
        return None, "read error: " + str(e)
    return results, None


def _collect_triggers(findings, self_agent):
    """Filter findings down to unprocessed insight_triggers addressed to us."""
    out = []
    for rec in findings:
        tags = rec.get("tags") or []
        if "insight_trigger" not in tags:
            continue
        finding_kind = _parse_tag_value(tags, "finding_kind:") or "coordination"
        if finding_kind != "coordination":
            continue  # code_review and advisory are not action-routed
        if rec.get("author") == self_agent:
            continue  # self-triggers don't apply
        requires_by = _parse_tag_value(tags, "requires_action_by:")
        # Policy: if requires_action_by is absent, BOTH agents see it and
        # neither is required to act — skip to avoid duplicate work. A finding
        # that targets this agent must say so explicitly via the tag.
        #
        # : the second clause was `requires_by != self_agent`, exact
        # string equality, so a tag in the @env-qualified form the
        # cross-deployment convention RECOMMENDS (`zeta@ayoai-mind`) compared
        # unequal and the trigger was dropped here in silence. That is the
        # worst place in this family to lose one: this gate is what FILES the
        # Investigate goal on an `invalidates` severity, so a partner saying
        # "your assumption is dead" would never reach the agent at all.
        # The absent-tag clause is deliberately UNTOUCHED (guard-1807 — do not
        # delete a clause of a compound skip-guard; the excluded class here
        # fails the equality clause, not the None clause).
        # Behaviour-preserving on the live corpus, measured 2026-08-06 over
        # 9110 board records: 353 bare tags and 7 qualified, all 7 targeting
        # `omni@zds-mind` — a PEER deployment, correctly skipped both before
        # and after. The newly-admitted set on today's data is EMPTY; this only
        # changes what happens the first time someone writes the qualified form
        # for a LOCAL agent (guard-1562 — name what the change newly admits).
        if requires_by is None or not routing_tag_targets_agent(
                requires_by, self_agent, ENVIRONMENT_ID):
            continue
        severity = _parse_tag_value(tags, "severity:") or "informs"
        if severity not in ("invalidates", "constrains"):
            continue  # enables/informs don't require action
        # Missing id ⇒ malformed finding. Skip rather than act, because
        # _already_processed(None) always returns False and we would re-fire
        # on every scan, creating duplicate Investigate goals forever.
        if not rec.get("id"):
            continue
        if _already_processed(rec["id"]):
            continue
        # Cross-aspiration dedup (rb-942 / ): if the sweep
        # already filed `origin_signal=insight_trigger:<msg_id>`, do NOT
        # file our `board_post:<msg_id>` duplicate. aspirations.py
        # duplication-gate uses exact-string match on origin_signal and
        # would let the format-asymmetric duplicate through silently.
        if _already_filed_in_aspirations(rec["id"]):
            continue
        affected_goals = _parse_all_tag_values(tags, "affects:")
        out.append({
            "finding": rec,
            "severity": severity,
            "affects": affected_goals,
        })
    return out


def _run_py(script_name, *args, stdin_input=None):
    """Invoke a core/scripts/*.py script directly via sys.executable.

    Bypasses bash wrappers. The bash shims (board-post.sh,
    aspirations-add-goal.sh) are 2-line `exec python3 target.py "$@"` passes;
    routing through bash adds a _paths.sh sourcing step that trips CRLF/
    backslash issues on Windows Python→bash subprocess (guard-168 territory).
    Invoking target.py directly avoids the whole class. See rb-350 family.

    Non-zero returncodes print stderr to our own stderr so failures are
    visible during testing instead of buried in insight-actions.jsonl.
    """
    path = CORE_ROOT / "scripts" / script_name
    try:
        r = subprocess.run(
            [sys.executable, str(path)] + list(args),
            input=stdin_input,
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
        )
        if r.returncode != 0:
            # Make failures loud. Dry-run never calls this; in real fire we
            # want the stderr of any failing invocation to reach the operator.
            print("[insight-trigger-gate] {} exit={} stderr={}".format(
                script_name, r.returncode, (r.stderr or "").strip()[:400]),
                file=sys.stderr)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        print("[insight-trigger-gate] {} raised: {}".format(script_name, e),
              file=sys.stderr)
        return -1, "", str(e)


def _post_reply(finding_id, message, tags):
    """Reply on the findings board via board.py post (directly, no bash)."""
    tag_args = []
    if tags:
        tag_args = ["--tags", ",".join(tags)]
    rc, stdout, stderr = _run_py(
        "board.py", "post",
        "--channel", "findings",
        "--type", "status",
        "--reply-to", finding_id,
        *tag_args,
        stdin_input=message,
    )
    return rc == 0, (stdout or "") + (stderr or "")


def _log_action(record):
    log = _actions_log_path()
    if log is None:
        return False
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        locked_append_jsonl(str(log), record)
        return True
    except Exception as e:
        print("[insight-trigger-gate] WARN: action-log append failed: " + str(e),
              file=sys.stderr)
        return False


def probe_goal_status(goal_id):
    """Return current status of goal_id by scanning world + per-agent
    aspirations.jsonl. Returns the status string when found, None when
    the goal does not exist anywhere.

    g-115-1077 / rb-1150: closes the audit-time -> apply-time staleness
    gap on the event-driven gate path. Mirrors
    insight-trigger-sweep.probe_goal_status (g-115-1076) but routes
    through _world_dir() + _enumerate_agent_confs() since this script
    already uses that resolution path for findings reads.

    Reads JSONL directly rather than _rt.aspirations_read so the function
    is testable against a sandbox WORLD_PATH via monkeypatch — _rt would
    require a running daemon. Same direct-read pattern as
    _already_filed_in_aspirations above.
    """
    if not goal_id:
        return None
    paths = []
    world = _world_dir()
    if world is not None:
        paths.append(world / "aspirations.jsonl")
    for conf in _enumerate_agent_confs():
        paths.append(conf.parent / "aspirations.jsonl")
    for path in paths:
        try:
            if not path.is_file():
                continue
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    for g in rec.get("goals") or []:
                        if g.get("id") == goal_id:
                            return g.get("status")
        except Exception:
            continue
    return None


def _emit_audit_stale_note(finding, affected_goal, target_status, severity):
    """Post a coordination-board status note for a skipped Investigate.

    Mirrors insight-trigger-sweep._emit_audit_stale_note (g-115-1076) but
    references the gate path's vocabulary (Investigate spawn vs Apply
    spawn) and the gate's per-affected loop semantics. One short post per
    audit-stale finding, matching the acceptance criteria shape so future
    grepping picks them up.

    Fail-open: board.py errors are logged to stderr but do not abort the
    gate — the metric record (returned to summary) is the durable audit
    trail.
    """
    text = (
        "Audit-stale: insight_trigger {fid} from {author} targeted "
        "{aff} (severity={sev}) -- already {st}; Investigate spawn "
        "skipped (rb-1150 audit-time vs apply-time gap)."
    ).format(
        fid=finding.get("id"),
        author=finding.get("author"),
        aff=affected_goal,
        sev=severity,
        st=target_status,
    )
    tags = (
        "audit-stale,insight-trigger:{fid},target:{aff},target_status:{st}"
    ).format(fid=finding.get("id"), aff=affected_goal, st=target_status)
    try:
        proc = subprocess.run(
            [sys.executable, str(CORE_ROOT / "scripts" / "board.py"),
             "post", "--channel", "coordination", "--type", "status",
             "--tags", tags],
            input=text, capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
        return {"posted": proc.returncode == 0,
                "msg_id": proc.stdout.strip() if proc.returncode == 0 else None}
    except Exception as e:
        print("[insight-trigger-gate] WARN: audit-stale board-post failed: "
              + str(e), file=sys.stderr)
        return {"posted": False, "msg_id": None, "error": str(e)}


def _act_on_trigger(trigger, dry_run):
    """Create one Investigate goal per affected-goal-id in .

    Both invalidates and constrains map to the same action (file an
    Investigate goal): aspirations-update-goal.sh has no append semantic for
    arbitrary fields — `goal[field] = value` overwrites — so the earlier
    "insight_constraints_append" path was a silent overwrite bug. The single
    Investigate-goal path is the simplest correct behavior: the trigger text
    is preserved in the new goal's description, and goal-scoring surfaces it
    the same way any other HIGH goal gets picked up.

    Triggers with no `affects:` tag are skipped — logged but no action — so
    we don't fabricate goals pointed at a fake "unspecified" target.
    """
    finding = trigger["finding"]
    severity = trigger["severity"]
    affects = trigger["affects"]
    if not affects:
        return [{"kind": "skipped-no-affects",
                 "finding_id": finding.get("id"),
                 "severity": severity}]
    actions = []
    verb = "invalidates" if severity == "invalidates" else "constrains"
    for affected in affects:
        #  / rb-1150: re-probe target status at filing time.
        # Terminal -> skip + audit-stale note (avoid duplicate Investigate
        # for a goal that already resolved). Missing -> file with warning
        # (target may have been archived after the finding was authored).
        # Pending/in-progress/blocked -> file as-is (normal path).
        target_status = probe_goal_status(affected)
        if target_status in TERMINAL_GOAL_STATES:
            note_result = {"posted": False, "msg_id": None}
            if not dry_run:
                note_result = _emit_audit_stale_note(
                    finding, affected, target_status, severity)
            actions.append({
                "kind": "audit-stale-skip",
                "target": affected,
                "target_status": target_status,
                "note_result": note_result,
            })
            continue
        affects_missing_warning = None
        if target_status is None:
            # Target not found in any queue - file with warning per
            # acceptance criteria. The warning is surfaced in the action
            # record so main()'s summary aggregation can flag it.
            affects_missing_warning = (
                "affects target not found in any queue at filing time")
        title = "Investigate: insight_trigger {verb} {aff}".format(
            verb=verb, aff=affected)
        text = (finding.get("text") or "")[:500]
        desc = ("Auto-generated from insight_trigger (finding {fid} by {author}). "
                "Severity: {sev}. Affects: {aff}. Original finding:\n\n"
                "{text}").format(fid=finding.get("id"),
                                 author=finding.get("author"),
                                 sev=severity, aff=affected, text=text)
        if dry_run:
            actions.append({"kind": "dry-run-create-goal",
                            "target": affected, "title": title,
                            "affects_missing_warning": affects_missing_warning})
            continue
        goal_payload = {
            "title": title,
            "description": desc,
            "priority": "HIGH",
            "participants": ["agent"],
            # origin-signal-gate: the source insight finding id IS the signal.
            # Use board_post:<finding-id> so the audit trail connects the
            # auto-filed Investigate back to the findings board entry.
            "origin_signal": "board_post:{fid}".format(fid=finding.get("id") or "unknown"),
        }
        try:
            _rt.aspirations_add_goal("asp-001", goal_payload, source="agent")
            ok = True
            err_text = None
        except _rt.RtError as e:
            ok = False
            err_text = (e.body or str(e)).strip()[:200]
            print("[insight-trigger-gate] add-goal failed: {}".format(
                err_text), file=sys.stderr)
        actions.append({
            "kind": "create-investigate-goal",
            "target": affected,
            "asp_id": "asp-001",
            "ok": ok,
            "script_stderr": err_text if not ok else None,
            "affects_missing_warning": affects_missing_warning,
        })
    return actions


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Insight-Trigger Gate — bash-enforced insight_trigger follow-through.",
    )
    ap.add_argument("--since-hours", type=int, default=24,
                    help="Scan window in hours (default 24)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show planned actions without writing")
    ap.add_argument("--limit", type=int, default=10,
                    help="Max actions per invocation (default 10)")
    ap.add_argument("--output", default="json", choices=["json", "human"])
    args = ap.parse_args(argv)

    agent = os.environ.get("MIND_AGENT", "").strip()
    if not agent:
        print("[insight-trigger-gate] MIND_AGENT unset — skipping", file=sys.stderr)
        print(json.dumps({"actions_taken": [], "reason": "no MIND_AGENT"}))
        return 0

    findings, err = _load_findings(args.since_hours)
    if err:
        print("[insight-trigger-gate] " + err, file=sys.stderr)
        return 1

    triggers = _collect_triggers(findings, agent)
    if args.limit and len(triggers) > args.limit:
        deferred = len(triggers) - args.limit
        triggers = triggers[:args.limit]
    else:
        deferred = 0

    all_actions = []
    for trig in triggers:
        sev = trig["severity"]
        acts = _act_on_trigger(trig, args.dry_run)

        # Acknowledge + log — even in dry-run we skip the reply and log
        if not args.dry_run:
            reply_msg = ("Acknowledged insight_trigger ({sev}); "
                         "agent={agent} actions={n}").format(
                sev=sev, agent=agent, n=len(acts))
            reply_ok, _ = _post_reply(
                trig["finding"].get("id"),
                reply_msg,
                tags=["insight_ack", "severity:" + sev, agent],
            )
            _log_action({
                "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
                "agent": agent,
                "finding_id": trig["finding"].get("id"),
                "severity": sev,
                "affects": trig["affects"],
                "actions": acts,
                "reply_posted": reply_ok,
            })

        all_actions.append({
            "finding_id": trig["finding"].get("id"),
            "severity": sev,
            "affects": trig["affects"],
            "actions": acts,
        })

    #  / rb-1150: aggregate audit_stale and affects_missing
    # counters from per-trigger action records so callers can detect the
    # staleness-gap closures without re-walking the actions tree.
    audit_stale_count = 0
    affects_missing_count = 0
    audit_stale_details = []
    affects_missing_details = []
    for entry in all_actions:
        for act in entry.get("actions") or []:
            if act.get("kind") == "audit-stale-skip":
                audit_stale_count += 1
                audit_stale_details.append({
                    "finding_id": entry.get("finding_id"),
                    "target": act.get("target"),
                    "target_status": act.get("target_status"),
                })
            elif act.get("affects_missing_warning"):
                affects_missing_count += 1
                affects_missing_details.append({
                    "finding_id": entry.get("finding_id"),
                    "target": act.get("target"),
                    "warning": act.get("affects_missing_warning"),
                })

    result = {
        "scanned_since_hours": args.since_hours,
        "total_findings_seen": len(findings),
        "triggers_matched": len(triggers) + deferred,
        "triggers_acted_on": len(triggers),
        "deferred_over_limit": deferred,
        "audit_stale": audit_stale_count,
        "affects_missing": affects_missing_count,
        "audit_stale_details": audit_stale_details,
        "affects_missing_details": affects_missing_details,
        "dry_run": args.dry_run,
        "actions": all_actions,
    }

    if args.output == "json":
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print("scanned={n} hours | findings={f} | triggers={t} | acted={a} | deferred={d} | dry_run={dr}".format(
            n=args.since_hours, f=len(findings),
            t=len(triggers) + deferred, a=len(triggers),
            d=deferred, dr=args.dry_run))
        for a in all_actions:
            print("  trigger {fid}: severity={sev} affects={aff}".format(
                fid=a["finding_id"], sev=a["severity"], aff=a["affects"]))
            for act in a["actions"]:
                print("    action: " + json.dumps(act)[:160])

    # Dry-run with matches = exit 2 (signals "would act but didn't")
    if args.dry_run and all_actions:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
