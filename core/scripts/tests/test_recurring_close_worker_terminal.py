""": recurring-close.sh's ITERATION COMPLETE banner must print the
WORKER terminal to a worker Body, never the reducer imperative.

THE DEFECT. The terminal NEXT-ACTION block built `_next_action` only from the
reducer forms — Skill(aspirations-spark) FIRST (reducer-only), the deadman pair
whose net is `<<autonomous-loop-dynamic>>` (the reducer's sentinel), and
Skill(aspirations) with args='loop' (the reducer re-entry guard-517/guard-463
forbid a worker). But a worker Body closes recurring goals through this script
too (measured: g-335-09 close from a worker, cc-08, 2026-09-07). A worker that
obeyed the banner would either die (spark/aspirations are reducer-only) or
re-enter AS a reducer. The banner had no role branch.

THE FIX. A `[[ "${BODY_ROLE:-}" == "worker" ]]` branch at the top of the banner
block emits the CANONICAL worker terminal via the shared `deadman-directive.sh
--role worker` emitter (the same emitter worker-loop Phase 5 uses), and keys on
the SAME BODY_ROLE predicate `bash-agent-inject.py` exports and
`iteration-close.sh` already branches on — not a second copy of the role test.
The reducer path (BODY_ROLE unset) is unchanged.

WHY BEHAVIOURAL, NOT SOURCE-GREP (guard-6333). "A test that asserts a branch
behaves MUST assert BEHAVIOUR, not source text." Both roles are exercised by
EXTRACTING the real terminal block from recurring-close.sh and EXECUTING it —
same pattern as test_recurring_close_failure_imperative.py. A test that restated
the block would pass against its own copy while production drifted. Mutation
proof: revert the worker branch and BODY_ROLE=worker falls to the reducer path,
so `Skill(worker-loop)` vanishes from the worker output and every worker-case
assertion below goes red.

WHY THE DISCRIMINATORS ARE THE REDUCER'S *PRESCRIPTIVE* TOKENS. The worker
terminal (deadman-directive --role worker) itself says "NEVER call
Skill(aspirations)" — a PROHIBITION — so "Skill(aspirations)" is present in the
worker output and cannot discriminate. The reducer's *prescriptions*
("Skill(aspirations-spark) FIRST", "args='loop'", "<<autonomous-loop-dynamic>>")
appear ONLY on the reducer path, so their ABSENCE is what proves a worker never
sees the reducer imperative.

Run: STORAGE_BACKEND=local python3 -m pytest \
     core/scripts/tests/test_recurring_close_worker_terminal.py -v
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent
RECURRING_CLOSE_SH = CORE_SCRIPTS / "recurring-close.sh"

sys.path.insert(0, str(CORE_SCRIPTS))
from _runtime_bash import BASH  # noqa: E402  (guard-580: never a bare "bash")

# Same sentinels test_recurring_close_failure_imperative.py extracts on. The
# block now opens with the worker branch and closes at the kill-the-loop line.
START = "# The proceed text is COMPUTED ONCE"
END = "A Bash echo or text summary as the terminal action kills the loop"

# The reducer's PRESCRIPTIVE tokens — present only on the reducer path.
REDUCER_PRESCRIPTIONS = (
    "Skill(aspirations-spark) FIRST",   # reducer deep spark
    "args='loop'",                       # reducer Skill(aspirations) re-entry
    "<<autonomous-loop-dynamic>>",       # reducer deadman net sentinel
)
# The worker terminal marker — the deadman-directive worker re-entry.
WORKER_MARKER = "Skill(worker-loop)"


def _extract_terminal_block() -> str:
    """Pull the terminal NEXT-ACTION block out of recurring-close.sh verbatim."""
    src = RECURRING_CLOSE_SH.read_text(encoding="utf-8")
    i = src.find(START)
    if i < 0:
        raise RuntimeError(f"start sentinel not found in recurring-close.sh: {START!r}")
    j = src.find(END, i)
    if j < 0:
        raise RuntimeError(f"end sentinel not found after {START!r}")
    return src[i:src.find("\n", j) + 1]


TERMINAL_BLOCK = _extract_terminal_block()


def _run_terminal(body_role, max_rc=0, outcome="routine",
                  deadman_disabled=False, failed_phases="", phase_results=""):
    """Execute the EXTRACTED terminal block under a controlled role.

    body_role: "worker" exports BODY_ROLE=worker (and SCRIPT_DIR, which the
    worker branch needs to call the real deadman-directive.sh emitter); None
    explicitly UNSETS BODY_ROLE so an inherited worker-box value cannot leak
    into the reducer path (guard-6333 — control the thing under test).
    """
    with tempfile.TemporaryDirectory() as td:
        agent_dir = Path(td) / "agent"
        (agent_dir / "session").mkdir(parents=True)
        if deadman_disabled:
            (agent_dir / "session" / "deadman-disabled").write_text("")
        role_line = (
            "unset BODY_ROLE\n" if body_role is None
            else f"export BODY_ROLE={body_role!r}\n"
        )
        script = (
            role_line
            + f"SCRIPT_DIR={CORE_SCRIPTS.as_posix()!r}\n"
            + f"AGENT_DIR={agent_dir.as_posix()!r}\n"
            + f"MAX_RC={max_rc}\nOUTCOME={outcome!r}\n"
            + f"FAILED_PHASES={failed_phases!r}\n"
            + f"PHASE_RESULTS={phase_results!r}\n"
            + "FAILED_RETRY_CMDS=''\n"
            + TERMINAL_BLOCK
        )
        r = subprocess.run(
            [BASH, "-c", script], capture_output=True, text=True,
            cwd=str(PROJECT_ROOT),
        )
        assert r.returncode == 0, f"rc={r.returncode}; stderr: {r.stderr}"
        return r.stdout


# ── Outcome 1: a worker sees the worker terminal, never the reducer clauses ──


@pytest.mark.parametrize("outcome", ["deep", "routine"])
@pytest.mark.parametrize("deadman_disabled", [False, True])
def test_worker_sees_worker_terminal(outcome, deadman_disabled):
    """A worker close emits the worker re-entry (Skill(worker-loop)) and NONE of
    the reducer's prescriptive clauses — under both outcomes and both deadman
    states, because a worker's terminal is outcome-independent."""
    out = _run_terminal("worker", outcome=outcome, deadman_disabled=deadman_disabled)
    assert WORKER_MARKER in out, (
        f"worker close missing the worker re-entry {WORKER_MARKER!r}: {out!r}"
    )
    for tok in REDUCER_PRESCRIPTIONS:
        assert tok not in out, (
            f"worker close wrongly carries the reducer prescription {tok!r} "
            f"(the exact defect g-115-10648 fixes): {out!r}"
        )


