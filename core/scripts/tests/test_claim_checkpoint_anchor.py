"""Behavioral tests for the iteration-checkpoint anchor folded into
`aspirations-claim.sh::_post_claim_effects` (g-115-3590).

WHAT IS BEING PINNED. Checkpoint CREATION used to have exactly one executable
call site in the repo — `aspirations-select/SKILL.md` Phase 2.95 — while
DELETION is bash-enforced (`iteration-close.sh` `rm -f`). A loop that selects by
calling `goal-selector.sh` directly instead of `Skill(aspirations-select)` never
anchors, so the checkpoint stays absent for the rest of the session and every
downstream reader degrades silently and fail-open. Measured on cc-04 before the
fix: 101 `update_against_missing_checkpoint` rows in
`agents/<agent>/session/checkpoint-miss.jsonl`.

WHY A STUBBED HARNESS AND NOT AN END-TO-END CLAIM. `_post_claim_effects` runs
only on the rc=0 daemon-claim path, so a fake goal id (the technique
`test_aspirations_claim_source_flag.sh` uses to test arg parsing without a
daemon) never reaches it. Instead the real wrapper is copied into a tmp tree
whose `core/scripts/` holds stubs for its five dependencies, and `rt_call` is
stubbed to return a canned successful claim. The function body under test is the
REAL one, byte-for-byte — only its collaborators are stubs.

COVERAGE SHAPE (guard-1451 / guard-1660). Sensitivity: `test_mutation_*` strips
the anchor block from a copy and requires the allow-case to go red, so a passing
suite is not a tautology. Specificity: `test_existing_anchor_for_same_goal_is_preserved`
requires the anchor NOT to be written when a richer one already exists — a
write-always implementation fails it. Both directions are permanent tests, not a
one-time manual proof.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _bash_helpers import BASH  # noqa: E402

CORE_SCRIPTS = SCRIPT_DIR.parent
WRAPPER = CORE_SCRIPTS / "aspirations-claim.sh"

# Matches the repo's own python-invocation convention (CLAUDE.md § Python
# Invocation): `py -3` on Windows, `python3` elsewhere. Deliberately NOT
# sys.executable — that can contain spaces, and the wrapper word-splits
# $(rt_python_launcher) on purpose.
LAUNCHER = "py -3" if sys.platform == "win32" else "python3"

# The anchor block's guard line. The mutation harness locates the block by this
# sentinel; if the block is reformatted, the mutation test fails loudly rather
# than silently passing on an unmutated file.
_GUARD_SENTINEL = '[ -n "$asp_num" ]'


def _canned_claim(goal_id: str, *, claimed_by: str = "alpha", title: str = "T") -> str:
    return json.dumps({"ok": True,
                       "goal": {"id": goal_id, "title": title,
                                "claimed_by": claimed_by}})


def _build_tree(tmp_path: Path, wrapper_text: str) -> Path:
    """A tmp PROJECT_ROOT whose core/scripts holds the real wrapper plus stubs.

    The wrapper resolves PROJECT_ROOT as `dirname($0)/../..`, so placing the
    copy at <tmp>/core/scripts/ gives it CORE_ROOT=<tmp>/core and it looks for
    every collaborator beside itself.
    """
    scripts = tmp_path / "core" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "aspirations-claim.sh").write_text(wrapper_text, encoding="utf-8",
                                                  newline="")

    # Arg normalizer: sourced with GOAL_NORMALIZE_TARGET=positional. No-op here
    # — these tests pass a positional goal id already.
    (scripts / "_goal-arg-normalize.sh").write_text(":\n", encoding="utf-8",
                                                    newline="")

    # _runtime.sh stub. rt_call returns the canned claim on stdout, rc=0.
    (scripts / "_runtime.sh").write_text(
        "rt_python_launcher() { printf '%s' \"${STUB_LAUNCHER}\"; }\n"
        "rt_url_encode() { printf '%s' \"$1\"; }\n"
        "rt_call() { printf '%s' \"${STUB_RESPONSE}\"; return \"${STUB_RC:-0}\"; }\n"
        "rt_try_autospawn() { return 1; }\n"
        "rt_no_daemon_error() { echo \"no daemon: $1\" >&2; exit 1; }\n",
        encoding="utf-8", newline="")

    # Scorer-sovereignty gate: allow (any non-2 rc proceeds).
    (scripts / "scorer-verdict-gate.py").write_text(
        "import sys\nsys.exit(0)\n", encoding="utf-8", newline="")

    # loop-state-save.sh stub — the observation point.
    #   read : echoes a checkpoint naming $CP_EXISTING_GOAL, else `null` + rc=1
    #          (matching the real cmd_read contract). The echoed record carries
    #          selector_score so "richer anchor" is literal, not notional.
    #   init : appends the stdin payload to $CP_LOG; fails when $CP_INIT_FAIL.
    # Written as one dedented block, NOT string concatenation: an earlier
    # concatenated version produced a mis-paired single quote in the `read`
    # branch, which made bash fail to PARSE the whole stub — so `init` returned
    # non-zero and every allow-case test read as "the fix does not work".
    (scripts / "loop-state-save.sh").write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        sub="${1:-}"
        if [ "$sub" = "read" ]; then
          if [ -n "${CP_EXISTING_GOAL:-}" ]; then
            printf '{"goal_id":"%s","aspiration_id":"asp-000","source":"world","phase":"selected","selected_at":"x","selector_score":9.9}' "$CP_EXISTING_GOAL"
            exit 0
          fi
          echo null
          exit 1
        fi
        if [ "$sub" = "init" ]; then
          payload="$(cat)"
          printf '%s\\n' "$payload" >> "${CP_LOG}"
          if [ -n "${CP_INIT_FAIL:-}" ]; then exit 1; fi
          exit 0
        fi
        exit 0
        """), encoding="utf-8", newline="")

    # Sibling folded effects — out of scope here, stubbed silent.
    for name in ("team-state-in-flight.sh", "board-post.sh"):
        (scripts / name).write_text("exit 0\n", encoding="utf-8", newline="")

    # Positive control on the harness itself. A stub that does not PARSE fails
    # every subcommand, which reads identically to "the wrapper never called
    # it" — that is exactly how the first version of this file produced four
    # convincing false failures. Assert parseability once, here, so a harness
    # defect can never again be reported as a defect in the code under test.
    for stub in ("loop-state-save.sh", "_runtime.sh", "team-state-in-flight.sh",
                 "board-post.sh", "_goal-arg-normalize.sh"):
        chk = subprocess.run([BASH, "-n", str(scripts / stub)],
                             capture_output=True, text=True, timeout=60)
        assert chk.returncode == 0, f"stub {stub} does not parse: {chk.stderr}"

    return scripts / "aspirations-claim.sh"


