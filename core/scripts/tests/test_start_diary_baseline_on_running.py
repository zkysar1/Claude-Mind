"""/start appends one execution-diary entry right after the RUNNING flip.

The loop-exhaustion fence's streak is "consecutive turn-ends for this SID
since the execution diary last advanced" (compute_streak anchors on the
diary's mtime). `/stop` then `/start` in ONE terminal keeps the Claude Code
SID, so a restart used to inherit the pre-stop streak: measured 2026-09-25, a
12:20Z restart of a stalled reducer was killed by the `stop` rung at turn-end
#13, most of them counted before the restart. One diary append at /start
resets the anchor.

These pins are source-shaped on purpose: /start is skill pseudocode the model
executes, so the executable contract is (a) the append is there, (b) it comes
AFTER the RUNNING flip and its halt, (c) its entry_type is one the diary
accepts -- a renamed type would make the append fail silently at every /start.
"""
import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "core" / "scripts"
SKILL = ROOT / ".claude" / "skills" / "start" / "SKILL.md"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "execution_diary_startpin", str(SCRIPTS / "execution-diary.py")
)
diary = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(diary)

APPEND_RE = re.compile(
    r'printf \'%s\' \'(\{"entry_type":"(?P<etype>[a-z_]+)"[^\']*\})\' \| '
    r'MIND_AGENT=<agent-name> bash core/scripts/execution-diary\.sh append'
)


def _lines():
    return SKILL.read_text(encoding="utf-8").splitlines()


def _append_hits():
    return [(i, APPEND_RE.search(ln)) for i, ln in enumerate(_lines())
            if APPEND_RE.search(ln)]


def test_start_appends_exactly_one_diary_baseline():
    hits = _append_hits()
    assert len(hits) == 1, (
        f"expected exactly one execution-diary append in start/SKILL.md, found "
        f"{[i for i, _ in hits]}")


def test_append_follows_the_running_flip_and_its_halt():
    lines = _lines()
    flip = [i for i, ln in enumerate(lines)
            if "session-state-set.sh RUNNING`" in ln and ln.lstrip().startswith("- Bash:")]
    assert len(flip) == 1, f"expected one RUNNING flip call, found {flip}"
    halt = [i for i, ln in enumerate(lines) if "HALT ON NON-ZERO EXIT" in ln and i > flip[0]]
    assert halt, "the RUNNING flip lost its HALT block"
    (append_idx, _), = _append_hits()
    assert append_idx > halt[0] > flip[0], (
        "the diary append must come AFTER the flip's halt so a failed flip "
        f"appends nothing (flip={flip[0]}, halt={halt[0]}, append={append_idx})")


def test_append_entry_type_is_one_the_diary_accepts():
    (_, m), = _append_hits()
    etype = m.group("etype")
    assert etype in diary.VALID_ENTRY_TYPES, (
        f"start/SKILL.md appends entry_type {etype!r}, which execution-diary.py "
        f"refuses (valid: {sorted(diary.VALID_ENTRY_TYPES)}) -- the baseline "
        "would fail at every /start")


def test_append_is_non_fatal():
    """The fence HOLDS on an unreadable diary, so the append must never halt /start."""
    (idx, _), = _append_hits()
    assert "|| echo" in _lines()[idx], "the diary baseline append must be non-fatal"
