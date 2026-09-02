"""test_live_phase_entry_type_sync.py — pins live-phase-emit.sh against execution-diary.py ().

core/scripts/live-phase-emit.sh HAND-MIRRORS the VALID_ENTRY_TYPES set defined in
core/scripts/execution-diary.py. Adding a type there without a matching edit here
silently breaks the live_phase mirror, and the breakage is INVISIBLE: the only
call site (heartbeat-tick.sh) guards the call with `|| true`, so the emitter
crashes, the mirror stops updating, and nothing reports it. `live_phase` is
strictly informational — partners use `last_active` for liveness — so no
downstream check fails either.

That drift ALREADY HAPPENED. `scorer_override` was added by g-115-2812 (Scorer
Sovereignty Layer B) and never added to the emitter, so every heartbeat landing
on a deviation-claim tail row fell through to the terminating `else` and wrote no
live_phase at all. It was repaired inline under g-115-3633; this file is the
guard so the NEXT one cannot happen, and it is deliberately not a re-fix of that
repair.

WHY A TEST AND NOT A REFACTOR. Importing the source of truth would be the obvious
fix and is not available: live-phase-emit.sh is annotated IRREDUCIBLY LOCAL and
its header forbids adding indirection on the per-Bash-call latency path. So the
mirror stays hand-kept and the pin lives out here. Modelled on the established
sibling test_rb_entry_type_taxonomy_sync.py, which pins two hand-kept sets as
set-equal for the same reason.

THREE ASSERTIONS, and each maps to one of the goal's verification outcomes:
  1. the types the emitter recognizes — its two `et == "..."` branches PLUS its
     `et in (...)` membership tuple — are set-equal to the imported
     VALID_ENTRY_TYPES;
  2. the terminating `else` still calls sys.exit, so a future repair cannot
     degrade into a catch-all. That would violate the emitter's own file-level
     CRITICAL INVARIANT against unrecognized-entry_type masking, and it would do
     so while making this file's assertion 1 EASIER to satisfy — which is exactly
     why it needs its own pin;
  3. `bash -n` parses the emitter. Its whole dispatch lives inside a bash
     SINGLE-QUOTED string, so one stray apostrophe breaks the script at parse
     time (guard-504), and an extracted-logic harness provably cannot catch that
     (rb-5486) — the check has to run against the real file.

Assertion 1 carries a PARSER POSITIVE CONTROL (see
test_parser_actually_matched_both_shapes). A regex that silently matches nothing
would make the set comparison fail loudly today, but if the emitter's shape ever
changes such that BOTH the parse and the source set come back empty, set-equality
passes vacuously. The control asserts all three collections are non-empty, so
this file can never go green by measuring nothing (guard-2298).
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
EMITTER = CORE_SCRIPTS / "live-phase-emit.sh"
DIARY_PY = CORE_SCRIPTS / "execution-diary.py"

if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

# Resolve the bash binary EXPLICITLY — never a bare "bash" as argv[0] (guard-580).
# On win32 CreateProcess searches System32 before PATH, so a bare "bash" resolves
# to the WSL launcher and blocks forever on a dead LxssManager: the parent hangs
# in communicate() with a 0-CPU child. A test that hangs is worse than a test
# that fails, because it takes the whole suite with it.
from _runtime_bash import BASH  # noqa: E402

# execution-diary.py is hyphen-named, so it cannot be a normal import; load it
# via importlib (the pattern proven in test_rb_entry_type_taxonomy_sync.py).
# No os.environ mutation here on purpose — the sibling stashes MIND_WORLD only
# because ITS target bootstraps world resolution at import; this one loads
# cleanly against the real environment, and not mutating env means there is
# nothing to leak into other tests in the session (guard-588).
_spec = importlib.util.spec_from_file_location("execution_diary_for_sync", DIARY_PY)
_ed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ed)
VALID_ENTRY_TYPES = set(_ed.VALID_ENTRY_TYPES)


def _emitter_text() -> str:
    assert EMITTER.is_file(), f"emitter not found at {EMITTER}"
    return EMITTER.read_text(encoding="utf-8")


def _emitter_recognized() -> tuple[set[str], set[str]]:
    """(types matched by `et == "..."` branches, types inside the `et in (...)` tuple).

    Returned separately rather than pre-unioned so the positive control can
    assert BOTH shapes were actually found — a union hides which half matched.
    """
    text = _emitter_text()
    branch = set(re.findall(r'et\s*==\s*"([a-z_]+)"', text))
    m = re.search(r"et\s+in\s+\(([^)]*)\)", text, re.S)
    tup = set(re.findall(r'"([a-z_]+)"', m.group(1))) if m else set()
    return branch, tup


def test_emitter_covers_every_valid_entry_type():
    """OUTCOME 1: adding a type to VALID_ENTRY_TYPES without updating the
    emitter fails here. Both directions are checked — an emitter that
    recognizes a type the diary no longer defines is drift too, in the
    direction that leaves dead branches behind."""
    branch, tup = _emitter_recognized()
    recognized = branch | tup
    missing = VALID_ENTRY_TYPES - recognized
    extra = recognized - VALID_ENTRY_TYPES
    assert not missing, (
        f"live-phase-emit.sh does NOT recognize {sorted(missing)}, which "
        f"execution-diary.py accepts. Every heartbeat whose diary tail carries "
        f"one of these will hit the terminating sys.exit and write no "
        f"live_phase — silently, because heartbeat-tick.sh guards the call with "
        f"`|| true`. Add them to the `et in (...)` tuple in live-phase-emit.sh."
    )
    assert not extra, (
        f"live-phase-emit.sh recognizes {sorted(extra)}, which execution-diary.py "
        f"does not define. Either the type was removed from VALID_ENTRY_TYPES and "
        f"the emitter kept a dead branch, or it was misspelled here."
    )


def test_parser_actually_matched_both_shapes():
    """POSITIVE CONTROL for assertion 1 (guard-2298). The set comparison above
    is only meaningful if this file actually parsed something. If the emitter's
    shape changed so that neither regex matched, and VALID_ENTRY_TYPES were
    likewise empty, set-equality would pass while measuring nothing."""
    branch, tup = _emitter_recognized()
    assert VALID_ENTRY_TYPES, "VALID_ENTRY_TYPES imported EMPTY — nothing is being pinned"
    assert branch, (
        "parsed NO `et == \"...\"` branches out of live-phase-emit.sh. The "
        "emitter's dispatch shape changed; this test is measuring nothing and "
        "its green is meaningless until the parser is updated."
    )
    assert tup, (
        "parsed NO `et in (...)` membership tuple out of live-phase-emit.sh. "
        "Same failure as above — a green set-comparison here would be vacuous."
    )
    # The two shapes must be disjoint: a type handled by a dedicated branch AND
    # listed in the fall-through tuple is unreachable in the tuple, which is a
    # sign the two halves have drifted apart rather than being kept in sync.
    assert not (branch & tup), (
        f"{sorted(branch & tup)} appear BOTH as a dedicated `et == ...` branch "
        f"and inside the fall-through tuple; the tuple entry is dead code."
    )


def test_terminating_else_still_exits():
    """OUTCOME 2: removing the terminating else-branch sys.exit fails here.

    The emitter's file-level CRITICAL INVARIANT forbids unrecognized-entry_type
    masking. A repair that turned the `else` into a catch-all would satisfy
    assertion 1 more easily while destroying the property that makes assertion 1
    worth having — so this is pinned separately rather than folded in."""
    text = _emitter_text()
    assert re.search(r"^else:\s*$", text, re.M), (
        "the terminating `else:` is gone from live-phase-emit.sh's dispatch"
    )
    assert re.search(r"^else:\s*\n\s*sys\.exit\(", text, re.M), (
        "live-phase-emit.sh's terminating `else` no longer calls sys.exit. An "
        "unrecognized entry_type must CRASH so stderr surfaces it — the file's "
        "own CRITICAL INVARIANT forbids masking it with a fallback, and "
        "heartbeat-tick.sh's `|| true` is the single sanctioned fail-open "
        "boundary."
    )


def test_emitter_parses_under_bash_n():
    """OUTCOME 3: a parse break in the emitter fails here.

    Run against the REAL file, never an extracted copy: the entire python
    dispatch lives inside a bash single-quoted string, so a single apostrophe
    anywhere in it breaks parsing (guard-504), and a harness that extracts the
    logic cannot reproduce that failure mode (rb-5486)."""
    proc = subprocess.run([BASH, "-n", str(EMITTER)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, (
        f"bash -n failed on {EMITTER.name} (rc={proc.returncode}). The dispatch "
        f"lives inside a single-quoted string — check for a stray apostrophe "
        f"(guard-504).\nstderr: {proc.stderr.strip()[:400]}"
    )