def _run(tmp_path: Path, goal_id: str, *, wrapper_text: str | None = None,
         existing_goal: str = "", init_fail: bool = False,
         claimed_by: str = "alpha", stub_rc: int = 0):
    """Run the wrapper against the stub tree. Returns (CompletedProcess, payloads)."""
    text = wrapper_text if wrapper_text is not None else WRAPPER.read_text(encoding="utf-8")
    script = _build_tree(tmp_path, text)
    log = tmp_path / "init.log"
    log.write_text("", encoding="utf-8")

    env = dict(os.environ)
    env.update({
        "MIND_AGENT": "alpha",
        "STUB_LAUNCHER": LAUNCHER,
        "STUB_RESPONSE": _canned_claim(goal_id, claimed_by=claimed_by),
        "CP_LOG": str(log),
        "CP_EXISTING_GOAL": existing_goal,
        "STUB_RC": str(stub_rc),
    })
    if init_fail:
        env["CP_INIT_FAIL"] = "1"
    env.pop("MIND_SID", None)

    proc = subprocess.run([BASH, str(script), goal_id],
                          capture_output=True, text=True, timeout=120,
                          cwd=str(tmp_path), env=env)
    payloads = [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()
                if ln.strip()]
    return proc, payloads


def _mutated_wrapper() -> str:
    """The wrapper with the anchor block's `if ... fi` removed."""
    lines = WRAPPER.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if _GUARD_SENTINEL in ln), None)
    assert start is not None, (
        f"mutation harness could not find {_GUARD_SENTINEL!r} in the wrapper — "
        "the anchor block was renamed or removed; update this harness")
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].strip() == "fi"), None)
    assert end is not None, "no closing `fi` found for the anchor block"
    return "".join(lines[:start] + lines[end + 1:])


