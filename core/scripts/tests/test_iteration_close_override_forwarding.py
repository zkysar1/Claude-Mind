"""Pin : iteration-close.sh must forward every completion-gate override.

THE DEFECT (alpha, hostname cc-04, uname -r 6.8.0-137-generic, 2026-08-13).
`gates/residual_work.py` refuses a `status=completed` write and names its own
escape hatch verbatim in the refusal message:

    "or pass --override-residual \"<justification>\" (audited to
     residual-work-overrides.jsonl)"

`aspirations-update-goal.sh` DOES define that flag (aspirations.py, the
`p_ug` parser). But `iteration-close.sh` -- the wrapper every loop close
actually goes through -- parsed only `--override-uncommitted` and
`--override-missing-artifact`, and died `unknown arg: --override-residual`
(rc=2) on the third. The remedy the gate advertised was unreachable from the
standard close path.

WHY THIS IS WORSE THAN AN ANNOYANCE. The only remaining way past a false
positive was to bypass `do_verify` entirely and set status directly. That
skips, silently: the pending-deploys ENFORCE gate, the ordered-write intent
marker, `completed_date`, `outcome_class`, the execution-diary breadcrumb, the
findings board post, and clear-in-flight. A gate designed to protect the store
was pushing callers onto the one path that corrupts it.

WHY A HAND-RUN CANNOT CATCH THIS (guard-1532, amended from this incident).
guard-1532 already says "execute its own advice once". Executing it by hand
PASSES -- `aspirations-update-goal.sh --override-residual "..."` works fine,
because the hand-run exercises the layer that DEFINES the flag and never the
layer that FORWARDS it. Only the production caller fails. That is
`probe-with-canonical-code-path.md` § "Canonical BINARY Is Not Canonical
INVOCATION" landing inside a gate's own remediation.

WHY THE PREDICATE IS NOT "ALL --override-* FLAGS".
The naive parity test -- every override flag on the `p_ug` parser must be
forwarded -- asserts a WRONG invariant and would demand forwarding two flags
whose gates cannot fire on this path. Measured on the live source:

    --override-uncommitted        consumed under `value == "completed"`  -> REQUIRED
    --override-missing-artifact   consumed under `value == "completed"`  -> REQUIRED
    --override-residual           consumed under `value == "completed"`  -> REQUIRED
    --override-agent-match        consumed under `value == "superseded"` -> not required
    --override-blocker-gate       consumed in CREATE_BLOCKER, not update -> not required

`do_verify` only ever writes `--status <completed|blocked|skipped>`, never
`superseded`, so the last two are correctly absent.
`test_non_completed_overrides_are_not_demanded` is the load-bearing test in
this file: it is the one that fails if someone "fixes" the predicate by
widening it to all five.

THE DERIVATION IS SELF-CONTROLLED. A regex that silently matched nothing would
make the forwarding assertion vacuously green -- the exact silent-zero shape
guard-2298 describes. `test_derivation_is_not_vacuous` is the positive control:
it pins that the derivation finds a non-empty set containing the three known
members, so a broken derivation reddens here instead of passing everything.
"""

import re
from pathlib import Path

import pytest

# NOTE: deliberately no sys.path.insert here. This module is pure static
# analysis over two source files and imports nothing from core/scripts, so the
# usual sibling-test path bootstrap would be dead code (removed by
# fresh-eyes-code, same session as this file was written).
SCRIPTS = Path(__file__).resolve().parents[1]
ASP_PY = SCRIPTS / "aspirations.py"
CLOSE_SH = SCRIPTS / "iteration-close.sh"

# The three flags measured on the completed-status branch at the time of the
# fix. Used ONLY as the positive control for the derivation (see module
# docstring) -- the forwarding assertion itself runs off the DERIVED set, so a
# newly-added fourth completion override is caught without editing this list.
KNOWN_COMPLETED_OVERRIDES = {
    "--override-uncommitted",
    "--override-missing-artifact",
    "--override-residual",
}

