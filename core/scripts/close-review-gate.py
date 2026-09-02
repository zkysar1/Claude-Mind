#!/usr/bin/env python3
"""close-review-gate.py — Definition-of-Done gate for goal closes ().

User directive 2026-08-31: before a goal can be closed it must be fully reviewed for
accuracy — "go all out, burn tokens to save humans time". Motivating incident: coach
g-012-02 closed green with 6/16 wrong entity identities because the author self-graded
against count-based criteria (g-357-39). SDLC principle: the author must not approve
their own close. Complements g-357-32 (filing-time criteria lint = Definition of Ready);
this gate is Definition of Done.

TWO INDEPENDENT CHECKS, TWO FLAGS, BOTH DEFAULT-OFF:

  A. tier-2 close review   — close_review_gate.enabled
     A tier-2 goal (goal_close_risk_tier.classify) REFUSES to close unless an APPROVE
     verdict artifact exists at agents/<agent>/session/close-reviews/<goal-id>.json.
  B. note-marker           — close_review_gate.note_marker_enabled
     A goal whose own outcome_note/progress_note carries a HIGH-confidence not-done
     marker (REVERTED / REVIEWED-NOT-CLOSED / do-not-close / reopen) REFUSES, printing
     the matched context. Reuses closed_against_own_note — the SAME detector precheck
     0.5b.22 already ships — rather than a second copy that could drift.

WHY BOTH DEFAULT OFF, AND WHY THAT IS NOT TIMIDITY (guard-1532). A gate whose printed
remedy is unreachable does not merely annoy: the caller is forced onto whatever exit
remains — usually an assertion or an override — so it MANUFACTURES FALSE RECORDS in the
very store it exists to protect, and those records are not self-correcting. Check A's
remedy is "run the close review", whose producer is the sibling goal g-357-41 and does
not exist yet; enabling A before it lands would make every tier-2 close reach for
--override-close-review. Check B's own filing requires measuring the refusal rate over
the live completed population first, because the high tier is known to flag at least one
legitimate close (g-115-5085, "do not reopen this goal"). So the flags are the ship
condition, not a hedge: build now, lock the invariant, enable when the remedy is real
(rb-4452 — ship a dep-blocked governance gate's invariant BEFORE the dependency, so it
CONSTRAINS that dependency's design instead of being retrofitted onto it).

FAIL-OPEN ON OUR OWN ERRORS, NEVER ON VERDICT ABSENCE (guard-142). Unreadable config,
missing goal record, an unparseable artifact, an import failure — all degrade to PASS
with decision=error. The one thing that must never fail open is the ABSENCE of an
APPROVE verdict on a tier-2 goal: absence of review is exactly the condition this gate
exists to catch, so it is a refusal, not an error.

LEDGER. Per-gate overrides land in world/close-review-overrides.jsonl and are recorded
as decision=override through _gate_log (gates log themselves) — NOT in
world/override-bypass-ledger.jsonl. The goal text named the bulk ledger, but
gate-overrides.md decision rule 3 reserves that file for --override-all, whose
`slots_filled` field means BLAST RADIUS across gates; a single-gate record there would
corrupt that reading. Convention wins over the goal text.

rc: 0 = pass / noop / override / gate error (fail-open).  1 = REFUSED.
Anything else is a gate fault; the caller treats it as fail-open.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

GATE_ID = "close-review-gate"

try:
    from _paths import PROJECT_ROOT, WORLD_DIR, agent_dir  # noqa: E402
except Exception:  # pragma: no cover - fail-open on import trouble
    PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
    WORLD_DIR = None
    agent_dir = None

try:
    from _gate_log import log as _gate_log  # noqa: E402
except Exception:  # pragma: no cover
    def _gate_log(*a, **k):
        return None

try:
    from _runtime_bash import bash_cmd  # noqa: E402  guard-580/581
except Exception:  # pragma: no cover
    # NO bare-"bash" fallback. argv[0] "bash" resolves to System32 WSL on win32 and
    # can hang FOREVER (guard-580) — a fallback that hangs is strictly worse than no
    # fallback, because this gate sits on the close path of every agent. Without the
    # helper we simply cannot read the store, which load_goal() reports as an empty
    # record; the gate then fails OPEN with decision=error, which is the correct
    # degradation for our own dependency failure (guard-142).
    bash_cmd = None


# ─── config ────────────────────────────────────────────────────────────────

def _flags() -> dict:
    """Read close_review_gate.{enabled,note_marker_enabled} from aspirations.yaml.

    A MISSING key, an unreadable file, or no yaml module all read FALSE — fail-safe
    to dormant. This is the single off-ramp; it must never raise."""
    out = {"enabled": False, "note_marker_enabled": False}
    try:
        import yaml  # noqa: WPS433
        cfg_path = Path(PROJECT_ROOT) / "core" / "config" / "aspirations.yaml"
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        section = data.get("close_review_gate") or {}
        if isinstance(section, dict):
            out["enabled"] = section.get("enabled") is True
            out["note_marker_enabled"] = section.get("note_marker_enabled") is True
    except Exception:
        pass
    # Env override for tests and for a deliberate one-run enable.
    if os.environ.get("CLOSE_REVIEW_GATE_ENABLED", "").strip().lower() in ("1", "true"):
        out["enabled"] = True
    if os.environ.get("CLOSE_REVIEW_NOTE_MARKER_ENABLED", "").strip().lower() in ("1", "true"):
        out["note_marker_enabled"] = True
    return out


# ─── store reads ───────────────────────────────────────────────────────────

def load_goal(goal_id: str, source: str) -> dict:
    script = Path(__file__).resolve().parent / "aspirations-query.sh"
    if bash_cmd is None or not script.is_file():
        return {}
    try:
        # bash_cmd(script, *args) — the script is the FIRST POSITIONAL, not a list.
        # Passing a list makes Path(list).as_posix() raise, which the except below
        # swallows into an empty record; the gate then reports "goal record
        # unavailable" and fails open on EVERY close. A broken call is
        # indistinguishable from a genuinely absent goal at the call site, so this
        # shape must be asserted end-to-end, not just typed correctly (guard-1404).
        res = subprocess.run(
            bash_cmd(script, "--goal-field", "goal_id", goal_id, "--full"),
            capture_output=True, text=True, timeout=120,
        )
        if res.returncode != 0 or not res.stdout.strip():
            return {}
        recs = json.loads(res.stdout)
    except Exception:
        return {}
    if isinstance(recs, list) and recs and isinstance(recs[0], dict):
        return recs[0]
    return {}


def verdict_path(agent: str, goal_id: str) -> Path | None:
    """agents/<agent>/session/close-reviews/<goal-id>.json — resolved through
    agent_dir(), never a PROJECT_ROOT/<agent> join (CLAUDE.md Agent-dir Resolution)."""
    try:
        if agent_dir is None:
            return None
        return Path(agent_dir(agent)) / "session" / "close-reviews" / f"{goal_id}.json"
    except Exception:
        return None


def read_verdict(path: Path | None) -> dict | None:
    if path is None or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# ─── ledger + telemetry ────────────────────────────────────────────────────

def _log_override(payload: dict) -> None:
    # Resolve the ledger root at CALL time and honor an explicit override.
    # WORLD_DIR is bound at IMPORT, so a test exercising the override path
    # appended to the PRODUCTION audit ledger: 20  / agent "nobody"
    # rows landed in world/close-review-overrides.jsonl before the
    # shipped-claim-mismatch gate surfaced them (). That is not
    # cosmetic pollution — the override RATE read off this ledger is the
    # documented precondition for enabling check B, so test rows corrupt the
    # very measurement that decides whether this gate ships. Any test touching
    # this path MUST set CLOSE_REVIEW_LEDGER_DIR to a tmp dir.
    root = os.environ.get("CLOSE_REVIEW_LEDGER_DIR", "").strip() or WORLD_DIR
    if root is None:
        return
    ledger = Path(root) / "close-review-overrides.jsonl"
    try:
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with open(ledger, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"close-review-gate: override ledger write failed: {e}", file=sys.stderr)


def _emit(decision: str, goal_id: str, check: str, override: str | None = None, **fields) -> dict:
    doc = {"gate": GATE_ID, "decision": decision, "goal_id": goal_id, "check": check}
    doc.update(fields)
    try:
        _gate_log(GATE_ID, decision, caller="iteration-close.sh do_verify",
                  trigger_matched=(decision in ("block", "override")),
                  payload={"goal_id": goal_id, "check": check},
                  override_reason=override)
    except Exception:
        pass
    if override:
        _log_override({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "gate": GATE_ID, "check": check, "goal_id": goal_id,
            "justification": override,
            "agent": os.environ.get("MIND_AGENT"),
            "session_id": os.environ.get("MIND_SID"),
            "context": {"caller": "iteration-close.sh do_verify"},
            **fields,
        })
    print(json.dumps(doc, ensure_ascii=False))
    return doc


# ─── main ──────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--goal", required=True)
    ap.add_argument("--source", default="world", choices=("world", "agent"))
    ap.add_argument("--agent", default=None, help="defaults to $MIND_AGENT")
    ap.add_argument("--files", nargs="*", default=None)
    ap.add_argument("--artifacts-count", type=int, default=None)
    ap.add_argument("--first-of-aspiration", action="store_true")
    ap.add_argument("--override-close-review", default=None,
                    help="justification; turns a tier-2 BLOCK into a logged pass")
    ap.add_argument("--override-note-marker", default=None,
                    help="justification; turns a note-marker BLOCK into a logged pass")
    ap.add_argument("--goal-json", default=None, help="JSON goal record path (tests)")
    args = ap.parse_args(argv)

    agent = args.agent or os.environ.get("MIND_AGENT") or ""
    flags = _flags()

    if not flags["enabled"] and not flags["note_marker_enabled"]:
        _emit("noop", args.goal, "both", reason="both flags dormant (ship default)")
        return 0

    # Load the goal. Absence is OUR error, not the goal's fault -> fail open.
    if args.goal_json:
        try:
            goal = json.loads(Path(args.goal_json).read_text(encoding="utf-8"))
        except Exception:
            goal = {}
    else:
        goal = load_goal(args.goal, args.source)
    if not goal:
        _emit("error", args.goal, "both", reason="goal record unavailable — fail-open")
        return 0

    # ── check B: note marker (runs first; cheapest, and independent of tier) ──
    if flags["note_marker_enabled"]:
        try:
            from closed_against_own_note import scan_note, confidence
            hits = []
            for field in ("outcome_note", "progress_note"):
                for h in scan_note(goal.get(field)) or []:
                    h = dict(h)
                    h["field"] = field
                    hits.append(h)
            if hits and confidence(hits) == "high":
                if args.override_note_marker:
                    _emit("override", args.goal, "note-marker",
                          override=args.override_note_marker, hits=hits[:5])
                else:
                    print(
                        f"close-review-gate: REFUSED — {args.goal}'s own note carries a "
                        f"HIGH-confidence not-done marker:", file=sys.stderr)
                    for h in hits[:5]:
                        print(f"    [{h.get('field')}] {h.get('marker')!r} :: "
                              f"{str(h.get('context'))[:160]}", file=sys.stderr)
                    print("  If this close is correct, pass "
                          "--override-note-marker \"<why the marker does not apply>\" "
                          "(logged to world/close-review-overrides.jsonl).", file=sys.stderr)
                    _emit("block", args.goal, "note-marker", hits=hits[:5])
                    return 1
        except Exception as e:  # detector fault -> fail open
            _emit("error", args.goal, "note-marker", reason=f"detector fault: {e}")

    # ── check A: tier-2 close review ──
    if flags["enabled"]:
        try:
            from goal_close_risk_tier import classify
            tier = classify(goal, files_touched=args.files,
                            artifacts_count=args.artifacts_count,
                            is_first_of_aspiration=args.first_of_aspiration)
        except Exception as e:
            _emit("error", args.goal, "tier", reason=f"classifier fault: {e} — fail-to-tier-1")
            return 0

        if tier.get("tier") != 2:
            _emit("pass", args.goal, "tier", tier=tier.get("tier"))
            return 0

        v = read_verdict(verdict_path(agent, args.goal))
        approved = isinstance(v, dict) and str(v.get("verdict", "")).upper() == "APPROVE"
        if approved:
            _emit("pass", args.goal, "tier", tier=2, reviewer=v.get("reviewer"))
            return 0

        # ABSENCE OF REVIEW — the one condition that must never fail open.
        if args.override_close_review:
            _emit("override", args.goal, "tier", override=args.override_close_review,
                  tier=2, reasons=tier.get("reasons"))
            return 0

        print(f"close-review-gate: REFUSED — {args.goal} is tier 2 and has no APPROVE "
              f"close-review verdict.", file=sys.stderr)
        for r in tier.get("reasons", []):
            print(f"    trigger: {r}", file=sys.stderr)
        p = verdict_path(agent, args.goal)
        print(f"  Expected verdict artifact: {p}", file=sys.stderr)
        print("  Produce it with the fresh-eyes close reviewer, or pass "
              "--override-close-review \"<justification>\" "
              "(logged to world/close-review-overrides.jsonl).", file=sys.stderr)
        _emit("block", args.goal, "tier", tier=2, reasons=tier.get("reasons"))
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