# --------------------------------------------------------------------------
# Allow-case: the anchor is written when none exists.
# --------------------------------------------------------------------------

def test_absent_checkpoint_is_anchored_at_claim(tmp_path):
    proc, payloads = _run(tmp_path, "g-115-3590")
    assert proc.returncode == 0, proc.stderr
    assert len(payloads) == 1, f"expected exactly one init, got {payloads}"
    p = payloads[0]
    assert p["goal_id"] == "g-115-3590"
    assert p["aspiration_id"] == "asp-115"
    assert p["source"] == "world", (
        "rc=0 from the claim endpoint means the goal was found in the WORLD "
        "queue; agent-queue goals are refused 400 upstream and never reach here")
    assert p["phase"] == "selected"
    # ISO 8601 naive, per CLAUDE.md § Naming Rules.
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", p["selected_at"]), p


def test_decomposition_child_id_resolves_its_parent_aspiration(tmp_path):
    """g-NNN-NN-a is a /decompose child; loop-state-save SCHEMA accepts it and
    its aspiration is still asp-NNN."""
    _, payloads = _run(tmp_path, "g-115-3590-a")
    assert len(payloads) == 1
    assert payloads[0]["aspiration_id"] == "asp-115"
    assert payloads[0]["goal_id"] == "g-115-3590-a"


def test_cross_world_id_resolves_to_the_xw_aspiration(tmp_path):
    """The g-xw-<ts>-NN / asp-xw-<ts> pair is the second id family in the
    SCHEMA patterns (g-115-2757). A naive `cut -f2` would yield asp-xw."""
    _, payloads = _run(tmp_path, "g-xw-20260101T000000-01")
    assert len(payloads) == 1
    assert payloads[0]["aspiration_id"] == "asp-xw-20260101T000000"


# --------------------------------------------------------------------------
# Specificity: a correct, richer anchor must survive.
# --------------------------------------------------------------------------

def test_existing_anchor_for_same_goal_is_preserved(tmp_path):
    """Phase 2.95 writes selector_score/skill/cross_agent_owner; a claim-time
    overwrite would silently downgrade that. ENSURE, not overwrite."""
    proc, payloads = _run(tmp_path, "g-115-3590", existing_goal="g-115-3590")
    assert proc.returncode == 0, proc.stderr
    assert payloads == [], (
        "claim re-anchored a checkpoint that already named this goal — the "
        "richer Phase 2.95 anchor (selector_score, skill, cross_agent_owner) "
        "would be lost")


def test_stale_anchor_naming_another_goal_is_replaced(tmp_path):
    """A checkpoint left behind by a prior iteration must not shadow the new
    goal — that is the anchor's whole purpose (post-compact goal substitution)."""
    _, payloads = _run(tmp_path, "g-115-3590", existing_goal="g-250-13")
    assert len(payloads) == 1
    assert payloads[0]["goal_id"] == "g-115-3590"


# --------------------------------------------------------------------------
# Fail-open: the claim already committed in the daemon.
# --------------------------------------------------------------------------

def test_init_failure_does_not_fail_the_claim(tmp_path):
    proc, _ = _run(tmp_path, "g-115-3590", init_fail=True)
    assert proc.returncode == 0, (
        "an anchor failure failed a claim that already committed in the "
        f"daemon: {proc.stderr}")
    assert "iteration-checkpoint init failed" in proc.stderr, (
        "the failure was swallowed with no diagnostic on any channel")


def test_claim_still_prints_the_goal_json(tmp_path):
    """The folded effects must not disturb the wrapper's stdout contract — the
    loop parses this JSON to confirm claimed_by == self."""
    proc, _ = _run(tmp_path, "g-115-3590")
    parsed = json.loads(proc.stdout)
    assert parsed["id"] == "g-115-3590"
    assert parsed["claimed_by"] == "alpha"


# --------------------------------------------------------------------------
# Sensitivity (guard-1475 / guard-1780): red without the fix.
# --------------------------------------------------------------------------