BRANCH_RE = re.compile(r'^(\s*)if field == "status" and value == "(\w+)"')


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def completed_branch_overrides() -> set:
    """Flags whose dest is consumed inside cmd_update_goal's completed branch.

    For each `--override-*` flag declared anywhere in aspirations.py, find where
    its argparse dest is read, then walk BACKWARD to the nearest enclosing
    `if field == "status" and value == "..."` guard. Containment is confirmed by
    indentation, so a consumption that merely follows such a branch without
    being inside it is not counted.
    """
    text = ASP_PY.read_text(encoding="utf-8")
    lines = text.splitlines()
    flags = set(re.findall(r'"(--override-[a-z-]+)"', text))
    required = set()
    for flag in sorted(flags):
        dest = flag[2:].replace("-", "_")
        # Match BOTH consumption spellings. aspirations.py is uniformly
        # `getattr(args, "<dest>"` today, but a future override read as
        # `args.<dest>` would be invisible to a getattr-only probe -- and an
        # invisible consumption makes the forward-looking test silently
        # under-derive, which is the same shape as the F-001 defect this file
        # was already amended for (a forward-looking check blind to the exact
        # failure mode it exists to catch). Widened at the same session.
        consume_re = re.compile(r"args[.,]\s*[\"']?" + re.escape(dest) + r"\b")
        for i, line in enumerate(lines):
            if not consume_re.search(line):
                continue
            for j in range(i - 1, -1, -1):
                m = BRANCH_RE.match(lines[j])
                if not m:
                    continue
                # Containment: the consumption must be deeper than the guard.
                if _indent(line) > len(m.group(1)) and m.group(2) == "completed":
                    required.add(flag)
                break
    return required


def _close_sh() -> str:
    return CLOSE_SH.read_text(encoding="utf-8")


def test_derivation_is_not_vacuous():
    """Positive control -- a derivation matching nothing must redden HERE.

    Without this, a regex drift would make every forwarding assertion below
    pass over an empty set and report green on a completely unforwarded script.
    """
    derived = completed_branch_overrides()
    assert derived, (
        "completed_branch_overrides() derived NO flags from aspirations.py -- "
        "the derivation is broken, so the forwarding tests below are vacuous. "
        "Check the --override-* declaration regex and the branch guard regex."
    )
    missing = KNOWN_COMPLETED_OVERRIDES - derived
    assert not missing, (
        f"derivation lost known completed-branch override(s): {sorted(missing)}. "
        "Either the flag was genuinely removed from the completed branch (update "
        "KNOWN_COMPLETED_OVERRIDES) or the derivation regressed."
    )


@pytest.mark.parametrize("flag", sorted(KNOWN_COMPLETED_OVERRIDES))
def test_known_completion_override_is_parsed_and_forwarded(flag):
    """Each known completion-gate override must be BOTH parsed and forwarded.

    Parsing alone is not enough: a flag can be accepted by the arg loop and then
    dropped on the floor, which fails exactly like not parsing it but passes a
    naive grep for the flag name.
    """
    src = _close_sh()
    var = flag[2:].replace("-", "_").upper()
    assert f"{flag})" in src, (
        f"{flag} is consumed on aspirations.py's completed-status branch but "
        f"iteration-close.sh does not PARSE it -- the standard close path will "
        f"exit 2 'unknown arg: {flag}' and the gate's own remediation is "
        f"unreachable (guard-1532)."
    )
    assert f"{var}=" in src, f"{flag} is parsed but never stored into ${var}"
    assert f'update_cmd+=({flag} "${var}")' in src, (
        f"{flag} is parsed into ${var} but never APPENDED to update_cmd -- it is "
        f"accepted and then silently discarded, which fails identically to not "
        f"parsing it while passing a grep for the flag name."
    )


