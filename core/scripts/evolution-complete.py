#!/usr/bin/env python3
"""Phase b of D1 (3-phase write path) — LLM-driven completion of an evolution stub.

Reads a stub entry from one of the 4 event streams:
  - world/self-evolution.jsonl
  - world/program-evolution.jsonl
  - world/skill-evolution.jsonl
  - world/rule-evolution.jsonl

Updates its `reasoning`, `signal_source`, `signal_evidence` fields, and transitions
`status` from `awaiting_completion` to `final`.

CLI:
  evolution-complete.py --revision-id <id> \\
                        --reasoning "<≥80 char justification for material>" \\
                        --signal-source <sq-XXX | rb-XXX | guard-XXX | goal-XXX | ...> \\
                        --signal-evidence '[{"type":"...","id":"..."}, ...]'

Optional flags:
  --dry-run   Validate inputs without writing
  --json-out  Print the finalized entry to stdout as JSON

Validation rules (§5.2 / §5.3):
  - reasoning ≥ 80 chars when change_class == "material" (or "material-rename")
  - signal_source REQUIRED for material
  - signal_evidence MUST have ≥1 element for material
  - bootstrap / cosmetic entries: validation relaxed (signal_source optional, reasoning min 0)

Phase 2 scope: validation + status transition. Monitor registration (verification_monitor_id),
board post (board_post_id), and email notification (user_notify_ref) are stubs — those wire up
in Phase 4 (backpressure) and Phase 5 (board + notifications).

Per world/conventions/self-program-evolution.md
"""

import argparse
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from _runtime_bash import bash_cmd  # noqa: E402  # : Windows-safe bash resolution


_STREAMS = (
    "self-evolution.jsonl",
    "program-evolution.jsonl",
    "skill-evolution.jsonl",
    "rule-evolution.jsonl",
    "script-evolution.jsonl",
)


def resolve_world_dir():
    """Resolve WORLD_DIR via _paths.py (uses MIND_AGENT binding)."""
    try:
        from _paths import WORLD_DIR
        if WORLD_DIR:
            return Path(WORLD_DIR)
    except Exception:
        pass
    return None


def find_stub(world_dir, revision_id):
    """Scan the 4 event streams for an entry with matching revision_id.

    Returns (stream_path, line_idx, entry_dict) or (None, None, None).
    """
    for stream_name in _STREAMS:
        path = Path(world_dir) / stream_name
        if not path.exists():
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception:
            continue
        for idx, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("revision_id") == revision_id:
                return path, idx, entry
    return None, None, None


def validate(entry, reasoning, signal_source, signal_evidence):
    """Validate inputs against §5.3 required-fields matrix. Returns (ok, error_msg)."""
    change_class = entry.get("change_class", "")
    material = change_class in ("material", "material-rename")

    if material:
        if not reasoning or len(reasoning.strip()) < 80:
            return False, (
                f"reasoning must be ≥80 chars for change_class={change_class!r} "
                f"(got {len(reasoning.strip()) if reasoning else 0} chars). "
                "Explain WHY this specific change (vs alternatives)."
            )
        if not signal_source:
            return False, (
                f"--signal-source required for change_class={change_class!r}. "
                "Acceptable values: sq-XXX | rb-XXX | guard-XXX | g-XXX-XX | user-directive | "
                "fresh-eyes-review | felt-sense | encode-session"
            )
        if not signal_evidence or len(signal_evidence) < 1:
            return False, (
                f"--signal-evidence must contain ≥1 element for change_class={change_class!r}. "
                "Example: '[{\"type\":\"spark_question\",\"id\":\"sq-012\",\"outcome\":\"confirmed\"}]'"
            )

    # Bootstrap and cosmetic — light validation only
    if not reasoning:
        reasoning = ""

    return True, None