def test_mutation_removing_the_anchor_block_goes_red(tmp_path):
    mutated = _mutated_wrapper()
    assert _GUARD_SENTINEL not in mutated
    proc, payloads = _run(tmp_path, "g-115-3590", wrapper_text=mutated)
    assert proc.returncode == 0, proc.stderr
    assert payloads == [], (
        "the anchor still fired with its block removed — the allow-case tests "
        "are measuring something other than the block under test")


def test_mutation_keeps_the_rest_of_the_wrapper_working(tmp_path):
    """Positive control for the mutation harness: excising the block must leave
    a syntactically valid script that still claims. Without this, a mutation
    that merely broke the file would read as a passing sensitivity proof."""
    mutated = _mutated_wrapper()
    proc, _ = _run(tmp_path, "g-115-3590", wrapper_text=mutated)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["id"] == "g-115-3590"


@pytest.mark.parametrize("bad", ["not-a-goal", "x-115-01"])
def test_unrecognized_id_shape_writes_no_anchor(tmp_path, bad):
    """A malformed id must not produce a bogus aspiration_id that
    loop-state-save would reject at init (or worse, accept)."""
    proc, payloads = _run(tmp_path, bad)
    assert proc.returncode == 0, proc.stderr
    assert payloads == []


# --------------------------------------------------------------------------
# Refuse-case: a REFUSED claim must leave no anchor behind ().
# --------------------------------------------------------------------------

@pytest.mark.parametrize("stub_rc", [1, 2])
def test_refused_claim_writes_no_anchor(tmp_path, stub_rc):
    """A claim the daemon refused must not anchor the checkpoint.

    WHY THIS IS WORTH A TEST rather than being obvious from the source. The
    wrapper's `case $rc` dispatch reaches `_post_claim_effects` from the rc=0
    and rc=3-after-autospawn arms only, and a comment at the function head says
    so. But a comment is not a pin: fold one more effect in above the case
    statement, or add an arm that falls through, and a refused claim starts
    anchoring the checkpoint to a goal nobody is executing.

    That is not a hypothetical failure — it is the exact shape g-115-4753 spent
    a month chasing. A checkpoint anchoring `g-001-01` (a goal whose world copy
    has been `skipped` since 2026-07-22) was read as proof of a rogue writer,
    and elimination (e) of that goal reasoned in the opposite direction: the
    daemon refuses that claim twice over, therefore `_post_claim_effects` never
    ran, therefore the writer is not the claim path. That inference is only as
    good as this ordering, and nothing pinned it.

    rc=1 is the record-level refusal family (409 `goal_terminal`, 409
    `goal_id_collision`, 400 `agent_queue_goal`) — measured live on this box
    2026-09-02: claiming an already-completed world goal returned rc=1 with
    `{"error": "goal_terminal"}` and left the checkpoint untouched. rc=2 is the
    routing-POLICY family (`cross_lane_refused`, `lane_pin_refused`), which
    exits through a different arm and so is a distinct path, not a duplicate.
    rc=3 is deliberately NOT parametrized here: its arm retries via
    `rt_try_autospawn`, which the harness stubs to fail, so it would exercise
    the no-daemon exit rather than the refusal ordering this test is about.

    The in-test CONTROL is load-bearing and follows this file's own rule about
    harness defects reading as defects in the code under test: without it, a
    stub that silently stopped recording would satisfy `payloads == []` for
    every rc and the test would pass while measuring nothing.
    """
    control_proc, control_payloads = _run(tmp_path / "control", "g-115-4753")
    assert control_proc.returncode == 0, control_proc.stderr
    assert len(control_payloads) == 1, (
        "harness control failed: an ACCEPTED claim did not record an anchor, so "
        "an empty log below would prove nothing about the refusal path")

    proc, payloads = _run(tmp_path / "refused", "g-115-4753", stub_rc=stub_rc)
    assert proc.returncode != 0, (
        f"stub rc={stub_rc} should have propagated a non-zero exit; got 0 with "
        f"stderr={proc.stderr!r}")
    assert payloads == [], (
        f"a claim refused with rc={stub_rc} anchored the checkpoint anyway: "
        f"{payloads}. Downstream readers would then attribute this session to a "
        f"goal it never claimed.")