def test_every_derived_completion_override_is_forwarded():
    """The forward-looking half: a NEW completion override must be forwarded.

    Runs off the DERIVED set, not the known list, so adding a fourth override to
    cmd_update_goal's completed branch without wiring iteration-close.sh reddens
    here with no edit to this file.

    CHECKS BOTH HALVES, and the second half was missing on first write
    (fresh-eyes-code, g-335-1216, same session). This test originally asserted
    only `f"{flag})" in src` -- i.e. that the flag is PARSED. A flag can be
    parsed and then never appended to update_cmd, which fails identically to not
    parsing it while passing a grep for the flag name. Proven, not reasoned:
    mutation 1 of this file's own verification run (redirect the append to a
    different array, leave the `--override-residual)` case intact) reddened
    test_known_completion_override_is_parsed_and_forwarded and
    test_residual_override_reaches_the_status_call_not_a_later_one, and left
    THIS test green. The one test written to catch a future override was blind
    to the exact failure mode its own sibling documents.
    """
    src = _close_sh()
    unforwarded = [
        f
        for f in sorted(completed_branch_overrides())
        if f"{f})" not in src or f"update_cmd+=({f} " not in src
    ]
    assert not unforwarded, (
        f"override flag(s) {unforwarded} fire on the status=completed write that "
        f"iteration-close.sh's do_verify performs, but iteration-close.sh does not "
        f"forward them. Every loop close routes through that wrapper, so the gate's "
        f"advertised escape hatch is unreachable and the only way past a false "
        f"positive is to bypass do_verify -- skipping the pending-deploys gate, the "
        f"ordered-write intent marker, completed_date, outcome_class, the diary "
        f"breadcrumb, the board post and clear-in-flight. Mirror the "
        f"--override-uncommitted wiring (declare, parse, append to update_cmd)."
    )


def test_non_completed_overrides_are_not_demanded():
    """LOAD-BEARING: the predicate must not widen to every --override-* flag.

    `--override-agent-match` is consumed under the `superseded` branch and
    `--override-blocker-gate` under CREATE_BLOCKER. `do_verify` writes only
    completed|blocked|skipped, so neither gate can fire on its path. A parity
    test over all five flags would demand forwarding both and be wrong; this
    test is what fails if someone widens it that way.
    """
    text = ASP_PY.read_text(encoding="utf-8")
    all_flags = set(re.findall(r'"(--override-[a-z-]+)"', text))
    derived = completed_branch_overrides()
    assert len(all_flags) > len(derived), (
        "every --override-* flag in aspirations.py was derived as "
        "completed-branch-required. That is almost certainly the derivation "
        "over-matching (e.g. the indentation containment check was dropped), "
        "not a real change -- verify against the enclosing branch guards."
    )
    for flag in ("--override-agent-match", "--override-blocker-gate"):
        if flag in all_flags:
            assert flag not in derived, (
                f"{flag} was derived as completed-branch-required, but it is "
                f"consumed on a path do_verify never takes (superseded / "
                f"CREATE_BLOCKER). Forwarding it would be harmless but the "
                f"derivation is wrong, and a wrong derivation is what makes the "
                f"forward-looking test above untrustworthy."
            )


def test_residual_override_reaches_the_status_call_not_a_later_one():
    """The append must land on update_cmd (the status write), not a sibling call.

    do_verify issues several aspirations-update-goal.sh calls -- status, then
    completed_date, then others. The residual gate fires on the STATUS write, so
    an override appended to any later call would be audited but never consulted.
    """
    src = _close_sh()
    idx_build = src.find("update_cmd=(")
    idx_exec = src.find('"${update_cmd[@]}"')
    idx_append = src.find('update_cmd+=(--override-residual')
    assert idx_build != -1 and idx_exec != -1, "update_cmd build/exec sites not found"
    assert idx_append != -1, "--override-residual is never appended to update_cmd"
    assert idx_build < idx_append < idx_exec, (
        "--override-residual is appended to update_cmd outside the "
        "build->exec window, so it never reaches the status write the residual "
        "gate actually fires on."
    )
