"""test_goal_id_five_digit_seq.py: pins the five-digit goal-id sequence widening.

Background (2026-09-15): asp-115 reached g-115-9999. Every goal-id regex that
bounded the sequence at \\d{2,4} or \\d{1,4} then failed in one of three ways:

  1. REFUSED   The validators rejected g-115-10000, so no goal could be filed
               to asp-115 on any box.
  2. WRONG ID  The unanchored mint parse `re.match(r"^g-\\d{3}-(\\d{2,4})", gid)`
               read g-115-10000 as seq 1000. The max stayed 9999, so the NEXT
               mint would have been g-115-10000 again: a duplicate goal id
               (guard-2414). This is the reason the validator alone could
               never be widened.
  3. SILENT    \\b-anchored extractors returned nothing, and unanchored
               captures returned a different goal (g-115-1000).

45c7bc2475 (g-306-486) widened all of these to five digits. Its tests adjusted
existing fixtures but added no behavioural pin, and its own note records that
core/scripts/user-signal-refresh.py had no test referencing it. This file adds
those pins.

THE FILE NAME IS HISTORY, NOT THE CONTRACT (g-115-10003, 2026-09-21). The
contract is now that the SEQUENCE group carries NO UPPER BOUND AT ALL -- the
name is kept only because the module is cited by name in the allocator's own
width warning and in the goal record, and renaming would cost those pointers.

Why the bound went away rather than up. This file's original sweep asked
``upper < 5``, so on the day asp-115 reaches g-115-99999 all 30 sites fail again
and the sweep stays GREEN -- 5 is not less than 5. Worse, its negative-controls
table asserted the five-digit forms as correct-and-expected-quiet, so anyone
copying a sibling copied the bound with a green test agreeing (guard-7263: a
regression test written at the moment a bound is RAISED encodes the new bound as
its floor, and a floor-shaped predicate is green by construction against the
exact recurrence). The predicate now asks ``has ANY upper bound``, which is what
guard-1161 actually requires, and the controls table asserts the five-digit forms
as VIOLATIONS.

The low end went away too, and that was measured rather than assumed: over 5,775
live goal ids (world active + archive, 2026-09-21) the sequence-length histogram
is {2: 2699, 3: 384, 4: 2237, 5: 455} and ZERO carry a 1-digit sequence, because
all three allocators mint ``{seq:02d}``. The old ``\\d{2,N}`` floor was a
transcription of CLAUDE.md prose, not a false-positive filter, so it rejected
nothing real while teaching the bounded shape.
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]
CORE_SCRIPTS = PROJECT_ROOT / "core" / "scripts"
for _p in (str(PROJECT_ROOT), str(CORE_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

FIVE = "g-115-10000"   # the id that broke the FOURTH-digit bound; a sample, never a bound
SIX = "g-115-100000"   # the id that would break a FIFTH-digit bound

# How far past today's data the derivation probes. Not a bound on anything real:
# it only bounds the SEARCH, and a validator that still accepts at this width is
# reported as open-ended. 24 digits is past any plausible counter and still
# instant.
_PROBE_CAP = 24


def _widest_seq_accepted(rx: "re.Pattern[str]", *, asp: str = "115",
                         matcher: str = "match") -> int:
    """Largest sequence WIDTH `rx` accepts for asp-`asp`, or _PROBE_CAP if it
    never refuses (i.e. the pattern is open-ended).

    F-003. The point of deriving this instead of hardcoding a number: a hardcoded
    sample can only ever prove the validator accepts THAT id. It cannot notice
    that the validator was widened past the allocators, which is the coupling
    whose violation is silent -- re.match(r"^g-\\d{3}-(\\d{2,5})", "g-115-100000")
    returns 10000 rather than failing, so max+1 re-mints a LIVE id (guard-2414).
    """
    widest = 0
    for width in range(2, _PROBE_CAP + 1):
        gid = f"g-{asp}-" + "1" + "0" * (width - 1)
        if getattr(rx, matcher)(gid):
            widest = width
        else:
            break
    return widest


def _compile_literal(file_name: str, target: str, flags: int = 0) -> "re.Pattern[str]":
    """Compile the string literal passed to re.compile(...) in `target = re.compile(...)`.

    Read with ast rather than imported: loop-state-save.py reconfigures stdio at
    import time, and drain-encode-probe.py is a CLI. A literal read cannot run
    either one.
    """
    tree = ast.parse((CORE_SCRIPTS / file_name).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == target
                and isinstance(node.value, ast.Call) and node.value.args
                and isinstance(node.value.args[0], ast.Constant)):
            return re.compile(node.value.args[0].value, flags)
    raise AssertionError(f"{file_name}: no `{target} = re.compile(<literal>)` found")


# -- 1. REFUSED: the validators ------------------------------------------------

@pytest.mark.parametrize("gid", ["g-115-9999", FIVE, "g-115-10000-a", "g-115-99999", "g-001-01"])
def test_cli_and_daemon_validators_accept_five_digit_sequences(gid):
    import aspirations
    from mind_api.src.endpoints import aspirations_write

    assert aspirations.GOAL_ID_RE.match(gid), f"CLI GOAL_ID_RE refused {gid}"
    assert aspirations_write._GOAL_ID_RE.match(gid), f"daemon _GOAL_ID_RE refused {gid}"


@pytest.mark.parametrize("bad", ["g-115-", "g-115-12x", "asp-115", "g-115-10000-ab"])
def test_validators_still_refuse_malformed_ids(bad):
    import aspirations
    from mind_api.src.endpoints import aspirations_write

    assert not aspirations.GOAL_ID_RE.match(bad)
    assert not aspirations_write._GOAL_ID_RE.match(bad)


def test_a_one_digit_sequence_is_accepted_and_that_is_deliberate():
    """`` was in the malformed list above until 2026-09-21 ().
    Opening the sequence to \\d+ admits it, and that is the RIGHT trade, measured
    rather than assumed:

    - Nothing produces one. All three allocators mint `{seq:02d}`, and over 5,775
      live goal ids (world active + archive) the sequence-length histogram is
      {2: 2699, 3: 384, 4: 2237, 5: 455} with ZERO at one digit.
    - The two failure directions are not symmetric. A validator that ACCEPTS a
      shape nobody mints costs a lookup miss. A validator that REFUSES a shape the
      allocator DOES mint is a fleet-wide filing outage, and that has now happened
      twice (g-115-999, g-115-9999).
    - guard-1161 forbids the floor for the same reason it forbids the ceiling: a
      digit-count bound on a counter is a dated assumption wearing a regex.

    Pinned as an EXPECTATION, not left as a silent side effect, so a future reader
    who re-adds `\\d{2,}` fails here and reads this instead of guessing."""
    import aspirations
    from mind_api.src.endpoints import aspirations_write

    assert aspirations.GOAL_ID_RE.match("g-115-1")
    assert aspirations_write._GOAL_ID_RE.match("g-115-1")


def test_loop_state_save_accepts_five_digit_goal_id():
    """A selected  must be checkpointable, or the loop cannot save state for it."""
    tree = ast.parse((CORE_SCRIPTS / "loop-state-save.py").read_text(encoding="utf-8"))
    patterns = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, spec in zip(node.keys, node.values):
            if not (isinstance(key, ast.Constant) and key.value == "goal_id" and isinstance(spec, ast.Dict)):
                continue
            for skey, sval in zip(spec.keys, spec.values):
                if isinstance(skey, ast.Constant) and skey.value == "pattern":
                    patterns.append(sval.value)
    assert patterns, "loop-state-save.py: goal_id schema pattern not found"
    for pat in patterns:
        assert re.match(pat, FIVE), f"loop-state-save goal_id pattern {pat!r} refused {FIVE}"


# -- 2. WRONG ID: minting must read the whole sequence ---------------------------

def test_daemon_mint_after_ten_thousand_is_not_a_duplicate():
    """The headline hazard: with  already present, the next mint is
    g-115-10001. With a four-digit mint parse it is g-115-10000 again."""
    from mind_api.src.endpoints import aspirations_write

    at_ceiling = {"id": "asp-115", "goals": [{"id": "g-115-9998"}, {"id": "g-115-9999"}]}
    assert aspirations_write._allocate_goal_id(at_ceiling) == FIVE

    past_ceiling = {"id": "asp-115", "goals": [{"id": "g-115-9999"}, {"id": FIVE}]}
    assert aspirations_write._allocate_goal_id(past_ceiling) == "g-115-10001"


# -- 3. SILENT: extractors and derivations ---------------------------------------

def test_experience_goal_id_derive_both_twins():
    import experience
    from mind_api.src.endpoints import experience_write

    for mod in (experience, experience_write):
        m = mod.GOAL_ID_IN_EXP_ID_RE.match(f"exp-{FIVE}-close")
        assert m and m.group(1) == FIVE, f"{mod.__name__} derived {m and m.group(1)!r}"


def test_dependency_graph_sees_a_five_digit_dependency():
    rx = _compile_literal("_dependency_graph.py", "_GOAL_ID_RE")
    assert rx.findall(f"blocked_by {FIVE} and g-115-9999") == [FIVE, "g-115-9999"]


def test_front_matter_capture_is_not_a_different_goal():
    rx = _compile_literal("drain-encode-probe.py", "_FM_GOAL_ID_RE", re.M)
    assert rx.findall(f"goal_id: {FIVE}\n") == [FIVE]


def test_user_signal_refresh_extracts_whole_ids_with_no_floor():
    """Was `..._and_keeps_its_floor` until 2026-09-21. The floor is gone (see
    test_a_one_digit_sequence_is_accepted_and_that_is_deliberate); what this
    surface must still guarantee is that a long id comes back WHOLE and not as a
    truncated prefix, which is the failure that actually costs something."""
    rx = _compile_literal("user-signal-refresh.py", "GOAL_ID_RE")
    assert rx.findall(f"see {SIX}, {FIVE}, g-115-01 and g-115-1") == [
        SIX, FIVE, "g-115-01", "g-115-1"]


# -- Static sweep: no sequence quantifier below five digits on any sibling ------

_DIGIT = r"(?:\\d|\[0-9\])"
# g-<asp part>-<sequence quantifier>. These spellings all expose the sequence
# group's {n} / {n,m} / {n,} to the capture:
#   g-\d{3}-\d{2,4}          g-(\d{3})-\d{2,4}
#   g-(?:\d{3}-\d{2,4}|..    g-\d{3}-(\d{2,4})
#   g-\d{1,4}-\d{1,4}        [0-9]{..}
# The left guard admits `\bg-` but not `msg-\d{8}-\d{6}`: board message ids share
# the g-<digits>-<digits> tail and are not goal ids.
_SEQ_QUANTIFIER = re.compile(
    r"(?:(?<![A-Za-z])|(?<=\\b))g-\(?(?:\?:)?" + _DIGIT
    + r"(?:\{\d+(?:,\d*)?\}|\+)?\)?-\(?" + _DIGIT + r"\{(\d+)(?:(,)(\d*))?\}"
)


def _seq_is_bounded(line: str) -> bool:
    """True when a goal-id sequence quantifier on `line` carries ANY upper bound.

    guard-1161's actual predicate. The previous one asked ``upper < 5``, which is
    green by construction against the very recurrence it was written for: at
    g-115-99999 every five-digit site breaks and ``5 < 5`` is False (guard-7263).
    ``{n,}`` -- a floor with no ceiling -- is NOT a violation here: it cannot
    expire. It is separately unnecessary for goal ids (see the module docstring's
    5,775-id histogram) but it is not this sweep's business.
    """
    for m in _SEQ_QUANTIFIER.finditer(line):
        low, comma, high = m.group(1), m.group(2), m.group(3)
        upper = int(low) if not comma else (int(high) if high else None)
        if upper is not None:
            return True
    return False


@pytest.mark.parametrize("line,bounded", [
    # -- four-digit era: the bound that expired at  -------------------
    (r'GOAL_ID_RE = re.compile(r"^g-(\d{3}-\d{2,4}(-[a-z])?|xw-\d{8}T\d{6}-\d{2})$")', True),
    (r'        m = re.match(r"^g-\d{3}-(\d{2,4})", gid)', True),
    (r'    m = re.match(r"^g-(\d{3})-\d{2,4}(?:-[a-z])?$", goal_id)', True),
    (r'_GOAL_ID_RE = re.compile(r"\bg-\d{1,4}-\d{1,4}\b")', True),
    (r"    pattern: 'g-\d{3}-\d{2,4}'", True),
    (r"grep -oE 'g-[0-9]{3}-[0-9]{2,4}'", True),
    # -- five-digit era: ALSO violations now, and this flip IS the fix ----------
    # These six rows asserted False until 2026-09-21. A reader copying a sibling
    # copied the bound and this table agreed with them (guard-7263).
    (r'GOAL_ID_RE = re.compile(r"^g-(\d{3}-\d{2,5}(-[a-z])?|xw-\d{8}T\d{6}-\d{2})$")', True),
    (r'        m = re.match(r"^g-\d{3}-(\d{2,5})", gid)', True),
    (r'_GOAL_ID_RE = re.compile(r"\bg-\d{1,4}-\d{1,5}\b")', True),
    (r"    pattern: 'g-\d{3}-\d{2,5}'", True),
    (r'_GOAL_RE = re.compile(r"^g-\d{3,}-\d{2,5}$")', True),
    (r'_ID = re.compile(r"\bg-\d{3}-\d{1,5}\b")', True),
    # -- the only shapes that may stay quiet: no upper bound on the sequence ----
    (r'GOAL_ID_RE = re.compile(r"^g-(\d{3}-\d+(-[a-z])?|xw-\d{8}T\d{6}-\d{2})$")', False),
    (r'        m = re.match(r"^g-\d{3}-(\d+)", gid)', False),
    (r'_GOAL_ID_RE = re.compile(r"\bg-\d{1,4}-\d+\b")', False),
    (r'GOAL_ID_RE = re.compile(r"^g-\d+-\d+$")', False),
    (r'_GOAL_RE = re.compile(r"^g-\d{3,}-\d{2,}$")', False),
    # -- not a goal id at all: a board message id shares the g-<d>-<d> tail -----
    (r'_BOARD_MSG = re.compile(r"\bmsg-\d{8}-\d{6}-[a-z0-9]+-\d+\b")', False),
])
def test_sweep_detector_positive_and_negative_controls(line, bounded):
    """guard-385: a sweep that returns zero proves nothing until it is shown to
    fire on the real pre-widening lines and to stay quiet on the open forms.

    Both eras are kept as positive controls on purpose. The four-digit rows prove
    the detector still fires on the oldest shape; the five-digit rows are the
    ones that were WRONG, and keeping them named prevents the table being quietly
    re-floored at the next widening."""
    assert _seq_is_bounded(line) is bounded


def _sweep_files():
    yield from (p for p in CORE_SCRIPTS.rglob("*.py") if "tests" not in p.relative_to(CORE_SCRIPTS).parts)
    yield from (p for p in CORE_SCRIPTS.rglob("*.sh") if "tests" not in p.relative_to(CORE_SCRIPTS).parts)
    yield from (PROJECT_ROOT / "mind_api" / "src").rglob("*.py")
    yield from (PROJECT_ROOT / "core" / "config").glob("*.yaml")


def test_no_goal_id_sequence_carries_an_upper_bound_in_framework_source():
    hits = []
    for path in _sweep_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue  # a historical note about the old pattern is not a regex
            if _seq_is_bounded(line):
                hits.append(f"{path.relative_to(PROJECT_ROOT)}:{n}: {line.strip()[:120]}")
    assert not hits, (
        "goal-id regex whose SEQUENCE quantifier carries an UPPER BOUND. The "
        "sequence is a growing counter and every bound placed on it has expired "
        "in production -- g-115-999 (2026-05-19) and g-115-9999 (2026-09-15), "
        "each a fleet-wide filing outage. A bound does not fail loudly: it "
        "refuses a legal id, or re-mints a live one from a truncated capture "
        "(guard-2414), or silently misses it. Use \\d+ (guard-1161). The "
        "ASPIRATION group is a different axis and is not swept:\n" + "\n".join(hits))


# -- F-003: the allocator bound must never be narrower than the validator -------

def test_both_validators_are_open_ended_not_merely_wide():
    """A sample id can only ever prove the validator accepts THAT id. This asks
    the pattern where it stops, and the answer must be 'it does not'."""
    import aspirations
    from mind_api.src.endpoints import aspirations_write

    for rx, name in ((aspirations.GOAL_ID_RE, "CLI GOAL_ID_RE"),
                     (aspirations_write._GOAL_ID_RE, "daemon _GOAL_ID_RE")):
        widest = _widest_seq_accepted(rx)
        assert widest == _PROBE_CAP, (
            f"{name} stops accepting at {widest} sequence digits. The sequence is "
            f"a growing counter; a ceiling here is a future fleet-wide filing "
            f"outage (guard-1161)")


def test_allocator_captures_the_widest_id_the_validator_accepts_WHOLE():
    """F-003, behaviourally. The validator says which ids are LEGAL; the allocator
    decides which is NEXT, with its OWN regex, in another function, in another
    file. Reading the allocator's regex would pin the shape; minting against real
    records pins the BEHAVIOUR, which is what actually breaks."""
    from mind_api.src.endpoints import aspirations_write

    for width in range(2, _PROBE_CAP + 1):
        top = int("1" + "0" * (width - 1))
        asp = {"id": "asp-115", "goals": [{"id": f"g-115-{top}"}]}
        got = aspirations_write._allocate_goal_id(asp)
        assert got == f"g-115-{top + 1}", (
            f"at {width} sequence digits the allocator minted {got}, not "
            f"g-115-{top + 1}. A truncated capture does not fail -- it returns a "
            f"SMALLER number, so max+1 re-mints an id that is already live on the "
            f"shared world store (guard-2414)")


def test_a_narrowed_allocator_bound_would_re_mint_a_live_id():
    """NEGATIVE CONTROL for the two tests above (guard-385). Without this, a
    green run cannot distinguish 'the coupling holds' from 'the probe is inert'.

    Both assertions below are the pre-fix behaviour, reproduced deliberately."""
    narrow_parse = re.compile(r"^g-\d{3}-(\d{2,5})")
    assert narrow_parse.match(SIX).group(1) == "10000", (
        "a capped capture on a longer id does not raise -- it silently returns "
        "the leading digits as a smaller number. That is the whole hazard.")
    assert _widest_seq_accepted(re.compile(r"^g-\d{3}-\d{2,5}$")) == 5, (
        "the derivation helper must actually detect a ceiling, or "
        "test_both_validators_are_open_ended_not_merely_wide passes on anything")


# -- Positive control: a SIX-digit id validates, allocates and round-trips ------

def test_six_digit_id_validates_allocates_and_round_trips():
    """The id one order of magnitude past today's ceiling, through every surface
    the five-digit tests above cover. This is the check the goal asked for, and
    it is what a hardcoded FIVE could never answer."""
    import aspirations
    import experience
    from mind_api.src.endpoints import aspirations_write, experience_write

    # validators, both twins
    assert aspirations.GOAL_ID_RE.match(SIX)
    assert aspirations_write._GOAL_ID_RE.match(SIX)

    # allocation past the five-digit ceiling
    asp = {"id": "asp-115", "goals": [{"id": "g-115-99999"}, {"id": SIX}]}
    assert aspirations_write._allocate_goal_id(asp) == "g-115-100001"

    # loop-state checkpoint schema
    tree = ast.parse((CORE_SCRIPTS / "loop-state-save.py").read_text(encoding="utf-8"))
    pats = [sval.value
            for node in ast.walk(tree) if isinstance(node, ast.Dict)
            for key, spec in zip(node.keys, node.values)
            if isinstance(key, ast.Constant) and key.value == "goal_id" and isinstance(spec, ast.Dict)
            for skey, sval in zip(spec.keys, spec.values)
            if isinstance(skey, ast.Constant) and skey.value == "pattern"]
    assert pats and all(re.match(p, SIX) for p in pats)

    # experience-id derivation, both twins
    for mod in (experience, experience_write):
        m = mod.GOAL_ID_IN_EXP_ID_RE.match(f"exp-{SIX}-close")
        assert m and m.group(1) == SIX

    # free-text extractors: the id must come back WHOLE, not as a prefix
    assert _compile_literal("_dependency_graph.py", "_GOAL_ID_RE").findall(
        f"blocked_by {SIX} and g-115-9999") == [SIX, "g-115-9999"]
    assert _compile_literal("drain-encode-probe.py", "_FM_GOAL_ID_RE", re.M).findall(
        f"goal_id: {SIX}\n") == [SIX]
    assert _compile_literal("user-signal-refresh.py", "GOAL_ID_RE").findall(
        f"see {SIX} and g-115-01") == [SIX, "g-115-01"]


# -- F-002: the growth curve is watched ----------------------------------------

@pytest.mark.parametrize("max_seq,new_seq,should_warn", [
    (9999, 10000, True),     # the crossing that caused the 2026-09-15 outage
    (99999, 100000, True),   # the next one -- the alarm this goal exists to add
    (10000, 10001, False),   # same width: silent, or asp-115 warns on every mint
    (99, 100, False),        # 2->3 digits: routine for every young aspiration
    (999, 1000, False),      # 3->4 digits: still routine
    (0, 1, False),           # a brand-new aspiration's first goal
])
def test_width_growth_warning_fires_only_on_a_crossing_that_matters(
        max_seq, new_seq, should_warn, capsys):
    """Warns on the CROSSING only, and only at widths where a bounded sibling
    outside this tree could break. Anything noisier gets muted by its readers."""
    import aspirations

    aspirations._warn_on_seq_width_growth("asp-115", max_seq, new_seq)
    err = capsys.readouterr().err
    assert ("[goal-id-width]" in err) is should_warn, err
    if should_warn:
        assert "asp-115" in err and str(max_seq) in err and str(new_seq) in err


def test_width_growth_warning_is_mirrored_in_the_daemon_twin(capsys):
    """guard-742: the daemon is the live path under no-python-cli-fallback, so a
    CLI-only alarm is an alarm nobody hears."""
    from mind_api.src.endpoints import aspirations_write

    aspirations_write._warn_on_seq_width_growth("asp-115", 99999, 100000)
    assert "[goal-id-width]" in capsys.readouterr().err


def test_width_growth_warning_never_breaks_allocation():
    """guard-1562: stopping a healthy path on a plumbing fault is worse than the
    disease. A warning that can raise turns a cosmetic bug into a filing outage --
    the exact failure class this whole goal is about."""
    import aspirations
    from mind_api.src.endpoints import aspirations_write

    for mod in (aspirations, aspirations_write):
        mod._warn_on_seq_width_growth(None, "not-an-int", object())  # type: ignore[arg-type]
