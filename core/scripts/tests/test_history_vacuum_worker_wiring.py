""": the .history vacuum must have a cadence surface a WORKER reaches.

THE DEFECT. `history-vacuum-tick.sh` had EXACTLY ONE caller in the tree —
`iteration-close.sh`, inside the productivity-check maintenance sink, which a
worker Body skips by contract. `.history` is machine-local BY DESIGN (the tick's
own header: "every box vacuums ITS OWN store"), so no reducer and no partner can
ever GC a worker box's store. Fourth instance of the same structural class as
g-306-233 (workers never pulled), g-306-235 and g-115-7414 (temp janitor).

MEASURED 2026-08-29 from the zakbox1 LXD host: 231 GB of world/.history across 10
Ayoai containers, >=43% of their total container space, against cc-08 — the one
box the vacuum had ever run on — sitting SMALLEST at 15 GB. The mechanism works;
it did not reach the other nine boxes.

WHY THESE ARE STATIC PINS. Running `sessionstart-orchestrator.sh` in a test
spawns the runtime daemon via mind-api-start.sh and hijacks daemon.port out from
under the live fleet — the daemon-storm hazard that
test_context_reads_clear_wiring.py documents and refuses for the same reason. So
the call-site properties are pinned by reading the file, exactly as that sibling
pins its own hook call sites.

WHAT IS *NOT* PINNED HERE, said plainly because a static pin cannot catch an
inert mechanism (guard-1943: pinning the writer says nothing about the wiring —
and the converse holds too): that the tick actually reclaims bytes. That is the
tick's own contract, covered by its own tests, and it was additionally verified
BY HAND on cc-08 during this goal — invoking the tick with a stamp 8h old left
`.vacuum-last-run` unchanged at 2026-08-29T08:07:38 and appended no log line,
confirming the 24h self-gate makes a per-session-start call nearly free.

RESIDUAL, deliberately not fixed here: a worker session that runs >24h WITHOUT
ever restarting or autocompacting would still miss a tick. SessionStart fires on
`compact` as well as startup, so in practice a long session keeps ticking; the
gap is narrow but real, and closing it would mean growing the hot-path-budgeted
worker-loop/SKILL.md.

Run: py -3 -m pytest core/scripts/tests/test_history_vacuum_worker_wiring.py -v
"""
import re
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parents[1]
ORCH = CORE_SCRIPTS / "sessionstart-orchestrator.sh"
ITER_CLOSE = CORE_SCRIPTS / "iteration-close.sh"
TICK = CORE_SCRIPTS / "history-vacuum-tick.sh"

CALL = re.compile(r'^\s*bash\s+"\$SCRIPT_DIR/history-vacuum-tick\.sh".*$', re.M)


def _orch() -> str:
    return ORCH.read_text(encoding="utf-8")


def test_the_tick_script_exists():
    assert TICK.is_file(), "the shared tick this wiring calls must exist"


def test_session_start_has_a_vacuum_call_site():
    """THE defining property. RED before ."""
    assert CALL.search(_orch()), (
        "sessionstart-orchestrator.sh must call history-vacuum-tick.sh — it is "
        "the only cadence surface a worker Body reaches")


def test_the_call_is_fail_open():
    """Advisory hygiene must never perturb session start: the whole chain is
    `|| true` and a GC tick is the last thing that should break a boot."""
    m = CALL.search(_orch())
    assert m and "|| true" in m.group(0), \
        f"vacuum call must be fail-open, got: {m.group(0) if m else None}"


def test_the_call_fires_on_startup_not_only_on_compact():
    """Source-gating is a structural property of this file (guard-404: compact !=
    startup != resume). Placing the call inside the `source=compact` branch would
    silently halve its cadence, and every other test here would still pass."""
    text = _orch()
    m = CALL.search(text)
    assert m
    gate = text.find('if [ "$SOURCE" = "compact" ]')
    assert gate != -1, "the compact gate moved; this test's premise needs review"
    assert m.start() < gate, \
        "the vacuum call must sit BEFORE the source=compact gate so it fires on startup too"


def test_the_reducer_call_site_still_exists():
    """Anti-vacuity twin (guard-1220). This goal ADDS a second entry point; it
    does not move the first. Without this, deleting the iteration-close call and
    keeping the new one would pass every assertion above while halving coverage
    for the reducer — the opposite of the defect being fixed."""
    assert "history-vacuum-tick.sh" in ITER_CLOSE.read_text(encoding="utf-8"), (
        "iteration-close.sh must KEEP its vacuum call site (guard-3448: a gate is "
        "only as broad as its entry points)")


def test_the_wiring_is_a_scoped_call_not_a_reimplementation():
    """guard-2676 (no-transcription): a second entry point calls the shared
    component. If the orchestrator ever grew its own enumerate/delete logic it
    would drift from the tick's archive-before-delete receipt contract."""
    text = _orch()
    for forbidden in ("history_vacuum_archive", "orphan_blobs", "drop_manifests"):
        assert forbidden not in text, (
            f"sessionstart-orchestrator.sh must not reimplement the vacuum "
            f"(found {forbidden!r}); call history-vacuum-tick.sh instead")