def rewrite_stream(stream_path, line_idx, new_entry):
    """Replace line line_idx in stream_path with new_entry (atomic via tmp+rename)."""
    from _fileops import acquire_lock, release_lock

    lock = stream_path.with_suffix(stream_path.suffix + ".lock")
    acquire_lock(lock)
    try:
        with open(stream_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        # Use ensure_ascii=False to preserve any unicode in reasoning
        lines[line_idx] = json.dumps(new_entry, ensure_ascii=False) + "\n"
        tmp = stream_path.with_suffix(stream_path.suffix + ".tmp-complete")
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(lines)
        os.replace(str(tmp), str(stream_path))
    finally:
        release_lock(lock)


def parse_evidence(s):
    """Parse signal_evidence argument as JSON list-of-objects.

    Accepts:
      - JSON array string: '[{"type":"...","id":"..."}]'
      - Empty / None for non-material
    """
    if not s:
        return []
    try:
        v = json.loads(s)
    except json.JSONDecodeError as e:
        raise SystemExit(f"ERROR: --signal-evidence is not valid JSON: {e}")
    if not isinstance(v, list):
        raise SystemExit("ERROR: --signal-evidence must be a JSON array")
    for item in v:
        if not isinstance(item, dict):
            raise SystemExit("ERROR: --signal-evidence must be array-of-objects")
    return v


_FILE_KIND_TO_MONITOR_KIND = {
    "agent_self": "self_evolution",
    "program": "program_evolution",
    "skill_edit": "skill_evolution",
    "rule_edit": "rule_evolution",
    # NOTE: script_edit intentionally omitted — no backpressure metric vector
    # is defined for raw .sh/.py/config files. Script changes are still recorded
    # in script-evolution.jsonl and posted to the decisions board; they just
    # don't carry an auto-rollback monitor. Add a "script_evolution" entry
    # (here + meta-backpressure.py + evolution-snapshot-metrics.py) if you
    # later define what "script regression" means metrically.
}

_FILE_KIND_TO_BOARD_TYPE = {
    "agent_self": "self-change",
    "program": "program-change",
    "skill_edit": "skill-change",
    "rule_edit": "rule-change",
    "script_edit": "script-change",
}


def _post_board_decision(entry):
    """Phase 5.1: post a decisions-channel entry for a material edit.

    Returns the board message ID on success, or None on any failure. Never
    raises — a board outage must not block finalization.
    """
    import subprocess
    file_kind = entry.get("file_kind", "")
    board_type = _FILE_KIND_TO_BOARD_TYPE.get(file_kind)
    if not board_type:
        print(f"WARN: cannot post board entry — unknown file_kind={file_kind!r}", file=sys.stderr)
        return None

    revision_id = entry.get("revision_id") or "?"
    file_path = entry.get("file_path") or "?"
    change_class = entry.get("change_class") or "?"
    agent = entry.get("agent") or os.environ.get("MIND_AGENT", "")
    reasoning = (entry.get("reasoning") or "").strip()
    reasoning_preview = reasoning[:300] + ("…" if len(reasoning) > 300 else "")
    signal_source = entry.get("signal_source") or "(none)"
    section_changed = entry.get("section_changed") or "(none)"

    body = (
        f"**{board_type}** — `{file_path}`\n\n"
        f"- revision: `{revision_id}`\n"
        f"- change_class: {change_class}\n"
        f"- section: {section_changed}\n"
        f"- signal_source: {signal_source}\n"
        f"- agent: {agent}\n\n"
        f"**Reasoning**: {reasoning_preview}"
    )

    tags = ",".join(t for t in (agent, Path(file_path).stem, revision_id) if t)

    # Call board.py directly via py -3, bypassing the bash wrapper (rb-370 /
    # guard-335 — the bash shim is a 2-line `exec python3` passthrough that
    # adds a Windows-path failure mode for no behavior change).
    bp_script = str(SCRIPT_DIR / "board.py")
    cmd = ["py", "-3", bp_script, "post", "--channel", "decisions",
           "--type", board_type, "--tags", tags]
    env = os.environ.copy()
    if agent:
        env["MIND_AGENT"] = agent
    try:
        result = subprocess.run(cmd, input=body, capture_output=True, text=True,
                                env=env, timeout=20)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        print(f"WARN: board-post subprocess failed: {exc}", file=sys.stderr)
        return None
    if result.returncode != 0:
        print(f"WARN: board-post returned {result.returncode}: {result.stderr.strip()}", file=sys.stderr)
        return None
    msg_id = result.stdout.strip().splitlines()[-1].strip() if result.stdout.strip() else None
    if not msg_id or not msg_id.startswith("msg-"):
        print(f"WARN: board-post stdout did not yield msg ID: {result.stdout!r}", file=sys.stderr)
        return None
    return msg_id


def _email_user(entry, event):
    """Phase 5.2: notify user about an evolution event (material Self edit or rollback).

    `event` ∈ {"material-self", "rollback"} — selects the subject/body template.
    Honors MIND_EVOLUTION_NOTIFY_DRYRUN=1 (skips subprocess, returns "dry-run:<id>").

    Returns a notify reference string on success, or None on failure / dry-run-disabled.
    Never raises — email transport outage must not block finalization.
    """
    import subprocess
    import json as _json
    file_kind = entry.get("file_kind", "")
    file_path = entry.get("file_path") or "?"
    revision_id = entry.get("revision_id") or "?"
    agent = entry.get("agent") or os.environ.get("MIND_AGENT", "")
    change_class = entry.get("change_class") or "?"
    reasoning = (entry.get("reasoning") or "").strip()
    signal_source = entry.get("signal_source") or "(none)"

    if event == "material-self":
        subject = f"Self edited: {file_path}"
        body = (
            f"{agent or '?'} autonomously edited {file_path}.\n\n"
            f"Change class: {change_class}\n"
            f"Section: {entry.get('section_changed') or '(none)'}\n"
            f"Signal source: {signal_source}\n"
            f"Revision: {revision_id}\n\n"
            f"Reasoning:\n{reasoning}\n\n"
            f"This is a post-change notification per guard-380 — no approval required. "
            f"If you disagree with this edit, you can roll it back via:\n"
            f"  bash core/scripts/history.py restore {file_path} <snapshot-name>"
        )
        info_type = "Self-Evolution"
    elif event == "rollback":
        subject = f"Auto-rollback: {file_kind} {file_path}"
        body = (
            f"Backpressure auto-rolled back a recent edit to {file_path}.\n\n"
            f"Original revision: {revision_id}\n"
            f"Reason: metric vector degraded — see entry.reasoning in {file_kind}-evolution.jsonl.\n\n"
            f"This is a post-rollback notification. The original edit is preserved in the event "
            f"stream for audit; the file has been restored to its pre-edit state from .history/."
        )
        info_type = "Self-Program-Evolution-Rollback"
    else:
        print(f"WARN: unknown notify event={event!r}", file=sys.stderr)
        return None

    if os.environ.get("MIND_EVOLUTION_NOTIFY_DRYRUN", "").strip() in ("1", "true", "yes"):
        print(f"NOTIFY DRY-RUN: would email about {event} ({subject})", file=sys.stderr)
        return f"dry-run:{event}:{revision_id}"

    payload = {
        "InfoType": info_type,
        "Title": subject,
        "InfoMessage": subject,
        "Body": body,
    }
    # email-send.sh lives at world/scripts/email-send.sh in the externally-mounted
    # world dir. Resolve via _paths.WORLD_DIR to honor MIND_WORLD env override.
    from _paths import WORLD_DIR
    email_script = (WORLD_DIR / "scripts" / "email-send.sh").as_posix()
    env = os.environ.copy()
    if agent:
        env["MIND_AGENT"] = agent
    try:
        result = subprocess.run(
            bash_cmd(email_script),
            input=_json.dumps(payload), capture_output=True, text=True,
            env=env, timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        print(f"WARN: email-send subprocess failed: {exc}", file=sys.stderr)
        return None
    if result.returncode != 0:
        print(f"WARN: email-send returned {result.returncode}: {result.stderr.strip()}", file=sys.stderr)
        return None
    return f"email:{event}:{revision_id}"


def _propose_program_change(entry, reasoning):
    """Phase 6.3 (D2): drive the cross-agent ack flow for a program edit.

    Returns the JSON dict from program-change-propose.py on success
    (keys: partners, proposal_path, board_post_id, ack_goal_ids,
    single_agent_world), or None on subprocess failure. Never raises —
    proposal-flow failure must not block finalization (caller falls back).
    """
    import subprocess
    revision_id = entry.get("revision_id") or ""
    file_path = entry.get("file_path") or ""
    history_snapshot = entry.get("history_snapshot") or ""
    agent = entry.get("agent") or os.environ.get("MIND_AGENT", "")
    if not (revision_id and file_path and history_snapshot):
        print("WARN: program-change-propose missing required entry fields",
              file=sys.stderr)
        return None

    reason = (reasoning or "(no reason provided)").strip().splitlines()[0][:200]
    propose_script = str(SCRIPT_DIR / "program-change-propose.py")
    cmd = ["py", "-3", propose_script,
           "--revision-id", revision_id,
           "--file-path", file_path,
           "--history-snapshot", history_snapshot,
           "--reason", reason]
    if agent:
        cmd += ["--agent", agent]
    env = os.environ.copy()
    if agent:
        env["MIND_AGENT"] = agent
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=60)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        print(f"WARN: program-change-propose subprocess failed: {exc}", file=sys.stderr)
        return None
    if result.returncode != 0:
        print(f"WARN: program-change-propose returned {result.returncode}: {result.stderr.strip()}",
              file=sys.stderr)
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        print(f"WARN: program-change-propose JSON decode failed: {exc}", file=sys.stderr)
        return None


def _register_monitor(entry):
    """Phase 4.4 (D4): register a multi-metric backpressure monitor for this edit.

    Returns the monitor's revision_id on success, or None on any failure. Never
    raises — a backpressure infra glitch must not block finalization. Errors
    are logged to stderr for diagnosis.
    """
    import subprocess
    file_kind = entry.get("file_kind", "")
    monitor_kind = _FILE_KIND_TO_MONITOR_KIND.get(file_kind)
    if not monitor_kind:
        # Distinguish "intentionally no monitor" from "unknown kind". Any
        # file_kind that's a known board type but not in the monitor map is
        # deliberate (currently: script_edit). Silently skip those — they
        # already have a stream record + board post. Genuinely-unknown kinds
        # still WARN so a missed wiring shows up in stderr review.
        if file_kind not in _FILE_KIND_TO_BOARD_TYPE:
            print(f"WARN: cannot register monitor — unknown file_kind={file_kind!r}", file=sys.stderr)
        return None

    revision_id = entry.get("revision_id")
    file_path = entry.get("file_path") or ""
    agent = entry.get("agent") or os.environ.get("MIND_AGENT", "")
    history_snapshot = entry.get("history_snapshot")
    if not (revision_id and file_path and history_snapshot):
        print(
            f"WARN: cannot register monitor — missing required field (revision_id={revision_id!r}, "
            f"file_path={file_path!r}, history_snapshot={history_snapshot!r})",
            file=sys.stderr,
        )
        return None

    # Sample baseline vector
    snap_script = SCRIPT_DIR / "evolution-snapshot-metrics.py"
    snap_cmd = ["py", "-3", str(snap_script), "--file-kind", file_kind]
    if file_kind in ("skill_edit", "rule_edit"):
        snap_cmd += ["--file-path", file_path]
    snap_env = os.environ.copy()
    if agent:
        snap_env["MIND_AGENT"] = agent
    try:
        snap_result = subprocess.run(snap_cmd, capture_output=True, text=True,
                                     env=snap_env, timeout=30)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        print(f"WARN: baseline snapshot subprocess failed: {exc}", file=sys.stderr)
        return None
    if snap_result.returncode == 64:
        # F3: evolution-snapshot-metrics is an INTENTIONAL unimplemented
        # placeholder (sentinel rc=64; the 15/21/10/9 signal registry was
        # never specced — fabricating it would feed a count-sensitive
        # auto-rollback combinator). Known disabled state, not an error:
        # one clean line, skip monitor registration quietly.
        print("INFO: evolution backpressure monitor not registered — "
              "snapshot-metrics intentionally unimplemented (F3 design goal "
              "pending). Expected state, not a failure.", file=sys.stderr)
        return None
    if snap_result.returncode != 0:
        print(f"WARN: baseline snapshot returned {snap_result.returncode}: {snap_result.stderr.strip()}",
              file=sys.stderr)
        return None

    # Register monitor
    bp_script = SCRIPT_DIR / "meta-backpressure.py"
    bp_cmd = [
        "py", "-3", str(bp_script), "evolution-monitor",
        "--monitor-kind", monitor_kind,
        "--revision-id", revision_id,
        "--file-path", file_path,
        "--history-snapshot", history_snapshot,
        "--baseline-vector", snap_result.stdout.strip(),
    ]
    if agent:
        bp_cmd += ["--agent", agent]
    try:
        bp_result = subprocess.run(bp_cmd, capture_output=True, text=True,
                                   env=snap_env, timeout=30)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        print(f"WARN: monitor registration subprocess failed: {exc}", file=sys.stderr)
        return None
    if bp_result.returncode != 0:
        print(f"WARN: monitor registration returned {bp_result.returncode}: {bp_result.stderr.strip()}",
              file=sys.stderr)
        return None

    return revision_id


def main():
    parser = argparse.ArgumentParser(
        description="Complete an awaiting_completion evolution stub entry (Phase b of D1)"
    )
    parser.add_argument("--revision-id", required=True, help="The stub revision_id to complete")
    parser.add_argument("--reasoning", default="", help="Why this specific change (≥80 chars for material)")
    parser.add_argument("--signal-source", default="", help="Origin signal: sq-XXX | rb-XXX | guard-XXX | g-XXX-XX | user-directive | fresh-eyes-* | felt-sense | encode-session")
    parser.add_argument("--signal-evidence", default="[]", help='JSON array: \'[{"type":"...","id":"..."}]\'')
    parser.add_argument("--dry-run", action="store_true", help="Validate without writing")
    parser.add_argument("--json-out", action="store_true", help="Print finalized entry to stdout as JSON")
    args = parser.parse_args()

    world_dir = resolve_world_dir()
    if not world_dir:
        print("ERROR: cannot resolve WORLD_DIR (is MIND_AGENT set with valid local-paths.conf?)", file=sys.stderr)
        return 2

    stream_path, line_idx, entry = find_stub(world_dir, args.revision_id)
    if not entry:
        print(f"ERROR: no entry found with revision_id={args.revision_id!r} across 4 event streams in {world_dir}", file=sys.stderr)
        return 3

    if entry.get("status") == "final":
        print(f"ERROR: entry {args.revision_id} is already final (no-op)", file=sys.stderr)
        return 4

    if entry.get("status") != "awaiting_completion":
        print(f"ERROR: entry {args.revision_id} has unexpected status={entry.get('status')!r} (expected awaiting_completion)", file=sys.stderr)
        return 5

    # F1 (session attribution): non-blocking WARN when the session completing
    # this stub differs from the one that created it. Cross-session completion
    # is sometimes legitimate (autocompact recovery, /stop consolidation, an
    # observer finalizing a loop stub), so this surfaces the mismatch for audit
    # rather than blocking it. Both IDs must be non-empty to compare — a stub
    # predating by_session_id (legacy) or a CLI invocation without MIND_SID
    # injected simply skips the check. by_session_id is written by
    # evolution-prepare.py / carried into the stub by evolution-record.py.
    stub_sid = (entry.get("by_session_id") or "").strip()
    cur_sid = (os.environ.get("MIND_SID") or "").strip()
    if stub_sid and cur_sid and stub_sid != cur_sid:
        print(
            f"WARN: session-attribution mismatch — stub {args.revision_id} was "
            f"created by session {stub_sid} but is being completed by session "
            f"{cur_sid}. Legitimate for cross-session recovery; suspicious if an "
            f"observer is finalizing the autonomous loop's stub (or vice versa). "
            f"Confirm the reasoning reflects the ACTUAL change made by the "
            f"creating session, not a post-hoc guess.",
            file=sys.stderr,
        )

    evidence = parse_evidence(args.signal_evidence)

    ok, err = validate(entry, args.reasoning, args.signal_source, evidence)
    if not ok:
        print(f"VALIDATION FAILED: {err}", file=sys.stderr)
        return 6

    # Apply completion
    new_entry = dict(entry)
    new_entry["reasoning"] = args.reasoning.strip() if args.reasoning else None
    new_entry["signal_source"] = args.signal_source or None
    new_entry["signal_evidence"] = evidence
    new_entry["status"] = "final"

    # Phase 6.3 wiring (D2): program edits route to the cross-agent ack flow
    # instead of immediate finalize. The agent's edit is already on disk; the
    # ack flow tracks consensus and rolls back via existing infra if any partner
    # rejects. Single-agent worlds (no active partners) fall back to immediate
    # finalize per §8.7 Q-D2 edge case.
    change_class = entry.get("change_class", "")
    program_ack_initiated = False
    if (entry.get("file_kind") == "program" and change_class == "material"
            and not args.dry_run):
        propose_outcome = _propose_program_change(new_entry, args.reasoning)
        if propose_outcome is None:
            # Subprocess error — leave entry status=final and continue; the
            # next program-ack-sweep will detect any missing proposal artifacts.
            print("WARN: program-change-propose failed; finalizing without ack flow", file=sys.stderr)
        elif propose_outcome.get("single_agent_world"):
            # No partners → Self protocol (immediate finalize)
            print("NOTE: single-agent world detected; finalizing program edit without ack flow",
                  file=sys.stderr)
        else:
            # Multi-agent world → mark awaiting_acks
            new_entry["status"] = "awaiting_acks"
            new_entry["pending_acks"] = propose_outcome["partners"]
            new_entry["proposal_diff_path"] = propose_outcome["proposal_path"]
            new_entry["ack_goal_ids"] = propose_outcome["ack_goal_ids"]
            new_entry["board_post_id"] = propose_outcome.get("board_post_id")
            program_ack_initiated = True

    # Phase 4 wiring (D4): register backpressure monitor for material edits.
    # Cosmetic, bootstrap, and material-rename without diff content are skipped —
    # the rationale per §14.10 is that monitors track regression of behavior, and
    # behavior only changes on a material body diff. Program edits in the
    # awaiting_acks state don't register a monitor yet — the monitor activates
    # on transition to final (handled by program-ack-sweep when quorum is met).
    if change_class == "material" and not args.dry_run and not program_ack_initiated:
        new_entry["verification_monitor_id"] = _register_monitor(new_entry)

    # Phase 5.1 + 5.2: board post + email notification for material edits.
    # Board post fires for every material file_kind (decisions-channel visibility),
    # EXCEPT program when ack-flow already posted the proposal (avoids duplicate).
    # Email fires ONLY for agent_self material per guard-380 / §14.9 (skill/rule
    # edits are too high-velocity to email; user opts in via board read).
    if change_class == "material" and not args.dry_run and not program_ack_initiated:
        new_entry["board_post_id"] = _post_board_decision(new_entry)
        if entry.get("file_kind") == "agent_self":
            ref = _email_user(new_entry, event="material-self")
            new_entry["user_notify_ref"] = ref
            # `user_notified` had been a dead field — initialized False and
            # never set True anywhere — so it read false on every material-self
            # edit even when user_notify_ref showed the email fired. Derive it
            # from the send result (single source of truth): True only on a real
            # send ("email:..."), not on dry-run ("dry-run:...") or failure (None).
            new_entry["user_notified"] = bool(ref) and ref.startswith("email:")

    if args.dry_run:
        print(f"DRY RUN — would finalize {args.revision_id} in {stream_path}")
        if args.json_out:
            print(json.dumps(new_entry, indent=2))
        return 0

    rewrite_stream(stream_path, line_idx, new_entry)
    print(f"FINALIZED {args.revision_id} in {stream_path.name} (change_class={entry.get('change_class')})")
    if args.json_out:
        print(json.dumps(new_entry, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
