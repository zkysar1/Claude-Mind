"""test_directive_mix_focus_field_scope.py -- .

Pins that the OBSERVABILITY path (directive_mix_check.focus_aspirations) and
the SCORING path (goal-selector.py load_strategic_focus) extract the SAME
aspiration set from one strategic_focus record, because both must scan the
same FIELDS -- `primary` + `secondary` -- and not merely use the same regex.

THE DEFECT THIS PINS (measured 2026-09-20, bravo, cc-05, Linux
6.8.0-139-generic, through build() over 3287 live goals, changing only the
scanned field). focus_aspirations did `json.dumps(sf)` and therefore scanned
the WHOLE strategic_focus dict, including `rationale`. load_strategic_focus
joins only `primary` + `secondary`. A directive's rationale necessarily names
the lanes it moved work OUT of and the lanes it explicitly DEBOOSTED, so those
ids were read back as in-lane:

    whole-dict scan   -> 10 lanes, 251/335 closes on-directive (74.9%), eligible 2514
    primary+secondary ->  6 lanes, 114/335 closes on-directive (34.0%), eligible   13

The 4 phantom lanes were asp-115/asp-353/asp-363/asp-374 -- precisely the ones
the prose names as NOT boosted. asp-115 is the FRAMEWORK lane and carried 128
of the 137 phantom closes, so framework work was scoring as compliance with a
PRODUCT-focus directive: the exact inversion the module's own WHY cites as its
reason to exist ("a 69/11 inversion sat unnoticed for 22 days").

WHAT WOULD HAVE CAUGHT IT, AND WHAT WOULD NOT. A test driving
focus_aspirations ALONE passes in the defective state -- it returned a
well-formed sorted list of real aspiration ids either way, and nothing about
its output announced which fields produced it. The bug lived in the
RELATIONSHIP between two consumers of one source, so `test_agreement` below
drives BOTH over the SAME record and asserts on the pair. (Same shape, and the
same reason, as test_goal_selector_directive_shared_predicate.py.)

`test_rationale_only_id_is_not_returned` is the targeted regression: it is the
minimal fixture that fails before the fix and passes after, and it is the one
that keeps failing if someone later "simplifies" the join back to json.dumps.

Daemon-safe (no daemon_integration marker): every fixture is an in-memory dict
and neither function performs I/O on the paths under test -- focus_aspirations
takes the team_state mapping as an argument, and the goal-selector side is
exercised through its module-level regex + field list, never its team-state
cache. No daemon spawns.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))


def _load(alias, filename):
    path = CORE_SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(alias, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dmc = _load("directive_mix_check_ffs", "directive_mix_check.py")
gs = _load("goal_selector_ffs", "goal-selector.py")

# The FIELDS the scoring path joins (goal-selector.py:4386-4389). These ARE
# retyped -- goal-selector holds them inline in load_strategic_focus rather
# than as a module constant, so there is nothing importable to bind to.
#
# ⚠ AND THAT MEANS NO TEST HERE CAN CATCH A GOAL-SELECTOR-SIDE FIELD CHANGE.
# An earlier draft of this comment claimed test_agreement... would fail if this
# tuple fell behind. It would not: BOTH sides of that comparison would be stale
# together (this tuple, and focus_aspirations' own inline pair), so the
# agreement assertion passes while both diverge from the scorer. The check
# below is the only real detector -- it reads goal-selector's SOURCE for the
# field names, so it reddens when that side gains or renames one.
SCORING_FIELDS = ("primary", "secondary")


def test_scoring_field_list_has_not_drifted():
    """Red-flag if goal-selector's join gains/renames a field.

    Everything else in this file compares two extractions that BOTH use
    SCORING_FIELDS, so a stale tuple is invisible to them. This reads the
    other side's source text instead.
    """
    src = (CORE_SCRIPTS / "goal-selector.py").read_text(encoding="utf-8")
    marker = 'for k in ("primary", "secondary")'
    assert marker in src, (
        "goal-selector.py no longer joins exactly "
        f'{SCORING_FIELDS} in load_strategic_focus -- update SCORING_FIELDS '
        "here AND focus_aspirations' inline tuple, or the compliance surface "
        "silently diverges from the boost again (g-115-10389).")


def _scoring_side(sf):
    """Reproduce load_strategic_focus's extraction over an explicit record.

    load_strategic_focus() reads the live team-state through a module-level
    cache and memoises into a module global, so it cannot be pointed at a
    fixture. Its actual matcher IS imported from the module under test
    (_STRATEGIC_FOCUS_ASP_RE), so the only thing restated here is the field
    join -- the single line this test exists to keep in sync.

    Note the two regexes differ in shape and must not be swapped: the
    goal-selector one has NO capture group and yields full 'asp-NNN' strings,
    while directive_mix_check's ASP_RE captures the digits and its caller
    re-prefixes. Comparing the two extractions is the point of this test, so
    each side is normalised to the same 'asp-NNN' form here.
    """
    text = " ".join(str(sf.get(k) or "") for k in SCORING_FIELDS).strip()
    return sorted(set(gs._STRATEGIC_FOCUS_ASP_RE.findall(text)))


def _record(primary, rationale="", secondary=None):
    sf = {"primary": primary, "rationale": rationale, "set_by": "zachary"}
    if secondary is not None:
        sf["secondary"] = secondary
    return {"strategic_focus": sf}


def test_rationale_only_id_is_not_returned():
    """The targeted regression: fails before the fix, passes after."""
    ts = _record(
        primary="BOOSTED LANES: asp-369 and asp-373.",
        rationale=("asp-363 LEAVES the boost set today; 3 monitors were "
                   "re-homed to asp-115 and the follow-on lane is asp-374."),
    )
    got, _ = dmc.focus_aspirations(ts)
    assert got == ["asp-369", "asp-373"], got
    for phantom in ("asp-115", "asp-363", "asp-374"):
        assert phantom not in got, f"{phantom} leaked in from rationale"


def test_agreement_between_the_two_consumers():
    """Both consumers must name the SAME lanes for one record."""
    ts = _record(
        primary="the four CLOSING LANES -- asp-368, asp-364, asp-369, asp-373 "
                "-- plus the cost pair asp-372 + asp-358.",
        rationale="asp-363 removed; asp-374 parked; monitors to asp-115/asp-353.",
    )
    observability, _ = dmc.focus_aspirations(ts)
    scoring = _scoring_side(ts["strategic_focus"])
    assert observability == scoring, (
        f"consumers disagree: surface={observability} scoring={scoring}")
    assert observability == ["asp-358", "asp-364", "asp-368", "asp-369",
                            "asp-372", "asp-373"], observability


def test_secondary_is_scanned_when_present():
    """`secondary` is optional but IS part of the boost set where it exists."""
    ts = _record(primary="asp-369 is the lane.", rationale="asp-115 noise.",
                 secondary="Also asp-373.")
    got, _ = dmc.focus_aspirations(ts)
    assert got == ["asp-369", "asp-373"], got
    assert got == _scoring_side(ts["strategic_focus"])


def test_plain_string_directive_still_works():
    """A deployment whose strategic_focus is a bare string is unchanged."""
    got, text = dmc.focus_aspirations({"strategic_focus": "work asp-335 now"})
    assert got == ["asp-335"], got
    assert text == "work asp-335 now"


def test_absent_and_empty_directive_are_inert():
    assert dmc.focus_aspirations({}) == ([], "")
    assert dmc.focus_aspirations({"strategic_focus": None}) == ([], "")
    assert dmc.focus_aspirations(None) == ([], "")


def test_directive_naming_no_aspiration_returns_empty_not_crash():
    """Prose that names no lane yields no boost target -- and no exception."""
    ts = _record(primary="Ship the product.", rationale="asp-115 was noisy.")
    got, text = dmc.focus_aspirations(ts)
    assert got == []
    assert "Ship the product." in text
