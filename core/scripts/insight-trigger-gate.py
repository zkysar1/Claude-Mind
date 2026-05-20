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
                    try:
                        rec_time = dt.datetime.fromisoformat(ts.replace("Z", ""))
                        if rec_time < cutoff:
                            continue
                    except Exception:
                        pass
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
        if requires_by is None or requires_by != self_agent:
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
                            "target": affected, "title": title})
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

    result = {
        "scanned_since_hours": args.since_hours,
        "total_findings_seen": len(findings),
        "triggers_matched": len(triggers) + deferred,
        "triggers_acted_on": len(triggers),
        "deferred_over_limit": deferred,
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
