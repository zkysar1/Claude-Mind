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


def cmd_init(args) -> int:
    """Write the initial checkpoint. Reads JSON payload from stdin OR from
    the --json flag. Required keys per SCHEMA must be present. Existing
    checkpoint is overwritten (selecting a new goal supersedes any prior anchor)."""
    if args.json:
        payload = json.loads(args.json)
    elif not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
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

    warns = _validate_keys(payload, mode="init")
    if warns:
        for w in warns:
            print(f"WARN[loop-state-save:init] {w}", file=sys.stderr)
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
            "reader. Re-anchor by piping a JSON object carrying ALL required keys "
            "(%s) into `loop-state-save.sh init` -- a bare `init` with no stdin "
            "exits 1, and a partial object warns once per missing key and writes "
            "nothing, so the naive reading of this line fails twice before it "
            "works (g-115-3454)."
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
        raw = sys.stdin.read().strip()
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
    """Remove the checkpoint file. No-op if absent."""
    path = _checkpoint_path()
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
    p_init.set_defaults(func=cmd_init)

    p_update = sub.add_parser("update", help="Merge-update fields on existing checkpoint")
    p_update.add_argument("--set", action="append", help="key=value pair (repeatable)")
    p_update.set_defaults(func=cmd_update)

    p_clear = sub.add_parser("clear", help="Remove the checkpoint file")
    p_clear.set_defaults(func=cmd_clear)

    p_read = sub.add_parser("read", help="Print checkpoint JSON or 'null' if absent")
    p_read.set_defaults(func=cmd_read)

    args = p.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