def test_worker_armed_emits_canonical_net_banner():
    """The armed worker terminal is the real deadman-directive emitter output,
    not a paraphrase — its distinctive WORKER NET ARMED banner must appear."""
    out = _run_terminal("worker", deadman_disabled=False)
    assert "WORKER NET ARMED" in out, (
        f"worker terminal not sourced from deadman-directive --role worker: {out!r}"
    )


def test_worker_failure_path_warns_and_stays_worker():
    """A failed phase on a worker close still warns AND still points the worker
    at its own loop — never the reducer imperative."""
    out = _run_terminal(
        "worker", max_rc=1, outcome="deep",
        failed_phases="verify ", phase_results="verify=fail(1) ",
    )
    assert "PHASE FAILURE" in out and "MAX_RC=1" in out, out
    assert WORKER_MARKER in out, out
    for tok in REDUCER_PRESCRIPTIONS:
        assert tok not in out, f"worker failure path leaked reducer clause {tok!r}: {out!r}"


# ── Outcome 2: the reducer path prints exactly what it prints today ──────────


def test_reducer_routine_unchanged():
    """BODY_ROLE unset, routine, deadman ON: the reducer net + re-entry are
    present and no worker marker leaked in."""
    out = _run_terminal(None, outcome="routine", deadman_disabled=False)
    assert "<<autonomous-loop-dynamic>>" in out and "args='loop'" in out, out
    assert WORKER_MARKER not in out and "WORKER NET ARMED" not in out, out


def test_reducer_deep_unchanged():
    """BODY_ROLE unset, deep: the reducer's spark prescription is present; no
    worker marker leaked in."""
    out = _run_terminal(None, outcome="deep", deadman_disabled=False)
    assert "Skill(aspirations-spark) FIRST" in out, out
    assert WORKER_MARKER not in out and "WORKER NET ARMED" not in out, out


def test_reducer_deadman_disabled_unchanged():
    """BODY_ROLE unset + deadman-disabled: the pre-deadman reducer imperative
    (Skill(aspirations) args='loop', no net) still fires; no worker marker."""
    out = _run_terminal(None, outcome="routine", deadman_disabled=True)
    assert "args='loop'" in out, out
    assert "<<autonomous-loop-dynamic>>" not in out, out  # disabled → no net
    assert WORKER_MARKER not in out, out


# ── check 1: keyed on the shared predicate + canonical emitter (structural) ──


def test_branch_keys_on_shared_predicate_and_canonical_emitter():
    """Secondary, structural pin (the behavioural tests above are primary,
    guard-6333). The worker branch must use the BODY_ROLE predicate — the same
    one bash-agent-inject.py exports and iteration-close.sh branches on — and
    call the shared deadman-directive emitter, never a second copy of either."""
    src = RECURRING_CLOSE_SH.read_text(encoding="utf-8")
    assert '[[ "${BODY_ROLE:-}" == "worker" ]]' in src, (
        "worker branch missing or not keyed on the shared BODY_ROLE predicate"
    )
    assert 'deadman-directive.sh" --role worker' in src, (
        "worker terminal not sourced from the shared deadman-directive emitter"
    )


if __name__ == "__main__":
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = total = 0
    for fn in fns:
        # expand parametrized combos for the direct-run smoke path
        combos = [()]
        if fn is test_worker_sees_worker_terminal:
            combos = [(o, d) for o in ("deep", "routine") for d in (False, True)]
        for args in combos:
            total += 1
            try:
                fn(*args)
                print(f"  [PASS] {fn.__name__}{args}")
                passed += 1
            except AssertionError as e:
                print(f"  [FAIL] {fn.__name__}{args}: {e}")
                traceback.print_exc()
            except Exception as e:
                print(f"  [ERROR] {fn.__name__}{args}: {type(e).__name__}: {e}")
                traceback.print_exc()
    print(f"\n{passed}/{total} tests passed")
    sys.exit(0 if passed == total else 1)
