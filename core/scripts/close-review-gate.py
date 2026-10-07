#!/usr/bin/env python3
"""close-review-gate.py — Definition-of-Done gate for goal closes ().

User directive 2026-08-31: before a goal can be closed it must be fully reviewed for
accuracy — "go all out, burn tokens to save humans time". Motivating incident: coach
g-012-02 closed green with 6/16 wrong entity identities because the author self-graded
against count-based criteria (g-357-39). SDLC principle: the author must not approve
their own close. Complements g-357-32 (filing-time criteria lint = Definition of Ready);
this gate is Definition of Done.

TWO INDEPENDENT CHECKS, TWO FLAGS. A ships OFF; B is ON since 2026-10-06:

  A. tier-2 close review   — close_review_gate.enabled
     A tier-2 goal (goal_close_risk_tier.classify) REFUSES to close unless an APPROVE
     verdict artifact exists at world/audit-reports/close-reviews/<goal-id>.json,
     written by someone OTHER than the closing agent. The path is GOAL-keyed and
     world-scoped (g-357-41); an APPROVE whose `reviewer` is the closer, or which
     names no reviewer at all, is refused as not-an-independent-review. A closer in the
     post-hoc lane, whose BODY_ROLE is listed in review_closer_roles or
     review_sampled_roles, passes with the lane named: its closures are reviewed AFTER
     the close (g-375-09). An unset BODY_ROLE means reducer-or-unknown and is checked
     (g-375-145).
     A refusal is SELF-SERVE (g-375-147). The gate stamps `review_requested` on the goal
     itself unless a request is already open, so close-review-queue.py offers it to an
     independent reviewer while the goal waits open. The queue offers only a goal that
     names its closer, so a refusal on a goal that names none says to claim it first.
     A releasing verdict releases the close only if it answers the goal's current
     request. --override-close-review is honored only where team-state lists no other
     mind, which is a solo deployment.
     A refused close WAITS (g-375-149). The gate writes the close's outcome note for the
     reviewer when the goal carries none (--outcome-note-file, else the summary on
     stdin), and goal-selector.py holds a goal whose request no verdict answers yet, so
     a released loop close is not picked again before its verdict lands.
  B. note-marker          — close_review_gate.note_marker_enabled
     A goal whose own outcome_note/progress_note carries a HIGH-confidence not-done
     marker (REVERTED / REVIEWED-NOT-CLOSED / do-not-close / reopen) REFUSES, printing
     the matched context. Reuses closed_against_own_note — the SAME detector precheck
     0.5b.22 already ships — rather than a second copy that could drift.

WHY A SHIPPED OFF, AND WHY THAT IS NOT TIMIDITY (guard-1532). A gate whose printed
remedy is unreachable does not merely annoy: the caller is forced onto whatever exit
remains — usually an assertion or an override — so it MANUFACTURES FALSE RECORDS in the
very store it exists to protect, and those records are not self-correcting. Check A was
first held off because its remedy's producer, the sibling goal g-357-41, did not exist.
That producer landed 2026-09-02, and A stays off for MEASURED reasons that replaced that
one. They live in one place, the close_review_gate block of core/config/aspirations.yaml;
read them there rather than restating them here. Check B's filing required measuring its
refusal rate first, because the high tier was known to flag a legitimate close
(g-115-5085, "do not reopen this goal"). Measured 0/448 (g-357-77) and 0/209 on
2026-10-06, B is on since that day (g-375-144). So the flags are the ship condition, not
a hedge: build now, lock the invariant, enable when the remedy is real (rb-4452 — ship a
dep-blocked governance gate's invariant BEFORE the dependency, so it CONSTRAINS that
dependency's design instead of being retrofitted onto it).

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
corrupt that reading. Convention wins over the goal text. An override the gate does not
honor, because another mind is listed, is not an override: it is logged on the block as
`override_refused` and never reaches the ledger (g-375-147).

rc: 0 = pass / noop / override / gate error (fail-open).  1 = REFUSED.
Anything else is a gate fault; the caller treats it as fail-open.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

GATE_ID = "close-review-gate"

#: The verdicts that RELEASE a tier-2 close. This gate owns the definition —
#: `close-review-verdict.py` imports it rather than keeping a second copy, the
#: same reason `independence_defect` lives here ( re-review, F3).
#:
#: APPROVE_WITH_NOTES is an APPROVAL, not a soft REJECT: the reviewer found the
#: close sound AND recorded non-blocking observations. It had to be added HERE
#: in the same change that made it writable, because the producer's own comment
#: on `VERDICTS` predicted exactly what a producer-only addition would do — an
#: unrecognised string "would read as 'not APPROVE' and silently behave as
#: REJECT while looking like a third state". A third state the consumer does not
#: know is worse than no third state at all.
#:
#: Its one behavioural difference from APPROVE is downstream, in the producer:
#: an APPROVE_WITH_NOTES ROUTES its findings to the goal record. A plain APPROVE
#: carrying findings routes nothing, so before this existed the notes on an
#: otherwise-good close reached the ledger and nobody else.
RELEASING_VERDICTS = ("APPROVE", "APPROVE_WITH_NOTES")


def releases_close(verdict: str | None) -> bool:
    """Whether this verdict string releases a tier-2 close.

    Case- and whitespace-insensitive on the same grounds as
    `independence_defect`: the value is written by one hand and read by another.
    """
    return str(verdict or "").strip().upper() in RELEASING_VERDICTS

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

def _post_hoc_roles(section) -> frozenset:
    """Closer roles reviewed AFTER the close (), which check A therefore passes:
    review_closer_roles plus review_sampled_roles, lower-cased. Anything but a list of
    strings contributes NOTHING, so a malformed list exempts no one (g-375-145)."""
    roles = set()
    for key in ("review_closer_roles", "review_sampled_roles"):
        val = section.get(key) if isinstance(section, dict) else None
        if isinstance(val, list):
            roles.update(r.strip().lower() for r in val if isinstance(r, str) and r.strip())
    return frozenset(roles)


def _flags() -> dict:
    """Read close_review_gate.{enabled,note_marker_enabled} and the post-hoc role lists
    from aspirations.yaml.

    A MISSING key, an unreadable file, or no yaml module all read FALSE — fail-safe
    to dormant. This is the single off-ramp; it must never raise."""
    out = {"enabled": False, "note_marker_enabled": False, "post_hoc_roles": frozenset()}
    try:
        import yaml  # noqa: WPS433
        cfg_path = Path(PROJECT_ROOT) / "core" / "config" / "aspirations.yaml"
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        section = data.get("close_review_gate") or {}
        if isinstance(section, dict):
            out["enabled"] = section.get("enabled") is True
            out["note_marker_enabled"] = section.get("note_marker_enabled") is True
            out["post_hoc_roles"] = _post_hoc_roles(section)
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


def verdict_path(goal_id: str) -> Path | None:
    """world/audit-reports/close-reviews/<goal-id>.json — GOAL-keyed and
    WORLD-scoped, deliberately NOT keyed by the closing agent.

    It was agents/<CLOSING agent>/session/close-reviews/<goal-id>.json until
    2026-09-02, and that made this gate satisfiable ONLY BY SELF-REVIEW — the
    exact thing the module docstring's SDLC principle forbids. An INDEPENDENT
    reviewer cannot write into the closer's private agent dir: cross-agent
    writes are unsupported by design (path-resolution.md routes them through
    the world board or the shared team-state store instead), and a SUBAGENT
    reviewer is not exempt, because it inherits its own agent binding and hits
    the same wall. All 32 tests passed throughout, because every one built the
    artifact under the SAME agent that then closed — a fixture shape that
    cannot express the defect. Found by fresh-eyes review (bravo, cc-05,
    2026-09-02) BEFORE the producer half was built, which is the only reason
    this is one function instead of two coordinated halves.

    WHY world/audit-reports/ AND NOT world/close-reviews/: guard-599 names
    agents/<agent>/session/ as a wrong home for exactly this artifact class
    ("fresh-eyes reports"), and the L1 cruft hook REFUSES a NEW top-level entry
    under WORLD_PATH with no agent-side override — its own printed remedy is
    "place under an EXISTING top-level dir". audit-reports/ already holds
    per-goal verdicts (g-335-534-verdict.md), so this needs no new world
    top-level entry and no init script change.

    Root resolution MIRRORS _log_override on purpose: the same env seam, so a
    test that isolates one isolates both and neither can reach the production
    world. That is the g-357-40 ledger-pollution lesson applied ahead of time
    rather than after."""
    try:
        root = os.environ.get("CLOSE_REVIEW_LEDGER_DIR", "").strip() or WORLD_DIR
        if root is None:
            return None
        return Path(root) / "audit-reports" / "close-reviews" / f"{goal_id}.json"
    except Exception:
        return None


def independence_defect(verdict: dict | None, closer: str) -> str | None:
    """Why this APPROVE verdict is not an INDEPENDENT review, or None if it is.

    The module docstring has carried "the author must not approve their own
    close" since g-357-40 as a stated principle with NO mechanism — none was
    POSSIBLE while the artifact lived in the closer's own agent dir, since every
    verdict there was self-written by construction. A goal-keyed world path is
    what makes reviewer identity comparable to closer identity, so the principle
    becomes checkable. Two defects, both meaning "nobody independent signed
    this":
      self-review   — reviewer IS the closing agent.
      unattributed  — no reviewer named. An approval nobody is accountable for
                      cannot be shown to be independent, and this gate's own
                      fail-direction rule says absence of review is a refusal,
                      never a fail-open (guard-142).
    Compared case-insensitively on the trimmed name: `reviewer` is written by
    the producer and `closer` comes from argv/env — two different hands, and a
    casing difference is not independence."""
    r = str((verdict or {}).get("reviewer") or "").strip()
    if not r:
        return "unattributed"
    if r.lower() == str(closer or "").strip().lower():
        return "self-review"
    return None


def read_verdict(path: Path | None) -> dict | None:
    """Return the goal's CURRENT verdict, or None.

    TWO ON-DISK SHAPES, both live:
      * list  -- the append-only audit trail (g-357-41 / F11). The ledger keeps
                 EVERY verdict so a re-review cannot erase its predecessor.
      * dict  -- a single verdict written before the ledger became append-only.
                 Still present on disk, so this leg is load-bearing, not legacy
                 politeness.

    THE LAST ENTRY WINS, and that is deliberate: REJECT -> rework -> re-review is
    the path this gate exists to drive, so an APPROVE recorded after a REJECT
    must be able to clear it. A gate that honoured the earliest verdict would
    make rework structurally unable to close the goal. Append order IS
    chronological (the writer only ever appends), so the last element is the
    newest without needing to sort on `reviewed_at` and invent a tie-break.
    """
    if path is None or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(data, list):
        # An empty trail is "no verdict", not a malformed one.
        return data[-1] if data else None
    return data


# ─── review requests and the roster () ────────────────────────────

def _queue():
    """close-review-queue.py by path (its filename is hyphenated), loaded only when a goal
    carries a review request. Its `answers` is the one definition of a request being
    answered, already shared by the queue's listing and the completed-not-closed drain, so
    this gate cannot disagree with either about whether a review is still owed. The queue
    loads this gate the same lazy way, so neither runs the other at import."""
    cached = sys.modules.get("close_review_queue")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        "close_review_queue", Path(__file__).resolve().parent / "close-review-queue.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["close_review_queue"] = mod
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except BaseException:
        # A half-loaded module must not be served to the next caller as if it were whole.
        sys.modules.pop("close_review_queue", None)
        raise
    return mod


def stamp_request(goal_id: str, source: str, when: str) -> str | None:
    """Write `review_requested=<when>` on the goal through the canonical store writer.

    Returns None when the write landed, else why it did not. Never raises: a request that
    could not be written still leaves the close refused, and the refusal prints the
    command that writes it by hand."""
    script = Path(__file__).resolve().parent / "aspirations-update-goal.sh"
    if bash_cmd is None or not script.is_file():
        return "store writer unavailable"
    try:
        # bash_cmd(script, *args): the script is the FIRST POSITIONAL, as in load_goal.
        res = subprocess.run(
            bash_cmd(script, "--source", source, goal_id, "review_requested", when),
            capture_output=True, text=True, timeout=120,
        )
    except Exception as e:
        return f"{type(e).__name__}: {e}"
    if res.returncode != 0:
        return f"rc={res.returncode}: {(res.stderr or res.stdout).strip()[-300:]}"
    return None


def would_be_note(note_file: str | None, summary: str) -> tuple[str, str]:
    """(text, source) of the outcome note this close would land ().

    The --outcome-note-file, which replaces the record's note at the status write, else
    the summary, which closure-evidence-write.sh lands after the status write when the
    record has no note: the same order the closure-evidence gate reads them in. An
    unreadable file reads as empty text, so nothing is written from it."""
    if note_file:
        try:
            return (Path(note_file).read_text(encoding="utf-8", errors="replace"),
                    "outcome-note-file")
        except OSError:
            return "", "outcome-note-file"
    return summary, ("summary" if summary.strip() else "")


def write_outcome_note(goal_id: str, source: str, note: str,
                       note_file: str | None) -> str | None:
    """Write the close's outcome note on the goal for the reviewer ().

    Through closure-evidence-write.sh, the one writer of the closure narrative. The
    caller writes only when the record carries no note, and the writer never clobbers
    one either; --no-supersede keeps even a recurring goal's earlier note. That writer
    always exits 0 by contract, so whether the note landed is read back from the store.

    Returns None when it landed, else why not. Never raises: a note that could not be
    written still leaves the close refused, and the refusal prints the command that
    writes it."""
    script = Path(__file__).resolve().parent / "closure-evidence-write.sh"
    if bash_cmd is None or not script.is_file():
        return "closure-evidence writer unavailable"
    text = ("--summary-file", note_file) if note_file else ("--summary", note)
    try:
        res = subprocess.run(
            bash_cmd(script, "--goal", goal_id, "--source", source, *text,
                     "--prefix", "[close-review-gate]", "--no-supersede"),
            capture_output=True, text=True, timeout=180,
        )
    except Exception as e:
        return f"{type(e).__name__}: {e}"
    # The whole note must be on the record. The writer may append a signature or a
    # provenance line after it but never changes its text, and a note that only shares
    # a line with this one was written by someone else. Whitespace is collapsed on both
    # sides: the writer's shell trims trailing newlines and keeps a CR this read drops.
    sent = " ".join(note.split())
    landed = " ".join(str((load_goal(goal_id, source) or {}).get("outcome_note") or "").split())
    if sent and sent in landed:
        return None
    said = (res.stderr or res.stdout or "").strip()[-300:]
    return f"not on the record after the write (rc={res.returncode})" + (f": {said}" if said else "")


def other_minds(agent: str, roster_json: str | None = None) -> list[str] | None:
    """The minds team-state lists other than the closer, or None when the roster cannot be
    read. team-state-retire.sh removes a retired mind's row and composing the roster drops
    retired rows, so the list is the minds that could write an independent verdict.
    `roster_json` replaces the read with a file of the same shape (tests)."""
    try:
        if roster_json:
            data = json.loads(Path(roster_json).read_text(encoding="utf-8"))
        else:
            script = Path(__file__).resolve().parent / "team-state-read.sh"
            if bash_cmd is None or not script.is_file():
                return None
            res = subprocess.run(bash_cmd(script, "--field", "agent_status", "--json"),
                                 capture_output=True, text=True, timeout=60)
            if res.returncode != 0 or not res.stdout.strip():
                return None
            data = json.loads(res.stdout)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    me = str(agent or "").strip().lower()
    return sorted(str(name) for name in data if str(name).strip().lower() != me)


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
    ap.add_argument("--roster-json", default=None,
                    help="JSON roster in team-state's agent_status shape (tests)")
    ap.add_argument("--outcome-note-file", default=None,
                    help="the close's outcome note, which a check-A refusal writes for the "
                         "reviewer when the goal carries none (g-375-149)")
    ap.add_argument("--summary-stdin", action="store_true",
                    help="read the close's summary from stdin (do_verify's path); the note a "
                         "refusal writes when no --outcome-note-file is given")
    args = ap.parse_args(argv)
    # Drain stdin FIRST, as the closure-evidence gate does: do_verify pipes the summary in,
    # and a return below must not leave its writer on a closed pipe. Decoded as UTF-8 so a
    # byte the locale cannot read never crashes the gate out of its verdict.
    summary = (sys.stdin.buffer.read().decode("utf-8", "replace")
               if args.summary_stdin else "")

    agent = args.agent or os.environ.get("MIND_AGENT") or ""
    flags = _flags()

    if not flags["enabled"] and not flags["note_marker_enabled"]:
        _emit("noop", args.goal, "both", reason="both flags dormant")
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
            else:
                # A pass is logged too (). With check A off, nothing else emits
                # for this close, so without this line a quiet field window could not be
                # told from a check that never ran (guard-5501).
                _emit("pass", args.goal, "note-marker",
                      confidence=confidence(hits) if hits else "none")
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

        # The post-hoc lane (): closers in a listed role are reviewed AFTER the
        # close, so check A passes them and names the lane. bash-agent-inject.py exports
        # BODY_ROLE only on the worker fork path, the same variable the completed_by_role
        # stamp reads, so unset means reducer-or-unknown and is checked ().
        role = os.environ.get("BODY_ROLE", "").strip().lower()
        if role and role in flags["post_hoc_roles"]:
            _emit("pass", args.goal, "tier", tier=2, lane="post-hoc", role=role)
            return 0

        v = read_verdict(verdict_path(args.goal))
        approved = isinstance(v, dict) and releases_close(v.get("verdict"))
        # A self-approved or unattributed APPROVE is NOT an approval. Demoting it
        # here (rather than refusing inline) is deliberate: it falls through to the
        # SAME override branch and the same _emit shape as verdict absence, so the
        # override stays reachable and there is exactly one refusal path to test.
        defect = independence_defect(v, agent) if approved else None
        if defect:
            approved = False
        # A request made after the verdict asks for a fresh review (), so the
        # verdict must ANSWER the goal's current request, by the rule the queue lists by.
        # `answered` stays None with no request, or when the queue cannot load; that is
        # our own fault, so the rule falls back to the one before .
        requested = goal.get("review_requested")
        answered = answers_fault = None
        if requested:
            try:
                answered = bool(_queue().answers(v, requested))
            except Exception as e:
                answers_fault = f"{type(e).__name__}: {e}"
        stale = bool(approved and answered is False)
        if stale:
            approved = False
        if approved:
            # `answered` and `answers_fault` say whether the request rule ran: a pass with a
            # request and answered None is one the rule could not check.
            _emit("pass", args.goal, "tier", tier=2, reviewer=v.get("reviewer"),
                  answered=answered, answers_fault=answers_fault)
            return 0

        # ABSENCE OF REVIEW — the one condition that must never fail open. The override
        # is the exit for a deployment with no second mind to review (). Where
        # team-state lists another mind, an independent review is available, and an
        # override would write a false record into the ledger that measures this gate.
        others = None
        if args.override_close_review:
            others = other_minds(agent, args.roster_json)
            if not others:  # [] is a solo deployment; None an unreadable roster (fail-open)
                _emit("override", args.goal, "tier", override=args.override_close_review,
                      tier=2, reasons=tier.get("reasons"), defect=defect,
                      roster="solo" if others == [] else "unreadable")
                return 0

        # Request the review itself (), so a refusal has a next move that is not
        # the override: the queue offers every goal carrying review_requested to an
        # independent reviewer. A request already open is left alone. One that a verdict
        # answered without releasing the close (a REJECT, or a demoted APPROVE) is made
        # again, because the closer is closing again and the queue no longer lists it.
        # Only a record read from the store is stamped: a --goal-json record has no store
        # record behind it, so its request is reported and never written.
        # `request` is the decision (stamp, restamp or open); `written` is what became of
        # the write: True landed, False failed, None not attempted.
        when, request, written, request_error = requested, "open", None, None
        if not requested or answered is True:
            request = "restamp" if requested else "stamp"
            when = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
            if not args.goal_json:
                request_error = stamp_request(args.goal, args.source, when)
                written = request_error is None
        # The queue offers a request only on a goal that names its closer (executor_of:
        # completed_by, else executed_by, else claimed_by) and declines one that names none,
        # so that request would wait unseen. A claim records executed_by, which survives the
        # release a refused loop close goes through. None when the queue cannot load: our
        # own fault, so the refusal then says nothing about reachability either way.
        try:
            reachable = bool(_queue().executor_of(goal))
        except Exception:
            reachable = None
        # Write the close's outcome note for the reviewer (). do_verify lands it
        # only at the status write, after this gate, so a refused close would leave the
        # reviewer a goal with no closure evidence, and leave the completed-not-closed
        # drain, which closes an open goal only when it carries a note, nothing to close
        # once a releasing verdict lands. Written only when the record carries no note;
        # a --goal-json record is reported, never written. `note` is the decision
        # (write, kept or none) and `note_written` what became of it, as for the request.
        note_text, note_from = would_be_note(args.outcome_note_file, summary)
        existing = str(goal.get("outcome_note") or "")
        note = "none" if not note_text.strip() else ("kept" if existing.strip() else "write")
        note_written = note_error = None
        if note == "write" and not args.goal_json:
            note_error = write_outcome_note(args.goal, args.source, note_text,
                                            args.outcome_note_file)
            note_written = note_error is None

        if defect == "self-review":
            print(f"close-review-gate: REFUSED — {args.goal} is tier 2 and its only "
                  f"APPROVE verdict was written by the closing agent itself "
                  f"(reviewer={str((v or {}).get('reviewer'))!r} == closer={agent!r}).",
                  file=sys.stderr)
            print("  A self-approved close is not a reviewed close — the author must "
                  "not approve their own close.", file=sys.stderr)
        elif defect == "unattributed":
            print(f"close-review-gate: REFUSED — {args.goal} is tier 2 and its APPROVE "
                  f"verdict names no reviewer, so its independence cannot be "
                  f"established.", file=sys.stderr)
        elif stale:
            print(f"close-review-gate: REFUSED — {args.goal} is tier 2 and its "
                  f"{v.get('verdict')} verdict (reviewed_at {v.get('reviewed_at')!r}) "
                  f"predates its review request (review_requested {requested!r}), so it "
                  f"does not answer the request.", file=sys.stderr)
        else:
            print(f"close-review-gate: REFUSED — {args.goal} is tier 2 and has no APPROVE "
                  f"close-review verdict.", file=sys.stderr)
        for r in tier.get("reasons", []):
            print(f"    trigger: {r}", file=sys.stderr)
        if request == "open":
            print(f"  A review request is already open (review_requested={when}). The goal "
                  f"stays open until a releasing verdict answers it; re-run this close "
                  f"then.", file=sys.stderr)
        elif written:
            print(f"  REVIEW REQUESTED: review_requested={when} is now on {args.goal}. The "
                  f"goal stays open; re-run this close once a releasing verdict answers the "
                  f"request.", file=sys.stderr)
        elif written is None:
            print("  The goal record came from --goal-json, so the review request was not "
                  "written.", file=sys.stderr)
        else:
            print(f"  REVIEW REQUEST NOT WRITTEN ({request_error}). Write it: bash "
                  f"core/scripts/aspirations-update-goal.sh --source {args.source} "
                  f"{args.goal} review_requested {when}", file=sys.stderr)
        if reachable is False:
            print(f"  NO CLOSER IS RECORDED on {args.goal} (no completed_by, executed_by or "
                  f"claimed_by), so close-review-queue.py declines its request and offers it "
                  f"to no reviewer. Claim the goal, which records executed_by: bash "
                  f"core/scripts/aspirations-claim.sh {args.goal} --source {args.source}",
                  file=sys.stderr)
        elif reachable and (request == "open" or written):
            print("  close-review-queue.py offers the request to an independent reviewer.",
                  file=sys.stderr)
        if request == "open" or written:
            print("  The goal selector holds the goal until a verdict answers the request "
                  "(block_reason awaiting_review), so a released loop close is not picked "
                  "again before then; a REJECT returns it for rework.", file=sys.stderr)
        if note == "write" and note_written:
            print(f"  OUTCOME NOTE WRITTEN for the reviewer: this close's {note_from} "
                  f"({len(note_text)} chars) is now the outcome_note of {args.goal}.",
                  file=sys.stderr)
        elif note == "write" and note_written is None:
            print("  The goal record came from --goal-json, so the outcome note was not "
                  "written.", file=sys.stderr)
        elif note == "write":
            src = args.outcome_note_file or "<a file holding this close's summary>"
            print(f"  OUTCOME NOTE NOT WRITTEN ({note_error}). The reviewer reads the goal "
                  f"record, so write it: bash core/scripts/closure-evidence-write.sh --goal "
                  f"{args.goal} --source {args.source} --summary-file {src}",
                  file=sys.stderr)
        elif note == "kept":
            print(f"  {args.goal} already carries an outcome_note ({len(existing)} chars). It "
                  f"is not overwritten, so the reviewer reads that note"
                  + (", and this close's --outcome-note-file replaces it only at the status "
                     "write." if args.outcome_note_file else "."), file=sys.stderr)
        else:
            print("  This close carried no outcome note and no summary, so none was written "
                  "for the reviewer.", file=sys.stderr)
        p = verdict_path(args.goal)
        print(f"  Expected verdict artifact: {p}", file=sys.stderr)
        print("  Produce it with the fresh-eyes close reviewer run by an INDEPENDENT "
              "reviewer (a live peer via the review-request lane, else a fresh-context "
              "subagent).", file=sys.stderr)
        if others:
            print(f"  --override-close-review was NOT honored: team-state lists other "
                  f"minds ({', '.join(others)}), so an independent review is available.",
                  file=sys.stderr)
        else:
            print("  --override-close-review \"<justification>\" is honored only where "
                  "team-state lists no other mind (logged to "
                  "world/close-review-overrides.jsonl).", file=sys.stderr)
        _emit("block", args.goal, "tier", tier=2, reasons=tier.get("reasons"),
              defect=defect, stale=stale, request=request, request_written=written,
              review_requested=when, request_error=request_error,
              request_reachable=reachable, override_refused=others or None,
              answers_fault=answers_fault, note=note, note_from=note_from or None,
              note_written=note_written, note_error=note_error)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
