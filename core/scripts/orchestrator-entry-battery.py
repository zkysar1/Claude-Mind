"""orchestrator-entry-battery — one call enumerating the aspirations/SKILL.md
orchestrator ENTRY-phase checks (g-115-2550; consumer action of CONFIRMED
hypothesis 2026-07-16_sentinel-battery-survives-compaction).

Kills the entry-phase omission class the precheck battery (g-115-2303) killed
for precheck sentinels: the orchestrator entry sequence (Phases -0.5a..-0.5e)
was LLM-ENUMERATED — N discrete file/WM checks re-derived from SKILL.md memory
every re-entry. Fresh incident 2026-07-18T00:07 (g-115-2314 resolution
evidence): a post-compaction re-entry ran the precheck battery correctly yet
MISSED Phase -0.5c — compact-checkpoint.yaml sat unconsumed 25min (stale-skip
made it harmless; a fresher checkpoint would have mattered). A compaction
summary need only preserve "run the entry battery" (1 line); the script owns
the check list, so an entry phase can never silently fall out of the protocol.

READ-ONLY: the battery enumerates and prints; the SKILL.md phase bodies keep
ownership of action + clear for each check. Time-dependent entry calls
(stranded-claim-sweep, quiescence/dry-idle cache checks, idle-tick) are NOT
invoked here — idle-tick recomputes on every call and wm-read mutates
accessed_at, so each must be called exactly once by its own phase. The battery
lists them in protocol order in the footer so the sequence itself survives
compaction.

ENTRY_CHECKS is the registry (single consumer today; extract to a shared
module mirroring _sentinel_registry if a canary consumer appears).

Output (guard-614: structured output on EVERY exit path):
  default — one line per ACTIONABLE check plus a summary + protocol footer:
      ▸ ENTRY: <name> (phase <phase>) payload=<json> → dispatch: <section>
      [entry-battery] N actionable / M checks
  --json  — single JSON object {checked_at, checks, actionable: [...],
            findings: [...], blind: [...], error?}

`findings` and `blind` are the COMPOSED-CALLER contract: `iteration-open.py`
lifts `payload["findings"]` via `_findings_from` and `payload["blind"]` via
`_blind_from`, by those key names. Emitting only `actionable` made this battery
invisible inside `iteration-open.sh --apply` — it ran the stage, reported rc=0,
and surfaced nothing. `actionable` is retained unchanged for direct readers
(the orchestrator's own Phase -0.5a0 calls this script directly and reads the
human lines); `findings` MIRRORS it for the composed path. Found 2026-08-18
alongside the identical defect in precheck-sentinel-battery (g-115-6618): two
of the three stages iteration-open composes were blind, and only
precheck-always-run-battery — which already emitted `findings` — worked, which
is why it was the sole lane ever reporting. guard-318.

Fail-open: any error prints the summary/JSON with an `error` field and exits
0 — the LLM falls back to per-phase checks. The battery must never block the
loop.

Invocation (aspirations/SKILL.md Phase -0.5, after heartbeat-tick): direct
`py -3 core/scripts/orchestrator-entry-battery.py` or the thin wrapper
`bash core/scripts/orchestrator-entry-battery.sh`.
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
sys.path.insert(0, str(SCRIPT_DIR))

# Registry: file-presence checks + WM-slot checks, in orchestrator entry order.
# kind: "file" (path relative to agent_state_dir), "wm_slot" (non-null =
# actionable), "init_markers" (any tier's .initialized ABSENT = actionable), or
# "persona" (RUNNING + autonomous + persona-active not "true", no stop in flight).
ENTRY_CHECKS = [
    # FIRST, because nothing below it means anything in a world that was never
    # initialized. State RUNNING is set by /start, which then CHAINS into boot,
    # whose Phase -2 runs the init scripts -- so "RUNNING" and "initialized" are
    # two facts joined only by a model following that chain. Measured 2026-09-19
    # on a served loop: the model loaded boot, answered with text, and the Stop
    # hook's uniform re-entry sent every later turn into THIS loop in a world
    # with no meta tier. This battery ran each turn and said nothing; the selector
    # then failed 9 of 9. The markers are the right discriminator because the
    # init scripts WRITE them (guard-6982: never infer "it started" from
    # scaffolding -- the vessel recipe pre-creates the empty directories).
    #
    # WHY THE DISPATCH IS ONE SCRIPT AND NOT Skill(boot). Boot was already loaded
    # once in the measured run, so a second invocation meets the skill-dedup door
    # on either harness, and re-walking its full status report is the path that
    # had just failed. The one call below is boot's own Phase -2 body, verbatim.
    # guard-1867 applied honestly: running a skill's step inline skips whatever
    # else that skill writes. Boot's OTHER unconditional write is Phase -1
    # (persona) and this row does NOT reproduce it -- same run: 0 persona writes.
    # That is a different fact with a different sensor: the next row, not folded
    # in here on a marker predicate that does not measure it.
    {
        "name": "world_not_initialized",
        "kind": "init_markers",
        "phase": "boot -2",
        "skill_section": (
            "boot/SKILL.md Phase -2 (State Initialization): run "
            "`bash core/scripts/init-mind.sh $MIND_AGENT` NOW, before the precheck "
            "and the selector — idempotent and additive-only (seeds what is missing, "
            "never overwrites). The selector cannot run without the meta tier it creates"
        ),
    },
    # Boot's OTHER unconditional write (). Phase -1 is the only persona
    # write on the autonomous path -- /start sets persona in its reader and
    # assistant branches, never its autonomous one -- so "RUNNING + autonomous"
    # and "persona true" are joined only by a model that follows /start into
    # /boot. Measured 3 of 3 examined: served runs 2026-09-19 (tool ledger) and
    # 2026-09-21 (sampled live), and a live vessel 2026-09-25 (build b66348ca)
    # all sat RUNNING+autonomous with persona-active "false" -- the
    # provisioner's pre-/start reset -- while the loop executed goals. The loop
    # never reads persona; the vessel does: /sidecar/mind folded the agent row
    # ready=false for the whole run, and the sidecar's ceremony watchdog never
    # latched done (3 attempts, then stalled + disarmed).
    #
    # "not true", NOT "== false" (unlike session_desync_check's
    # running_without_persona, which asks a different question): unset is ON to
    # the framework (session.py) but NOT ready to the vessel, and after boot
    # Phase -1 it is never unset, so either value here means the write was
    # skipped. A stop in flight is excluded so this can never race the stop's own
    # mode writes. Still READ-ONLY: the dispatch is boot's own Phase -1 Step 1,
    # verbatim, and the loop runs it (the caller list in user-interaction.md
    # names this dispatch).
    {
        "name": "persona_not_active",
        "kind": "persona",
        "phase": "boot -1",
        "skill_section": (
            "boot/SKILL.md Phase -1 (Persona Activation): run "
            "`bash core/scripts/session-persona-set.sh true` NOW — the step was "
            "skipped on this RUNNING autonomous loop; the loop runs without it, "
            "but a vessel reads persona for readiness and never reports ready"
        ),
    },
    {
        "name": "pending_agents",
        "kind": "file",
        "rel": "pending-agents.yaml",
        "phase": "-0.5a",
        "skill_section": (
            "aspirations/SKILL.md Phase -0.5a (Background Agent Result Collection; "
            "pending-agents.sh list --json, collect + deregister)"
        ),
    },
    {
        "name": "compact_checkpoint",
        "kind": "file",
        "rel": "compact-checkpoint.yaml",
        "phase": "-0.5c",
        "skill_section": (
            "aspirations/SKILL.md Phase -0.5c (Compact Checkpoint Processing; "
            "compact-restore-slots.sh — idempotent, stale-skip deletes)"
        ),
    },
    {
        "name": "pending_phase_6_spark",
        "kind": "wm_slot",
        "slot": "pending_phase_6_spark",
        "phase": "-0.5c.2",
        "skill_section": (
            "aspirations/SKILL.md Phase -0.5c.2 (Pending Phase-6 Spark Sentinel; "
            "TTL check -> spark-fire-dedup check -> fire-or-clear)"
        ),
    },
    {
        "name": "blocked_sleep_until",
        "kind": "wm_slot",
        "slot": "blocked_sleep_until",
        "phase": "-0.5e",
        "skill_section": (
            "aspirations/SKILL.md Phase -0.5e Branch B (blocked-sleep residual; "
            "load-blocked-sleep-recovery.sh digest — do NOT re-read the slot)"
        ),
    },
]

# Always-run entry calls the battery deliberately does NOT invoke (each is
# time-dependent or state-mutating and must run exactly once, by its phase).
#
# stranded-claim-sweep is named as the .sh WRAPPER, not the bare .py, and that
# is load-bearing (guard-3864 / rb-7918 / ). Only the wrapper sources
# _paths.sh, which reads the per-agent local-paths.conf for MIND_WORLD --
# STORAGE_BACKEND is set globally in settings.json, so a bare `py -3` has the
# backend but no mappable world root, silently falls back to the LOCAL MIRROR,
# and decides whether a live peer's claim gets released from stale data. This
# footer is the line the protocol relies on surviving summarization after an
# autocompact, so naming the forbidden form here propagates it fleet-wide, every
# iteration. Measured 2026-08-19 (zeta, cc-02, both forms in one turn):
# shard_provenance "local-mirror" (bare .py) vs "authoritative" (wrapper).
# The other three .py entries below are CORRECT as-is -- their SKILL.md phases
# invoke them as `py -3 core/scripts/<name>.py`; only this one has a wrapper.
PROTOCOL_FOOTER = (
    "[entry-battery] always-run entry calls (protocol order, invoke each ONCE): "
    "stranded-claim-sweep.sh --apply (-0.5c.1) -> quiescence-cycle-cache.py check "
    "(-0.5e.0) -> dry-idle-cycle-cache.py check (-0.5e.0b) -> idle-tick.sh (-0.5e) "
    "-> quiescence-gate.py verify-wake (-0.5e') -> identity restore (-0.5d)"
)


def _now_iso() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


# A stop in flight owns agent-mode/persona until it lands (graceful-stop D-phases,
# the FW-11 checkpoint); the persona row must never race it.
STOP_IN_FLIGHT = ("stop-requested", "stop-loop", "stop-checkpoint.json")


def _read_signal(state_dir: Path, name: str) -> str | None:
    """A session control file's stripped text, or None when absent -- the same
    read session.py's read_file does, kept local because this battery resolves
    its own state_dir (the --agent override) rather than session.py's import-time
    SESSION_DIR."""
    p = state_dir / name
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8").strip()


def _init_marker_paths(agent: str) -> dict:
    """{tier: Path | None} for the three `.initialized` markers.

    These are the SAME local files init-world.sh / init-agent.sh / init-meta.sh
    gate on, so this check and the remedy it dispatches can never disagree about
    what "initialized" means (guard-5647). None means the tier's root did not
    RESOLVE -- a different fact from "the marker is missing", kept apart so an
    unconfigured box reads as BLIND and never as a false dispatch.

    A module-level function so tests patch ONE seam instead of three imports.
    """
    from _paths import META_DIR, WORLD_DIR, agent_dir  # type: ignore

    return {
        "world": (Path(WORLD_DIR) / ".initialized") if WORLD_DIR else None,
        "agent": (Path(agent_dir(agent)) / ".initialized") if agent else None,
        "meta": (Path(META_DIR) / ".initialized") if META_DIR else None,
    }


def _payload_str(payload) -> str:
    """Serialize a dispatch payload for the human-mode line. Dict payloads emit
    SHORT scalar fields first and long text last, THEN truncate — so the cap
    can only ever eat prose, never machine-consumed keys (g-115-2553: the
    writer's insertion order put a long `summary` before set_at/expires_at, the
    400-char cap cut them from a live pending_phase_6_spark line, and Phase
    -0.5a0's contract — the payload on the line IS the read — forced a
    contract-violating wm re-read just to recover set_at for the dedup call)."""
    if isinstance(payload, dict):
        short = {k: v for k, v in payload.items() if len(str(v)) <= 64}
        payload = {**short, **{k: v for k, v in payload.items() if k not in short}}
    s = json.dumps(payload, ensure_ascii=False, default=str)
    if len(s) > 400:
        s = s[:400] + "…"
    return s


def _context_line() -> str:
    """The context-pressure banner, once per iteration, from a RUNNER.

    WHY THIS LIVES HERE (g-115-9588). The harness injects
    `<total_tokens>N tokens left</total_tokens>` into EVERY turn, and it can read
    0 while the framework's own sensor reads zone=normal with 200,000 tokens of
    headroom — measured 2026-09-09, alpha, cc-04, SID ed7229e3 (guard-6380). An
    agent that believes the marker narrows its own scope: it releases claims,
    declines to select, abbreviates obligations and ends turns early. The
    falsifier for that belief already existed and was reachable only through
    PROSE: `context-budget-banner.sh` had ZERO script callers anywhere in the
    tree (measured cc-10, 2026-09-10), and exactly 1 of alpha's 163 journal
    entries carried its line. A misleading signal present on every turn cannot be
    beaten by a correct signal that depends on someone remembering to look.

    This battery already runs once per iteration from a literal Bash line, so
    emitting the banner here costs one 0.02s subprocess and puts the sensor value
    beside the marker in the same context window, every iteration.

    A SCOPED CALL, NEVER A RE-FORMAT (guard-2676). The banner's exact text is a
    contract with BANNER_RE in abbreviated-obligation-audit.py and banner_re in
    context-citation-audit.sh. Parsing context-budget.json and building the line
    here would be a second formatter that drifts from both silently, and the
    banner already owns the `CTX: unavailable (...)` degradation shape.

    Returns a line, never raises: this battery must never block the loop.
    """
    try:
        # bash_cmd, never a bare ["bash", ...] argv (guard-580/guard-581): on
        # win32 CreateProcess searches SYSTEM32 before PATH, so bare "bash"
        # resolves to the WSL stub, which cannot read a Windows-side script path
        # and fails rc=127 — silently, at a callsite like this one that swallows
        # exceptions. bash_cmd also passes the path through .as_posix(), whose
        # backslashes bash would otherwise strip. Imported lazily to match this
        # file's existing style and so an import failure lands in the same
        # fail-open except below.
        from _runtime_bash import bash_cmd  # type: ignore

        proc = subprocess.run(
            bash_cmd(SCRIPT_DIR / "context-budget-banner.sh"),
            capture_output=True,
            text=True,
            timeout=15,
        )
        lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
        if lines:
            return lines[0].strip()
        return "CTX: unavailable (banner produced no output)"
    except Exception as exc:  # noqa: BLE001 - fail-open by contract
        return f"CTX: unavailable (banner call failed: {type(exc).__name__})"


def _emit(report: dict, as_json: bool) -> None:
    # Set BEFORE the json branch so both output modes carry it, and because
    # _fail_open() routes through here -- an errored battery must still emit the
    # sensor (guard-614: structured output on EVERY exit path).
    report["context_line"] = _context_line()
    if as_json:
        print(json.dumps(report, ensure_ascii=False))
        return
    for entry in report.get("actionable", []):
        payload = _payload_str(entry.get("payload"))
        print(
            f"▸ ENTRY: {entry['name']} (phase {entry['phase']}) "
            f"payload={payload} → dispatch: {entry['dispatch']}"
        )
    n_act = len(report.get("actionable", []))
    n_chk = report.get("checks", 0)
    err = f" error={report['error']}" if report.get("error") else ""
    if n_act == 0 and not err:
        print(f"[entry-battery] all {n_chk} entry checks clean — no dispatches")
    else:
        print(f"[entry-battery] {n_act} actionable / {n_chk} checks{err}")
    # UNPREFIXED AND ON ITS OWN LINE, deliberately: abbreviated-obligation-audit
    # anchors BANNER_RE at `^CTX:`, so any prefix makes this line unquotable as a
    # valid citation under that audit.
    print(report["context_line"])
    print(PROTOCOL_FOOTER)


def _fail_open(report: dict, reason: str, as_json: bool) -> int:
    """Record a fail-open exit as BLIND, then emit. Mirrors the identical helper
    in precheck-sentinel-battery.py — fail-open must not mean fail-SILENT to a
    composed caller, or an errored battery is indistinguishable from a clean one
    (guard-4093) and iteration-open's own "N lane(s) blind" branch is unreachable.
    """
    report["error"] = reason
    report["blind"].append({"name": "entry-checks", "phase": "-0.5a..-0.5e",
                            "reason": reason})
    _emit(report, as_json)
    return 0


def run(agent_override: str | None, wm_path_override: str | None, as_json: bool) -> int:
    report: dict = {"checked_at": _now_iso(), "checks": len(ENTRY_CHECKS),
                    "actionable": [], "findings": [], "blind": []}

    # Resolve agent state dir (agent-wide session/ dir holds both files).
    try:
        from _paths import agent_state_dir  # type: ignore

        agent = agent_override or os.environ.get("MIND_AGENT", "")
        state_dir = Path(agent_state_dir(agent)) if agent else None
    except Exception as exc:
        return _fail_open(report, f"paths_import_failed: {exc}", as_json)
    if state_dir is None:
        return _fail_open(report, "no_agent_binding (MIND_AGENT unset)", as_json)

    # WM slots (same resolver + torn-read posture as precheck-sentinel-battery).
    wm_slots: dict = {}
    try:
        import yaml

        if wm_path_override:
            wm_path = Path(wm_path_override)
        else:
            from wm import wm_path as _resolve_wm_path

            wm_path = _resolve_wm_path()
        if wm_path.exists():
            data = yaml.safe_load(wm_path.read_text(encoding="utf-8")) or {}
            wm_slots = data.get("slots", data) if isinstance(data, dict) else {}
        else:
            report["error"] = "no_working_memory_file"
            report["blind"].append({"name": "entry-checks/wm_slot", "phase": "-0.5a..-0.5e",
                                    "reason": "no_working_memory_file"})
    except Exception as exc:
        report["error"] = f"wm_read_failed: {exc}"
        report["blind"].append({"name": "entry-checks/wm_slot", "phase": "-0.5a..-0.5e",
                                "reason": f"wm_read_failed: {exc}"})
    # These two do NOT return: the file-kind checks below are still evaluable, so
    # the battery degrades PARTIALLY rather than going dark. The blind entry is
    # what tells a composed caller that the wm_slot half was not covered -- without
    # it, a partial run and a full clean run are the same object.

    for spec in ENTRY_CHECKS:
        try:
            if spec["kind"] == "init_markers":
                markers = _init_marker_paths(agent)
                unresolved = sorted(t for t, p in markers.items() if p is None)
                if unresolved:
                    # Cannot evaluate != clean (guard-4093). Dispatching init on a
                    # box whose roots do not resolve would be a false positive.
                    report["blind"].append({
                        "name": spec["name"], "phase": spec["phase"],
                        "reason": f"tier root unresolved: {', '.join(unresolved)}"})
                    continue
                missing = sorted(t for t, p in markers.items() if not p.exists())
                if not missing:
                    continue
                payload = {"missing": missing,
                           "present": sorted(t for t in markers if t not in missing)}
            elif spec["kind"] == "persona":
                state = _read_signal(state_dir, "agent-state")
                mode = _read_signal(state_dir, "agent-mode")
                persona = _read_signal(state_dir, "persona-active")
                if state != "RUNNING" or mode != "autonomous":
                    continue
                if persona is not None and persona.lower() == "true":
                    continue
                if any((state_dir / f).exists() for f in STOP_IN_FLIGHT):
                    continue
                payload = {"agent_state": state, "agent_mode": mode,
                           "persona_active": "unset" if persona is None else persona}
            elif spec["kind"] == "file":
                p = state_dir / spec["rel"]
                if p.exists():
                    payload = {"path": str(p), "mtime": _dt.datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%dT%H:%M:%S")}
                else:
                    continue
            else:  # wm_slot
                value = wm_slots.get(spec["slot"])
                if value is None or value == "null":
                    continue
                payload = value
        except Exception:
            continue  # per-check fail-open
        report["actionable"].append(
            {
                "name": spec["name"],
                "phase": spec["phase"],
                "payload": payload,
                "dispatch": spec["skill_section"],
            }
        )
        # An ACTIONABLE entry check IS a finding. Built in the SAME loop from the
        # same spec so the two lists cannot drift; `detail` is a list, which
        # iteration-open's _findings_from joins.
        report["findings"].append(
            {
                "name": spec["name"],
                "phase": spec["phase"],
                "detail": [f"ACTIONABLE -> dispatch {spec['skill_section']}"],
            }
        )

    _emit(report, as_json)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="emit one JSON object")
    parser.add_argument("--agent", default=None, help="override agent name (tests only)")
    parser.add_argument("--wm-path", default=None, help="override working-memory.yaml path (tests only)")
    args = parser.parse_args()
    return run(args.agent, args.wm_path, args.json)


if __name__ == "__main__":
    sys.exit(main())
