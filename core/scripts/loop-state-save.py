#!/usr/bin/env python3
"""Single-writer wrapper for <agent>/session/iteration-checkpoint.json ().

Replaces ad-hoc writes scattered across aspirations-select Phase 2.95 (init),
iteration-close.sh _checkpoint_refresh (per-phase update), and any future call
site that needs to anchor the in-flight goal across autocompact. Centralising
ensures: typed-key validation, atomic tempfile+rename, stderr WARN on unknown
keys, and a single place to evolve the schema.

rb-428 lineage: same family as the bash-consolidation pattern that
g-240-74 / g-240-27 extended. Each prior commit hardened a different write
path; this one hardens the iteration-checkpoint write surface.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

# : force utf-8 on stdin/stdout/stderr (covers Windows cp1252 fallback
# when callers bypass the _platform.sh PYTHONIOENCODING=utf-8 shim).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _stdio import reconfigure_stdio  # noqa: E402
from _paths import agent_dir as _paths_agent_dir  # noqa: E402
from _paths import body_state_path as _body_state_path  # noqa: E402
reconfigure_stdio()

# Schema — the typed-keys list. Adding a key here is the ONLY way to make it
# a valid checkpoint field. Unknown keys passed via --set / init JSON trigger
# a stderr WARN and non-zero exit.
SCHEMA = {
    # Required at init time:
    # Match the canonical ID regexes in aspirations.py (ASP_ID_RE / GOAL_ID_RE,
    # ~L211-212) — this SCHEMA duplicates them and MUST stay in sync (SSOT).
    # B13: the old goal_id `[a-z]?` (appended, no hyphen) rejected the
    # decomposition-child form g-NNN-NN-a (/decompose, decompose/SKILL.md:147),
    # so checkpoint init aborted on the WARN (~line 136) for any decomposed child.
    # : the xw branch (g-xw-<ts>-NN / asp-xw-<ts> cross-world ids) was
    # widened in canonical () but never mirrored here — so loop-state-save
    # init/update rejected every cross-world goal's checkpoint. This re-syncs both.
    "goal_id":          {"required": True,  "type": str, "pattern": r"^g-(\d{3}-\d{2,4}(-[a-z])?|xw-\d{8}T\d{6}-\d{2})$"},
    "aspiration_id":    {"required": True,  "type": str, "pattern": r"^asp-(\d{3}|xw-\d{8}T\d{6})$"},
    # WORLD_AGENT_ONLY: cross-agent goals reach here already translated to
    # source='agent' + cross_agent_owner (see that field's comment below).
    "source":           {"required": True,  "type": str, "enum": ("world", "agent")},
    "phase":            {"required": True,  "type": str},  # selected, executed, verified, ...
    "selected_at":      {"required": True,  "type": str},  # ISO 8601
    # Optional at init, may be updated:
    "selector_score":   {"required": False, "type": (int, float)},
    "skill":            {"required": False, "type": str},
    "phase_completed":  {"required": False, "type": str},
    "last_updated":     {"required": False, "type": str},
    # Compact-recovery fields:
    "phase_progress":   {"required": False, "type": dict},
    "obligations_remaining": {"required": False, "type": list},
    # Outcome class — written by iteration-close.sh state-update; read by
    # obligation-audit.py + abbreviated-obligation-audit.py to decide which
    # obligations are required on this iteration. bravo FE-001 (2026-04-24):
    # without this key the reader side always saw null → the abbreviated-
    # obligation gate fell through with "claim says routine but checkpoint
    # says None" and never caught drift. SCHEMA now owns the key; writer
    # wired below in iteration-close.sh.
    "outcome_class":    {"required": False, "type": str, "enum": ("deep", "routine")},
    #  ordered-write intent marker. do_verify writes intent_state=complete
    # BEFORE any state mutation, transitions to committed after status/outcome/
    # in_flight all land. Recovery (iteration-close.sh --phase recover) detects
    # intent_state=complete + aspirations.status=pending and writes
    # intent_state=rolled_back so the next iteration's verify can re-execute
    # without tripping recover again. intent_state=rolled_back preserves the
    # audit trail (vs. silently deleting the field). intent_outcome mirrors
    # --outcome so the recovery path knows what was attempted.
    "intent_state":     {"required": False, "type": str, "enum": ("complete", "committed", "rolled_back")},
    "intent_outcome":   {"required": False, "type": str, "enum": ("deep", "routine")},
    #  Option 3 — cross-agent execution owner. When set, downstream
    # aspirations-*.sh / iteration-close.sh / recurring-close.sh subprocess
    # calls are env-prefixed MIND_AGENT=<owner> so writes route to the
    # sibling agent's queues + session state. The selector's
    # collect_cross_agent_candidates emits source='cross-agent:<sib>' for
    # cross-pulled goals; aspirations-select Phase 2.95 translates that to
    # source='agent' + cross_agent_owner='<sib>'. Pattern restricts to
    # lowercase agent-dir names — defensive, since this field controls
    # subprocess env injection. Keep `source` enum strict (world/agent only) —
    # this field is the cross-agent escape hatch.
    "cross_agent_owner": {"required": False, "type": str, "pattern": r"^[a-z][a-z0-9_-]*$"},
}

REQUIRED_INIT_KEYS = [k for k, v in SCHEMA.items() if v["required"]]


def _agent_dir() -> Path:
    """Resolve $MIND_AGENT to its session directory. Fails loud if unset —
    the bash hook injects this on every Bash call, so a missing env var means
    we're running outside the bound-agent context.

    The _paths.agent_dir helper is imported under a renamed alias so this
    local def does not shadow it (the import-shadow recursion was the
    fe56cbc2 regression caught by fresh-eyes-code F1 on 2026-05-19)."""
    agent = os.environ.get("MIND_AGENT")
    if not agent:
        print("ERROR: MIND_AGENT not set — refusing to guess session path", file=sys.stderr)
        sys.exit(2)
    return _paths_agent_dir(agent)


def _checkpoint_path() -> Path:
    """Body-keyed in-flight breadcrumb, agent-wide when unbodied ().

    aspirations-claim.sh pipes an anchor into `init` on EVERY successful
    world-goal claim, and CLAIM is a WORKER_PHASE — so without body-keying a
    compliant worker body rewrites the reducer's breadcrumb on every claim,
    and postcompact-restore then points the reducer at the worker's goal.

    `.name` of agents/<name> IS the agent name (see _paths.agent_dir); taking
    it from _agent_dir keeps that function the single env-read + fail-loud
    point rather than adding a second parallel accessor.
    """
    return _body_state_path(_agent_dir().name, "iteration-checkpoint.json")


def _validate_keys(payload: dict, mode: str) -> list[str]:
    """Return list of WARN messages. Empty = clean. Caller decides whether to
    fail on warns based on `mode` ('init' fails loud, 'update' allows partials).

    Dotted keys (e.g. 'phase_progress.q1_passed') are valid when the top-level
    component is a SCHEMA key whose type includes dict — sub-keys within those
    dicts are free-form, mirroring jq dotted-path semantics. Atomic-merge for
    aspirations-verify Q1/Q2/Q3 phase_progress writes routes through this."""
    warns: list[str] = []
    for key in payload:
        top_key = key.split(".", 1)[0]
        if top_key not in SCHEMA:
            warns.append(f"unknown key: {key!r}")
            continue
        spec = SCHEMA[top_key]
        # Dotted key — only validate that top-level allows dict; skip leaf checks.
        if "." in key:
            t = spec["type"]
            ok = (t is dict) or (isinstance(t, tuple) and dict in t)
            if not ok:
                warns.append(f"{key!r}: dotted assignment requires dict-typed parent {top_key!r}, got {t}")
            continue
        val = payload[key]
        # Type check (top-level only)
        expected_t = spec["type"]
        if not isinstance(val, expected_t):
            warns.append(f"{key!r}: expected {expected_t}, got {type(val).__name__}")
            continue
        # Enum check
        if "enum" in spec and val not in spec["enum"]:
            warns.append(f"{key!r}: value {val!r} not in enum {spec['enum']}")
        # Pattern check (string only)
        if "pattern" in spec and isinstance(val, str):
            import re
            if not re.match(spec["pattern"], val):
                warns.append(f"{key!r}: value {val!r} does not match pattern {spec['pattern']!r}")
    if mode == "init":
        for req in REQUIRED_INIT_KEYS:
            if req not in payload:
                warns.append(f"required key missing at init: {req!r}")
    return warns


def _infer_aspiration_id(goal_id: str):
    """Derive the owning aspiration id from a goal id by PATTERN, with no I/O.

    Both goal-id shapes carry their aspiration in the prefix, so this needs no
    store read (and therefore cannot fail because a store is unreachable):
        g-115-3704              -> asp-115
        g-115-3704-a            -> asp-115
        g-xw-20260830T041617-01 -> asp-xw-20260830T041617
    Returns None when the id matches neither shape -- the caller then omits the
    key and lets the normal SCHEMA check report it, rather than inventing an
    aspiration that does not exist."""
    import re
    m = re.match(r"^g-(\d{3})-\d{2,4}(?:-[a-z])?$", goal_id)
    if m:
        return "asp-" + m.group(1)
    m = re.match(r"^g-(xw-\d{8}T\d{6})-\d{2}$", goal_id)
    if m:
        return "asp-" + m.group(1)
    return None


def _payload_from_goal_id(goal_id: str, args) -> dict:
    """Build a complete init payload from just a goal id ().

    Every required key is either derived from the id or defaulted, so the
    remedy `_warn_checkpoint_missing` prints is runnable AS PRINTED -- which
    it was not while the only form demanded a hand-built object carrying all
    five (g-115-3704). `selected_at` is the RE-ANCHOR time, not the original
    selection time: this path exists precisely because the original anchor is
    gone, so there is nothing on disk to recover the earlier stamp from, and
    claiming otherwise would be worse than being explicit."""
    from datetime import datetime
    payload = {
        "goal_id": goal_id,
        "source": getattr(args, "source", None) or "world",
        "phase": getattr(args, "phase", None) or "selected",
        "selected_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }
    asp = _infer_aspiration_id(goal_id)
    if asp:
        payload["aspiration_id"] = asp
    return payload


def _set_dotted(data: dict, path: str, value) -> None:
    """Mutate `data` so the dotted path resolves to `value`, creating intermediate
    dicts as needed. Mirrors jq's `.a.b.c = x` semantics."""
    parts = path.split(".")
    d = data
    for p in parts[:-1]:
        if p not in d or not isinstance(d.get(p), dict):
            d[p] = {}
        d = d[p]
    d[parts[-1]] = value


