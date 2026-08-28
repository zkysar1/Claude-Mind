""" — wm-append.sh must tell its caller WHERE the slot landed.

THE DEFECT: `wm-append.sh <slot>` routes some slots to the YAML TOP LEVEL
(the seven names in TOP_LEVEL_KEYS) and every other slot under the `slots:`
mapping, and nothing at the call site said which. Reading the wrong level
returns a clean, plausible `0` that is byte-identical to the answer for "this
slot is empty", so a caller who guesses wrong gets NO error to notice.

Measured in ONE session, in BOTH directions:
  * `spark_capture` / `encoding_capture` / `exp_capture` / `hyp_capture` live
    UNDER `slots:` — a read of the top level returned 0 immediately after
    wm-append had reported success AND an eviction.
  * `goals_completed_this_session` lives at the TOP LEVEL with 135+ entries —
    a read under `slots:` returned 0 immediately after a successful append.
Both times rc=0 from the writer was correct and the READER was wrong; both
times the wrong answer was a clean, plausible ZERO.

Placement is not cosmetic routing. wm-prune evicts top-level slots only and
never descends into a slot's interior (guard-1544), so the same field also
decides whether an entry can be evicted out from under its writer.

THE FIX IS TWO-COMPONENT, which is what makes the wiring test below the
load-bearing one:
  * PRODUCER — mind_api/src/endpoints/wm_write.py::append_slot returns
    `placement` ("top-level" | "slots"), derived from _resolve_slot's own
    routing decision rather than a restated copy of TOP_LEVEL_KEYS.
  * CONSUMER — this wrapper greps that field out of the response and prints a
    line to stderr. "A fix is not shipped when the producer emits it; it is
    shipped when a consumer displays it" (wm-append.sh's own comment, g-115-6541
    — where `evicted` had been emitted by the daemon and displayed by nobody for
    its entire existence).
A rename on either side re-creates exactly that silence, with every test that
checks only one side still green. test_placement_field_name_is_wired_end_to_end
reads the key from the DAEMON SOURCE at runtime (guard-1220).

HERMETIC BY CONSTRUCTION — no daemon, no network, no working-memory write. Each
case runs a COPY of the production wrapper (copied at test time, so it can never
drift from the real file) inside a tmp project root whose `_runtime.sh` is a
stub returning a canned response. Harness borrowed from
test_wm_append_arg_parser.py, which pins the ARG LOOP; this file pins the
NOTICE. Neither covers the other.

Run:
  STORAGE_BACKEND=local python3 -m pytest core/scripts/tests/test_wm_append_placement_notice.py -q
"""
from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _bash_helpers import BASH  # noqa: E402

CORE_ROOT = SCRIPT_DIR.parent.parent
PROJECT_ROOT = CORE_ROOT.parent
WRAPPER = CORE_ROOT / "scripts" / "wm-append.sh"
DAEMON = PROJECT_ROOT / "mind_api" / "src" / "endpoints" / "wm_write.py"


def _stub(response: str) -> str:
    """A _runtime.sh stub whose rt_call returns exactly `response`."""
    return (
        "rt_url_encode() { printf '%s' \"$1\"; }\n"
        "rt_call() {\n"
        "    while [ $# -gt 0 ]; do shift; done\n"
        "    printf '%s' " + json.dumps(response) + "\n"
        "    return 0\n"
        "}\n"
        "rt_try_autospawn() { return 1; }\n"
        "rt_no_daemon_error() { echo 'stub: no daemon' >&2; exit 1; }\n"
    )


def _sandbox(tmp_path: Path, response: str, mutate: bool = False) -> Path:
    scripts = tmp_path / "core" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    shutil.copy2(WRAPPER, scripts / "wm-append.sh")
    (scripts / "_runtime.sh").write_text(_stub(response), encoding="utf-8")
    if mutate:
        # Positive control: delete the placement `case` block from the copy.
        # Without this, an assertion that merely observes "no line for a null
        # placement" would pass just as happily against a wrapper that never
        # prints a placement line at all (guard-2421 / guard-4166 — a fix whose
        # effect is that something APPEARS still needs the absence proven).
        src = (scripts / "wm-append.sh").read_text(encoding="utf-8")
        start = src.index('    case "$_p" in')
        end = src.index("esac", start) + len("esac\n")
        src = src[:start] + src[end:]
        (scripts / "wm-append.sh").write_text(src, encoding="utf-8")
    return scripts


def _run(scripts: Path, slot: str = "spark_capture"):
    proc = subprocess.run(
        [BASH, (scripts / "wm-append.sh").as_posix(), slot],
        input='{"observation": "x"}', text=True, capture_output=True, timeout=60,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(scripts.parent.parent)},
    )
    return proc


# --- (a) both placements are reported, and each names the OTHER level --------

def test_top_level_placement_is_reported(tmp_path):
    scripts = _sandbox(tmp_path, '{"ok": true, "evicted": 0, "placement": "top-level"}')
    proc = _run(scripts, "goals_completed_this_session")
    assert proc.returncode == 0, proc.stderr
    assert "TOP LEVEL" in proc.stderr, proc.stderr
    assert "goals_completed_this_session" in proc.stderr, proc.stderr
    # Naming only the level it IS leaves the reader to infer the level it is
    # NOT, which is the inference that produced the clean wrong zero.
    assert "slots:" in proc.stderr, (
        f"the notice must name the level a hand-rolled read would wrongly try: "
        f"{proc.stderr!r}")


