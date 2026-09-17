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
those pins. It also adds a static sweep that fails if any goal-id sequence
quantifier in framework source falls back BELOW five digits, which catches a
new or missed sibling copied from pre-widening code (guard-1161: sweep the
sibling instances).
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

FIVE = "g-115-10000"


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


@pytest.mark.parametrize("bad", ["g-115-", "g-115-1", "g-115-12x", "asp-115", "g-115-10000-ab"])
def test_validators_still_refuse_malformed_ids(bad):
    import aspirations
    from mind_api.src.endpoints import aspirations_write

    assert not aspirations.GOAL_ID_RE.match(bad)
    assert not aspirations_write._GOAL_ID_RE.match(bad)


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


def test_user_signal_refresh_extracts_five_digit_ids_and_keeps_its_floor():
    rx = _compile_literal("user-signal-refresh.py", "GOAL_ID_RE")
    assert rx.findall(f"see {FIVE}, g-115-01 and g-115-1") == [FIVE, "g-115-01"]


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


def _caps_below_five(line: str) -> bool:
    """True when a goal-id sequence quantifier on `line` has an upper bound under 5."""
    for m in _SEQ_QUANTIFIER.finditer(line):
        low, comma, high = m.group(1), m.group(2), m.group(3)
        upper = int(low) if not comma else (int(high) if high else None)
        if upper is not None and upper < 5:
            return True
    return False


@pytest.mark.parametrize("line,capped", [
    (r'GOAL_ID_RE = re.compile(r"^g-(\d{3}-\d{2,4}(-[a-z])?|xw-\d{8}T\d{6}-\d{2})$")', True),
    (r'        m = re.match(r"^g-\d{3}-(\d{2,4})", gid)', True),
    (r'    m = re.match(r"^g-(\d{3})-\d{2,4}(?:-[a-z])?$", goal_id)', True),
    (r'_GOAL_ID_RE = re.compile(r"\bg-\d{1,4}-\d{1,4}\b")', True),
    (r"    pattern: 'g-\d{3}-\d{2,4}'", True),
    (r"grep -oE 'g-[0-9]{3}-[0-9]{2,4}'", True),
    (r'GOAL_ID_RE = re.compile(r"^g-(\d{3}-\d{2,5}(-[a-z])?|xw-\d{8}T\d{6}-\d{2})$")', False),
    (r'        m = re.match(r"^g-\d{3}-(\d{2,5})", gid)', False),
    (r'_GOAL_ID_RE = re.compile(r"\bg-\d{1,4}-\d{1,5}\b")', False),
    (r'GOAL_ID_RE = re.compile(r"^g-\d+-\d+$")', False),
    (r'_GOAL_RE = re.compile(r"^g-\d{3,}-\d{2,}$")', False),
    (r'_BOARD_MSG = re.compile(r"\bmsg-\d{8}-\d{6}-[a-z0-9]+-\d+\b")', False),
])
def test_sweep_detector_positive_and_negative_controls(line, capped):
    """guard-385: a sweep that returns zero proves nothing until it is shown to
    fire on the real pre-widening lines and to stay quiet on the widened forms."""
    assert _caps_below_five(line) is capped


def _sweep_files():
    yield from (p for p in CORE_SCRIPTS.rglob("*.py") if "tests" not in p.relative_to(CORE_SCRIPTS).parts)
    yield from (p for p in CORE_SCRIPTS.rglob("*.sh") if "tests" not in p.relative_to(CORE_SCRIPTS).parts)
    yield from (PROJECT_ROOT / "mind_api" / "src").rglob("*.py")
    yield from (PROJECT_ROOT / "core" / "config").glob("*.yaml")


def test_no_goal_id_sequence_capped_below_five_digits_in_framework_source():
    hits = []
    for path in _sweep_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue  # a historical note about the old pattern is not a regex
            if _caps_below_five(line):
                hits.append(f"{path.relative_to(PROJECT_ROOT)}:{n}: {line.strip()[:120]}")
    assert not hits, (
        "goal-id regex whose SEQUENCE quantifier caps below five digits. asp-115 is "
        "past g-115-9999, so a four-digit cap refuses, re-mints or silently misses "
        "live ids (guard-1161, 45c7bc2475):\n" + "\n".join(hits))
