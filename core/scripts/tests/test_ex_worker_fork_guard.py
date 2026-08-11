"""Regression test for : ex-worker fork guard coverage + placement.

A SID that previously ran as a Worker Body keeps its
`sessions/<sid>/working-memory.yaml` after wind-down BY DESIGN, and
`bash-agent-inject.py` exports `BODY_ROLE=worker` + `BODY_WM_PATH` on that
file's EXISTENCE. Re-binding the same terminal therefore mislabels the new
session as a worker, and the bind sequence then destroys the dead Body's
unmerged divergence: `body-manifest.sh write` is idempotent on `body_state`
(a re-write RESETS the Body to `active` and replaces `forked_wm_hash`), and
the worker path's `cp` overwrites the fork file itself.

`/start` refuses such a bind. Two invariants are locked down here:

  1. COVERAGE — the probe exists in all THREE binding branches that can meet
     a pre-existing fork (IDLE 0-pre2, RUNNING-worker W-pre, RUNNING-observer
     0-pre). Before g-306-210 only the IDLE branch had it, while 0-pre2's own
     prose claimed the refusal was "mode-wide" (guard-530: verify a per-session
     predicate against every session mode it can encounter).

  2. PLACEMENT — in each branch the probe precedes that branch's FIRST
     destructive write (session-binding-write.sh, then body-manifest.sh write).
     A refusal is only side-effect-free when it fires before the write it is
     protecting (guard-1813). Placed at W1 as originally proposed, the refusal
     would have fired AFTER W0.4 had already reset the manifest.

Plus a routing-decision pin on the hook, so a silent flip from
existence-routing to manifest-state-routing trips a test rather than changing
BODY_WM_PATH under a live worker mid-session.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
START_SKILL = PROJECT_ROOT / ".claude" / "skills" / "start" / "SKILL.md"
HOOK = PROJECT_ROOT / "core" / "scripts" / "bash-agent-inject.py"

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from _bash_helpers import BASH  # noqa: E402

# Anchor on the EXECUTABLE probe (`Bash:` + the sentinel), never the bare token.
# The token also appears in each branch's prose ("IF output is `EX_...`: STOP"),
# so a bare-token regex matches a branch that merely TALKS about the guard while
# having no probe — measured vacuous by mutation-proof-test.sh (sabotaging all 3
# Bash lines left all 4 tests green, because 3 prose lines still carried the
# token). Same discipline as test_recovery_ordering_invariant.py's
# `_executable_lines`. (guard-1475 — this is what the mutation proof caught.)
PROBE_RE = re.compile(r'Bash:.*EX_WORKER_FORK_PRESENT')
BIND_RE = re.compile(r'bash core/scripts/session-binding-write\.sh --sid')
MANIFEST_RE = re.compile(r'bash core/scripts/body-manifest\.sh write --sid')

# (label, section-header regex) in file order. The slice for each branch runs
# from its own header to the next header in this list (or EOF for the last).
SECTIONS = [
    ("RUNNING-worker", re.compile(r'^#### RUNNING \+ requested mode is `autonomous`')),
    ("RUNNING-observer", re.compile(r'^#### RUNNING \+ requested mode is `reader`')),
    ("IDLE", re.compile(r'^### IDLE ')),
    ("UNINITIALIZED", re.compile(r'^### UNINITIALIZED ')),
]


def _section_bounds(lines: list[str]) -> dict[str, tuple[int, int]]:
    """Map each section label to its [start, end) line-index slice."""
    starts: dict[str, int] = {}
    for idx, line in enumerate(lines):
        for label, pat in SECTIONS:
            if label not in starts and pat.search(line):
                starts[label] = idx
    missing = [label for label, _ in SECTIONS if label not in starts]
    assert not missing, f"start/SKILL.md section header(s) not found: {missing}"
    ordered = sorted(starts.items(), key=lambda kv: kv[1])
    bounds: dict[str, tuple[int, int]] = {}
    for i, (label, start) in enumerate(ordered):
        end = ordered[i + 1][1] if i + 1 < len(ordered) else len(lines)
        bounds[label] = (start, end)
    return bounds


def _hits(lines: list[str], lo: int, hi: int, pat: re.Pattern) -> list[int]:
    return [n for n in range(lo, hi) if pat.search(lines[n])]


def test_probe_present_in_all_three_binding_branches() -> None:
    lines = START_SKILL.read_text(encoding="utf-8").splitlines()
    bounds = _section_bounds(lines)
    for label in ("RUNNING-worker", "RUNNING-observer", "IDLE"):
        lo, hi = bounds[label]
        assert _hits(lines, lo, hi, PROBE_RE), (
            f"{label}: no EX_WORKER_FORK_PRESENT probe in this branch. Every /start "
            f"branch that can meet a pre-existing per-SID WM fork must refuse the bind "
            f"(g-306-210). 0-pre2's prose calls the refusal mode-wide; that is a claim "
            f"about scope, and each branch has to carry the probe for it to be true."
        )


def test_probe_precedes_first_destructive_write_in_each_branch() -> None:
    """guard-1813: a refusal after the destructive write protects nothing."""
    lines = START_SKILL.read_text(encoding="utf-8").splitlines()
    bounds = _section_bounds(lines)
    for label in ("RUNNING-worker", "RUNNING-observer", "IDLE"):
        lo, hi = bounds[label]
        probe = _hits(lines, lo, hi, PROBE_RE)
        writes = _hits(lines, lo, hi, BIND_RE) + _hits(lines, lo, hi, MANIFEST_RE)
        assert probe, f"{label}: probe missing (covered by the coverage test)"
        assert writes, (
            f"{label}: no session-binding-write.sh / body-manifest.sh write invocation "
            f"found — the ordering invariant has nothing to anchor against, which means "
            f"this test would pass vacuously. Re-derive the branch bounds."
        )
        assert min(probe) < min(writes), (
            f"{label}: PLACEMENT VIOLATION — the ex-worker probe is at line "
            f"{min(probe) + 1} but this branch's first destructive write is at line "
            f"{min(writes) + 1}. body-manifest.sh write RESETS body_state to active and "
            f"replaces forked_wm_hash, so a refusal after it has already destroyed the "
            f"dead Body's state record (guard-1813). Move the probe ahead of the write."
        )


def test_uninitialized_branch_exemption_is_documented() -> None:
    """The one unguarded branch must say so, and say why, in 0-pre2."""
    text = START_SKILL.read_text(encoding="utf-8")
    assert re.search(r"UNINITIALIZED branch is deliberately UNGUARDED", text), (
        "0-pre2 must state that the UNINITIALIZED branch is unguarded. An undocumented "
        "gap reads as coverage; this is the one branch where the refusal is deliberately "
        "absent (a first bind cannot meet a pre-existing fork for its own SID)."
    )
    assert "reducer_sid" in text and "cross-box worker" in text, (
        "the exemption must name BOTH fork-capable UNINITIALIZED paths (transplant-resume "
        "AND the cross-box worker, which forks unconditionally via reducer_sid=remote) — "
        "naming one member is not coverage of the set."
    )


def test_hook_routes_body_role_on_file_existence_not_manifest_state() -> None:
    """Pin the  routing decision at the export site."""
    text = HOOK.read_text(encoding="utf-8")
    assert "_body_wm.exists()" in text, (
        "bash-agent-inject.py must route BODY_WM_PATH/BODY_ROLE on the fork FILE's "
        "existence. Routing on body_state would make BODY_WM_PATH vanish mid-session "
        "the moment the reducer merges a live worker's fork, silently redirecting that "
        "worker's wm-*.sh writes to the agent-wide WM (g-306-210)."
    )
    # ONE marker, no `or`-chain. The first draft accepted either this marker OR a
    # prose regex about body_state, which made it vacuous: sabotaging the marker
    # left the prose matching and the test green (measured by mutation-proof-test).
    # An alternative branch in an assertion is a second way to pass, not a second
    # thing checked.
    assert "DECISION RE-DERIVED AND UPHELD (g-306-210" in text, (
        "the routing decision must stay documented at the export site with its reason, "
        "so a future reader does not re-litigate it from the latency argument alone "
        "(measured: the manifest read costs ~1.5% of this hook's own spawn — real, and "
        "too small to decide anything). The deciding reason is that body_state is "
        "written by another process, so state-routing changes BODY_WM_PATH mid-session."
    )


def test_probe_command_actually_discriminates() -> None:
    """Execute the probe SKILL.md ships, both ways. Structure is not behaviour.

    The three tests above assert the probe EXISTS and is PLACED correctly. None
    of them runs it, so a typo inside the path (`session/` for `sessions/`, a
    dropped `$MIND_SID`) satisfies every one of them while the guard silently
    never fires — refusing nothing, forever, exactly like a fork-free box. This
    test lifts the literal command out of SKILL.md and runs it against a tmp
    tree with the fork present and absent. It is the only assertion here with
    power over the probe's CONTENT.
    """
    import re as _re
    import subprocess
    import tempfile

    lines = START_SKILL.read_text(encoding="utf-8").splitlines()
    probes = [L for L in lines if PROBE_RE.search(L)]
    assert probes, "no executable probe line found to execute"

    # Lift the command out of the markdown backticks: Bash: `<cmd>`
    cmds = []
    for L in probes:
        m = _re.search(r"Bash:\s*`(.+)`\s*$", L)
        assert m, f"probe line is not in the documented Bash-backtick form: {L.strip()!r}"
        cmds.append(m.group(1))
    assert len(set(cmds)) == 1, (
        f"the three branches must ship the IDENTICAL probe command, else one branch "
        f"can drift without the others noticing. Got: {sorted(set(cmds))}"
    )
    cmd = cmds[0].replace("<agent-name>", "testagent")

    with tempfile.TemporaryDirectory() as td:
        env = {"PATH": "/usr/bin:/bin", "MIND_SID": "SID-UNDER-TEST"}
        sess = Path(td) / "agents" / "testagent" / "sessions" / "SID-UNDER-TEST"
        sess.mkdir(parents=True)

        def run() -> str:
            # BASH, not a bare "bash" argv[0] (guard-580): on win32 a bare
            # "bash" resolves via System32 to the WSL launcher and can hang
            # forever against a wedged LxssManager. The pre-commit gate refuses
            # the literal, and it refused this file's first draft.
            return subprocess.run([BASH, "-c", cmd], cwd=td, env=env,
                                  capture_output=True, text=True).stdout.strip()

        # Bind the result BEFORE asserting on it. Calling run() again inside the
        # f-string would spawn a SECOND subprocess and report ITS value, not the
        # one that failed — so a non-deterministic probe would be described by an
        # execution that never failed. (Caught by fresh-eyes on this file.)
        # ABSENT -> must NOT trip the guard (a false refusal wedges every bind).
        absent = run()
        assert absent == "no-fork", (
            f"probe reported {absent!r} with NO fork file present — this refuses "
            f"legitimate binds in all three branches. Command: {cmd!r}"
        )

        # PRESENT -> must trip it. This is the crashed-worker re-activation shape:
        # a wound-down Worker Body's fork surviving on the SAME SID.
        (sess / "working-memory.yaml").write_text("slots: {}\n", encoding="utf-8")
        present = run()
        assert present == "EX_WORKER_FORK_PRESENT", (
            f"probe reported {present!r} with the fork file PRESENT at "
            f"{sess / 'working-memory.yaml'} — the guard is inert and an ex-worker "
            f"terminal would re-bind, resetting body_state and overwriting the fork. "
            f"Command: {cmd!r}"
        )


if __name__ == "__main__":
    test_probe_present_in_all_three_binding_branches()
    test_probe_precedes_first_destructive_write_in_each_branch()
    test_uninitialized_branch_exemption_is_documented()
    test_hook_routes_body_role_on_file_existence_not_manifest_state()
    # The behavioural test MUST be in this list. Its first draft omitted it, so a
    # hand-run printed ALL PASS having executed only the four STRUCTURAL tests —
    # i.e. the standalone path reported green while never once running the probe.
    # pytest collects all five regardless, which is exactly what hid it.
    test_probe_command_actually_discriminates()
    print("ALL PASS — g-306-210 ex-worker fork guard: 3-branch coverage + placement "
          "+ routing pin + probe behaviour (5 tests)")