def _atomic_write(path: Path, data: dict) -> None:
    """tempfile+rename in same dir — POSIX semantics guarantee no torn writes
    on either rename or the subsequent reader. Same pattern as background-jobs,
    pending-agents, reasoning-snapshot, context-budget-status."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".iteration-checkpoint.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _read_optional_stdin(timeout_s=None):
    """Read OPTIONAL stdin without blocking forever on a non-EOF pipe (guard-664).

    `isatty()` distinguishes a terminal from a non-terminal but does NOT
    guarantee EOF: a non-tty stdin can be an inherited pipe that never closes,
    so a bare `sys.stdin.read()` blocks indefinitely. Measured on THIS script
    (g-115-3661): a bare `loop-state-save.sh init` sat 11 minutes at 0% CPU,
    wchan `unix_stream_data_wait`, and wrote no checkpoint -- while the WARN
    emitted on every skipped Phase 2.95 prescribed that very command, so the
    remediation advice was itself the trap.

    Reading in a daemon thread with a join() deadline degrades a non-EOF stdin
    to "" instead of hanging; when stdin IS piped, EOF arrives in << timeout_s
    and the read completes normally. select()/signal.alarm do NOT work on
    Windows pipes; a daemon thread does. Canonical reference:
    experience.py::_read_optional_stdin. Tunable via
    LOOP_STATE_SAVE_STDIN_TIMEOUT_S (default 10s) -- far above any real piped
    delivery (EOF in ms), far below the observed hang.

    Both call sites take OPTIONAL stdin (init has --json, update has --set),
    which is exactly the class guard-664 covers; MANDATORY stdin is exempt.
    """
    if sys.stdin is None or sys.stdin.isatty():
        return ""
    if timeout_s is None:
        timeout_s = float(os.environ.get("LOOP_STATE_SAVE_STDIN_TIMEOUT_S", "10"))
    import threading
    box = {"data": "", "done": False}

    def _reader():
        try:
            box["data"] = sys.stdin.read()
        except Exception:
            pass
        finally:
            box["done"] = True

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    t.join(timeout_s)
    if not box["done"]:
        sys.stderr.write(
            f"WARN: stdin did not reach EOF within {timeout_s:.0f}s -- proceeding "
            f"without stdin JSON. The caller likely invoked the wrapper bare (no "
            f"`echo '...' |` pipe and no `</dev/null`). Use a non-blocking form "
            f"instead -- `loop-state-save.sh init --json '{{...}}'` or "
            f"`loop-state-save.sh update --set key=value` -- since this warning "
            f"is emitted for BOTH subcommands (guard-664, g-115-3661).\n"
        )
        return ""
    return box["data"]


def cmd_init(args) -> int:
    """Write the initial checkpoint. Reads JSON payload from stdin OR from
    the --json flag. Required keys per SCHEMA must be present. Existing
    checkpoint is overwritten (selecting a new goal supersedes any prior anchor)."""
    if getattr(args, "goal_id", None):
        # Self-sufficient form: infer the whole payload from the goal id, then
        # let an explicit --json override any inferred field (explicit beats
        # inferred). Deliberately checked BEFORE --json so the two compose.
        payload = _payload_from_goal_id(args.goal_id, args)
        if args.json:
            payload.update(json.loads(args.json))
    elif args.json:
        payload = json.loads(args.json)
    elif not sys.stdin.isatty():
        raw = _read_optional_stdin().strip()
        if not raw:
            print("ERROR: init requires JSON on stdin or via --json", file=sys.stderr)
            return 2
        payload = json.loads(raw)
    else:
        print("ERROR: init requires JSON on stdin or via --json", file=sys.stderr)
        return 2

    if not isinstance(payload, dict):
        print("ERROR: payload must be a JSON object", file=sys.stderr)
        return 2

    # An unknown key is a TYPO; a missing anchor is an OUTAGE. Refusing the
    # whole write over a stray key traded the cheap failure for the expensive
    # one: every downstream reader then degrades silently for the REST of the
    # session (that is what _warn_checkpoint_missing exists to catch), and the
    # rc=1 was announced with the word "WARN" -- so the message told the caller
    # the opposite of what happened. Unknown keys are now DROPPED and warned
    # (non-fatal, which is what "WARN" promises); everything else still refuses
    # and SAYS it refused. Known keys stay fully validated either way.
    #
    # Dropping happens BEFORE validation on purpose: every warn _validate_keys
    # then returns is a genuine schema violation by construction, so nothing
    # here has to classify a message by its text ().
    unknown = [k for k in payload if k.split(".", 1)[0] not in SCHEMA]
    if unknown:
        for k in unknown:
            print(
                f"WARN[loop-state-save:init] unknown key {k!r} -- DROPPED; "
                f"checkpoint still written, known keys unaffected",
                file=sys.stderr,
            )
        payload = {k: v for k, v in payload.items() if k not in unknown}

    warns = _validate_keys(payload, mode="init")
    if warns:
        for w in warns:
            print(f"REFUSED[loop-state-save:init] {w} -- wrote NOTHING, "
                  f"checkpoint unchanged", file=sys.stderr)
        return 1

    _atomic_write(_checkpoint_path(), payload)
    print(f"iteration-checkpoint anchored: {payload.get('goal_id', '?')} phase={payload.get('phase', '?')}")
    return 0


def _warn_checkpoint_missing(path, args) -> None:
    """Surface a write against an absent checkpoint. NEVER raises, never blocks.

    Two channels, deliberately, per guard-772: a stderr banner is invisible when
    the caller runs inside a backgrounded subprocess -- which is how much of the
    loop executes -- so the durable JSONL half is what makes the miss auditable
    after the fact. Single-line JSON under PIPE_BUF in O_APPEND mode is
    single-write atomic; this is observability-grade, not durable-state-grade.
    """
    # Hoisted out of the try below: the two channels are independent, and a
    # stderr failure must not blind the durable one via NameError.
    keys = []
    try:
        keys = [p.split("=", 1)[0] for p in (getattr(args, "set", None) or []) if "=" in p]
    except Exception:
        pass
    try:
        print(
            "[loop-state-save] WARN: update against a MISSING iteration-checkpoint "
            "(%s) -- wrote nothing, returning 0 for fail-open. Attempted key(s): %s. "
            "Cause is almost always a skipped Phase 2.95 (aspirations-select creates "
            "the checkpoint; only iteration-close deletes it), which leaves it absent "
            "for the REST of the session and silently degrades every downstream "
            "reader. Re-anchor with the SELF-SUFFICIENT form, which needs no "
            "payload at all: `loop-state-save.sh init --goal-id <g-NNN-NN>` -- "
            "it infers aspiration_id from the id, phase=selected and "
            "selected_at=now (add `--source agent` for an agent-queue goal, "
            "`--phase X` to override). The explicit form "
            "`loop-state-save.sh init --json '{...}'` still works and must "
            "carry ALL required keys (%s). Do NOT use a bare "
            "`loop-state-save.sh init`: with no "
            "stdin it exits 2, and with an inherited never-EOF stdin it BLOCKED "
            "FOREVER until g-115-3661 (it now degrades after "
            "LOOP_STATE_SAVE_STDIN_TIMEOUT_S, default 10s, and still exits 2). "
            "A partial object REFUSES once per missing key and writes nothing "
            "(g-115-3454); an unknown key is warned and dropped, and the "
            "checkpoint is still written (g-115-3704)."
            % (path, ", ".join(keys) or "<none>", ", ".join(REQUIRED_INIT_KEYS)),
            file=sys.stderr,
        )
    except Exception:
        pass
    try:
        from datetime import datetime
        ledger = Path(path).parent / "checkpoint-miss.jsonl"
        rec = {
            "at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "agent": os.environ.get("MIND_AGENT", "unknown"),
            "event": "update_against_missing_checkpoint",
            "checkpoint_path": str(path),
            "attempted_keys": keys,
        }
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with open(ledger, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=True) + "\n")
    except Exception:
        pass  # the ledger must never be the outage


def _warn_checkpoint_wrong_goal(path, args, have: str, want: str) -> None:
    """Surface a refresh aimed at a DIFFERENT goal than the checkpoint anchors.

    Same two-channel design as _warn_checkpoint_missing (guard-772: a stderr
    banner is invisible from a backgrounded subprocess, so the durable JSONL
    half is what makes the refusal auditable), and deliberately the SAME ledger
    file -- one place to look, distinguished by `event`.

    REFUSING is the conservative half of the g-357-84 contract. Replacing the
    record instead would fabricate a Phase-2.95 anchor that never existed --
    the same "invent state rather than surface the miss" step the
    missing-checkpoint branch already declines to take.
    """
    keys = []
    try:
        keys = [q.split("=", 1)[0] for q in (getattr(args, "set", None) or []) if "=" in q]
    except Exception:
        pass
    try:
        print(
            "[loop-state-save] WARN: update REFUSED -- checkpoint at %s is anchored "
            "to %s but the caller named %s. Wrote nothing, returning 0 for fail-open. "
            "Attempted key(s): %s. This is the g-357-84 wrong-goal refresh: when no "
            "Phase 2.95 anchor exists for the goal being closed, a stale checkpoint "
            "for a DIFFERENT goal is the only record on disk, and an unguarded update "
            "stamps phase_completed/last_updated onto it -- making that other goal "
            "look freshly advanced to every downstream reader. Re-anchor the goal "
            "actually being closed: `loop-state-save.sh init --goal-id %s`."
            % (path, have or "<none>", want, ", ".join(keys) or "<none>", want),
            file=sys.stderr,
        )
    except Exception:
        pass
    try:
        from datetime import datetime
        ledger = Path(path).parent / "checkpoint-miss.jsonl"
        rec = {
            "at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "agent": os.environ.get("MIND_AGENT", "unknown"),
            "event": "update_against_wrong_goal",
            "checkpoint_path": str(path),
            "anchored_goal_id": have,
            "requested_goal_id": want,
            "attempted_keys": keys,
        }
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with open(ledger, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=True) + "\n")
    except Exception:
        pass  # the ledger must never be the outage


def cmd_update(args) -> int:
    """Merge-update one or more fields on the existing checkpoint. If the
    checkpoint doesn't exist, exit 0 (treat as no-op — caller may be running
    outside an iteration) but SURFACE the miss on stderr + a durable ledger.
    Unknown keys → stderr WARN + non-zero exit."""
    path = _checkpoint_path()
    if not path.exists():
        # Fail-open on the EXIT CODE is deliberate and must stay: the six
        # iteration-close.sh call sites treat a nonzero rc as an iteration
        # failure, and a caller genuinely outside an iteration is a legitimate
        # no-op. But exit-0 is not a licence to be SILENT.
        #
        # : checkpoint CREATION is LLM-discretionary pseudocode
        # (.claude/skills/aspirations-select/SKILL.md Phase 2.95 -- the only
        # init call site in the repo) while its DELETION is bash-enforced
        # (iteration-close.sh do_productivity_check `rm -f`). So a skipped
        # Phase 2.95 removes the file for the REST of the session, and this
        # branch then reported success to every writer while persisting
        # nothing -- degrading ~13 downstream readers invisibly. Reproduced
        # live 2026-07-28: an iteration that selected via goal-selector.sh
        # without invoking Skill(aspirations-select) ran with no checkpoint,
        # and this call returned rc=0.
        _warn_checkpoint_missing(path, args)
        return 0

    if args.set:
        # Pairs of --set key=value (parsed via argparse action='append')
        updates = {}
        for pair in args.set:
            if "=" not in pair:
                print(f"ERROR: --set requires key=value, got {pair!r}", file=sys.stderr)
                return 2
            k, v = pair.split("=", 1)
            # Try parsing v as JSON for typed values; fall back to string
            try:
                updates[k] = json.loads(v)
            except json.JSONDecodeError:
                updates[k] = v
    elif not sys.stdin.isatty():
        raw = _read_optional_stdin().strip()
        if not raw:
            return 0
        updates = json.loads(raw)
    else:
        print("ERROR: update requires --set key=value or JSON on stdin", file=sys.stderr)
        return 2

    if not isinstance(updates, dict):
        print("ERROR: update payload must be a JSON object", file=sys.stderr)
        return 2

    warns = _validate_keys(updates, mode="update")
    if warns:
        for w in warns:
            print(f"WARN[loop-state-save:update] {w}", file=sys.stderr)
        return 1

    data = json.loads(path.read_text(encoding="utf-8"))
    # : REFUSE a refresh aimed at a different goal than this checkpoint
    # anchors. Placed AFTER the parse so it reuses the read above -- the calling
    # shell would otherwise round-trip the value through text mode, where a
    # trailing "\r" makes the compare never equal (the trap cmd_clear documents
    # at its own --if-goal check). Inert when --if-goal is absent or empty, so
    # every existing caller keeps its exact current behavior. Exit 0 on refusal
    # is deliberate and load-bearing: the iteration-close.sh call sites read a
    # nonzero rc as an ITERATION failure, so a wrong-goal refresh must not abort
    # the close -- the stderr WARN + durable ledger are the observability half.
    _want_goal = (getattr(args, "if_goal", None) or "").strip()
    if _want_goal:
        _have_goal = str((data or {}).get("goal_id") or "").strip()
        if _have_goal != _want_goal:
            _warn_checkpoint_wrong_goal(path, args, _have_goal, _want_goal)
            return 0
    # Apply each key — dotted paths via _set_dotted (jq-style merge into nested
    # dicts); flat keys via plain assignment. Leaving plain dict.update() for
    # flat keys is intentional — overwrite is the right semantic for top-level
    # field replacement (phase, last_updated, etc.).
    for k, v in updates.items():
        if "." in k:
            _set_dotted(data, k, v)
        else:
            data[k] = v
    _atomic_write(path, data)
    return 0


def cmd_clear(args) -> int:
    """Remove the checkpoint file. No-op if absent.

    --if-goal <id> makes this a COMPARE-AND-SWAP: clear only when the checkpoint
    names that goal. It exists because `clear` had no CAS and therefore no safe
    caller (g-115-4990): the only place that wants to clear is a goal exiting —
    defer, release, skip — and an unconditional clear there would unlink an
    anchor naming a DIFFERENT, live goal. Mirrors `team-state-clear-in-flight.sh
    --if-goal`, the CAS aspirations-release.sh already relies on for the
    in_flight surface, so the two cleanups in that script read the same way.

    Doing the compare HERE rather than in the calling shell is the point. The
    caller would otherwise have to `read` the checkpoint, pipe it through
    python, and strip a trailing \\r before comparing — the whole round-trip is
    text-mode on Windows, so `goal_id` arrives as "g-NNN-NN\\r" and never
    compares equal, silently making the caller inert on exactly one platform
    (the same trap aspirations-claim.sh documents at its own ENSURE check).
    The single-writer already has the parsed value; nothing else should re-derive
    it.

    A mismatch is exit 0, not an error: "the anchor moved on" is the normal
    outcome of a stale release, not a failure the caller should react to.
    """
    path = _checkpoint_path()
    want = (getattr(args, "if_goal", None) or "").strip()
    if want:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return 0
        except Exception as exc:                      # unreadable/corrupt
            # Deliberately do NOT clear. An unparseable checkpoint is a
            # different defect, and unlinking it would destroy the evidence
            # while looking like a successful cleanup.
            print(f"[loop-state-save] WARN: checkpoint unreadable, not cleared "
                  f"({exc.__class__.__name__})", file=sys.stderr)
            return 0
        have = str((data or {}).get("goal_id") or "").strip()
        if have != want:
            print(f"iteration-checkpoint NOT cleared: anchored to "
                  f"{have or '<none>'}, release named {want}")
            return 0
    try:
        path.unlink()
        print(f"iteration-checkpoint cleared: {path}")
    except FileNotFoundError:
        pass
    return 0


def cmd_read(args) -> int:
    """Print the checkpoint as JSON to stdout. Exit 1 if missing (so callers
    can branch on presence). Used by graceful-stop, postcompact-restore,
    obligation-audit."""
    path = _checkpoint_path()
    if not path.exists():
        print("null")
        return 1
    print(path.read_text(encoding="utf-8"), end="")
    return 0


def main():
    p = argparse.ArgumentParser(
        prog="loop-state-save.py",
        description="Atomic + validated writes to <agent>/session/iteration-checkpoint.json",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Write initial checkpoint (overwrites existing)")
    p_init.add_argument("--json", help="JSON payload (alternative to stdin)")
    p_init.add_argument("--goal-id", dest="goal_id", default=None,
                        help="Self-sufficient form: infer the whole payload from this goal id")
    p_init.add_argument("--source", default=None, choices=("world", "agent"),
                        help="Queue the goal came from (default: world). Only with --goal-id.")
    p_init.add_argument("--phase", default=None,
                        help="Phase to anchor at (default: selected). Only with --goal-id.")
    p_init.set_defaults(func=cmd_init)

    p_update = sub.add_parser("update", help="Merge-update fields on existing checkpoint")
    p_update.add_argument("--set", action="append", help="key=value pair (repeatable)")
    p_update.add_argument("--if-goal", dest="if_goal", default=None,
                          help="COMPARE-AND-SWAP: apply only when the checkpoint is "
                               "anchored to this goal id (empty/absent = no compare)")
    p_update.set_defaults(func=cmd_update)

    p_clear = sub.add_parser("clear", help="Remove the checkpoint file")
    p_clear.add_argument("--if-goal", dest="if_goal", default=None,
                         help="Compare-and-swap: clear ONLY if the checkpoint "
                              "names this goal id (g-115-4990)")
    p_clear.set_defaults(func=cmd_clear)

    p_read = sub.add_parser("read", help="Print checkpoint JSON or 'null' if absent")
    p_read.set_defaults(func=cmd_read)

    args = p.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