def test_slots_placement_is_reported(tmp_path):
    scripts = _sandbox(tmp_path, '{"ok": true, "evicted": 0, "placement": "slots"}')
    proc = _run(scripts, "spark_capture")
    assert proc.returncode == 0, proc.stderr
    assert "under slots:" in proc.stderr, proc.stderr
    assert "spark_capture" in proc.stderr, proc.stderr
    assert "top level" in proc.stderr, (
        f"the notice must name the level a hand-rolled read would wrongly try: "
        f"{proc.stderr!r}")


def test_the_two_notices_are_distinguishable(tmp_path):
    """A single notice reused for both placements would answer nothing."""
    top = _run(_sandbox(tmp_path / "a",
                        '{"ok": true, "evicted": 0, "placement": "top-level"}')).stderr
    slots = _run(_sandbox(tmp_path / "b",
                          '{"ok": true, "evicted": 0, "placement": "slots"}')).stderr
    assert top.strip() and slots.strip()
    assert top != slots


# --- (b) the three non-placement shapes stay silent --------------------------

@pytest.mark.parametrize("resp", [
    '{"ok": true, "evicted": 0, "placement": null}',   # resolver never ran
    '{"ok": true, "evicted": 0}',                      # field absent entirely
    '{"ok": true}',                                    # pre-fix daemon
])
def test_absent_or_null_placement_prints_nothing(tmp_path, resp):
    """null is a THIRD fact — "the resolver never ran" — not a placement.
    Printing a guess there would be the defect with extra steps."""
    scripts = _sandbox(tmp_path, resp)
    proc = _run(scripts)
    assert proc.returncode == 0, proc.stderr
    assert "lives at" not in proc.stderr and "lives under" not in proc.stderr, proc.stderr


def test_quiet_path_still_exits_zero(tmp_path):
    """set -e regression: the wrapper's own comment records that a trailing `&&`
    list whose final test fails aborts before `exit 0`, turning a successful
    append into a failed one on the COMMON path. The placement branch must not
    re-introduce it."""
    scripts = _sandbox(tmp_path, '{"ok": true, "evicted": 0}')
    proc = _run(scripts)
    assert proc.returncode == 0, (
        f"quiet path must exit 0; got rc={proc.returncode} stderr={proc.stderr!r}")


# --- (c) positive control: the assertions above can go RED -------------------

def test_mutation_control_removing_the_case_block_kills_the_notice(tmp_path):
    scripts = _sandbox(tmp_path, '{"ok": true, "evicted": 0, "placement": "slots"}',
                       mutate=True)
    proc = _run(scripts)
    assert proc.returncode == 0
    assert "lives under" not in proc.stderr, (
        "the mutant still printed a placement notice — the tests above are "
        "vacuous and prove nothing")


# --- (d) THE WIRING TEST (guard-1220) ----------------------------------------

def test_placement_field_name_is_wired_end_to_end():
    """The producer and the consumer must agree on the JSON key, and the
    expectation is read from the PRODUCER at runtime rather than restated here.

    This is the one failure a single-sided test cannot see: rename the key in
    wm_write.py and the wrapper's sed quietly matches nothing, so every append
    goes back to saying nothing about placement while both components' own
    tests stay green. That is byte-for-byte the g-115-6541 defect this fix
    exists to avoid repeating (`evicted` emitted by the daemon, displayed by
    nobody, for its entire existence).
    """
    daemon_src = DAEMON.read_text(encoding="utf-8")
    # The key as the PRODUCER writes it, from the response-builder literal.
    emitted = set(re.findall(r'"(\w+)":\s*_placement', daemon_src))
    assert emitted, (
        "wm_write.py no longer emits _placement in any response literal — if the "
        "producer was removed, remove the consumer branch in wm-append.sh too")
    # The key as the CONSUMER greps it.
    wrapper_src = WRAPPER.read_text(encoding="utf-8")
    grepped = set(re.findall(r'\.\*"(\w+)"\[\[:space:\]\]\*:', wrapper_src))
    assert emitted <= grepped, (
        f"key drift: wm_write.py emits {sorted(emitted)} but wm-append.sh greps "
        f"{sorted(grepped)} — the wrapper will print nothing and no other test "
        f"will notice (guard-1220)")


def test_placement_is_derived_not_restated():
    """The producer must read its answer from _resolve_slot's routing decision.

    A second copy of TOP_LEVEL_KEYS inside append_slot would drift from the real
    one, and the drift would be INVISIBLE because both answers still look like
    valid placements — no exception, no empty result, just a confidently wrong
    label. So pin the derivation, not the value.
    """
    src = DAEMON.read_text(encoding="utf-8")
    start = src.index("def append_slot(ctx)")
    end = re.compile(r"^def \w+", re.M).search(src, start + 10).start()
    body = src[start:end]
    assert '_placement = "top-level" if is_top else "slots"' in body, (
        "append_slot no longer derives placement from _resolve_slot's is_top")
    # AST, not a substring scan over the source text: the first version of this
    # assertion matched the WORD "TOP_LEVEL_KEYS" inside the explanatory comment
    # that says the set is deliberately NOT consulted here — i.e. it went red on
    # correct code because the code documented itself. Parse for the IDENTIFIER
    # so comments and docstrings cannot trip it.
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "append_slot")
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    assert "TOP_LEVEL_KEYS" not in names, (
        "append_slot now consults TOP_LEVEL_KEYS directly — placement must come "
        "from _resolve_slot's routing decision, which is the thing that actually "
        "routes the write")
