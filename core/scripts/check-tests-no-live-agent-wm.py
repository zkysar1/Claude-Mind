#!/usr/bin/env python3
"""Refuse any test that writes WORKING MEMORY for a LIVE fleet agent ().

WHY THIS EXISTS. Three post-state-update-gate test files carried an autouse
fixture that saved, null-wrote, then restored `fresh_eyes_last_fire` for the LIVE
agent `zeta`. Every branch was `except Exception: pass` with no `finally`, so any
crash between neutralize and restore destroyed a real cross-agent signal
permanently and silently. The pattern spread by COPYING: daemon_dispatch
inherited it from mode_only, which inherited it from committed_files_only. Two
instances is what made the family visible; this check is what stops the fourth.

THE ROSTER IS DERIVED AT CHECK TIME, NEVER HARDCODED (guard-1699). A check
pinning "zeta" goes stale the moment an agent is retired or added, and would then
silently test nothing while still reporting green — the exact fixture-rot this
check guards against, reproduced one level up.

WHY THE PREDICATE IS NARROW, and why that is deliberate rather than lax. Setting
MIND_AGENT to a real agent name is COMMON and mostly harmless: 59 of the test
files do it, overwhelmingly for path routing or as a module-level default. A
check on that broad shape would be RED on arrival, and a checklist entry that
fails from the day it lands trains every reader to skim the section it lives in
(the reason g-115-4887 deliberately did NOT ship this check before the fix). So
the predicate requires BOTH halves of the actual defect: a live-roster agent name
AND a working-memory WRITE in the same file.

AST, NOT GREP. A plain grep matches the writer's name inside COMMENTS and
DOCSTRINGS — measured: test_dependent_unblock_windows_path.py merely MENTIONS
wm-append.sh in prose and was a false positive under two successive grep
refinements. Docstrings are excluded structurally here instead.

Exit 0 = clean. Exit 1 = at least one offender (named on stdout). Exit 2 = the
roster could not be derived, which fails LOUD rather than passing vacuously: a
check that cannot see the roster has no opinion and must not report success
(rb-245 — verify the population before trusting a zero).
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _runtime_bash import bash_cmd  # noqa: E402  (guard-580)

PROJECT_ROOT = Path(__file__).resolve().parents[1].parent
TESTS_DIR = PROJECT_ROOT / "core" / "scripts" / "tests"

WM_WRITER_NAMES = {"WM_SET_SH", "WM_APPEND_SH"}
WM_WRITER_PATHS = ("wm-set.sh", "wm-append.sh")

# --- Shell surface () ---------------------------------------------
# The .py glob below used to be the whole check, so all 36 shell tests under
# this directory were structurally invisible: not scanned, not parsed, never
# reportable. Measured 2026-09-06 with a LIVE specimen present while the check
# reported PASS — alpha's working memory held `last_strategic_scan_tick =
# "test_value_for_last_strategic_scan_tick"`, seeded by a shell test.
#
# The shell defect has a DIFFERENT SHAPE from the python one, which is why it
# needs its own predicate rather than a wider glob. The python side catches a
# LITERAL binding (`env["MIND_AGENT"] = "zeta"`). A shell test binds nothing:
# it does `source _paths.sh`, which resolves $AGENT_DIR from the AMBIENT
# MIND_AGENT, so it targets whichever live agent happens to be running the
# suite. Ambient is invisible to a literal-binding predicate by construction.
WM_PATH_LITERAL = "working-memory.yaml"
AMBIENT_AGENT_DIR_TOKENS = ("$AGENT_DIR", "${AGENT_DIR}")

# Shell tests that ALREADY carry this defect and have a goal that owns the fix.
# Keyed to the owning goal so the entry is machine-findable (reclaim-routed-work
# rule 4), and VERIFIED NON-STALE in main(): if an allowlisted file stops
# matching the predicate the check FAILS and asks for the entry to be deleted.
# So this cannot rot into permanent suppression the way a bare skip-list does —
# the allowlist retires itself the moment the fix lands
# (learning-philosophy.md rule 5: scar tissue must be removable, and something
# has to notice when it is no longer earning its keep).
SHELL_ALLOWLIST: dict[str, str] = {
    # EMPTY IS THE DESIGNED END STATE, not a gap — see the comment above. An
    # entry lives only while its owning goal's fix is outstanding, and the
    # stale-entry check below is what forces the deletion.
    #
    # It worked: "test-wm-prune-cadence-protection.sh" -> "" was
    # removed 2026-09-06 when that fix landed. The test now seeds a throwaway
    # per-session target (routed by the X-Mind-Sid header) instead of the live
    # agent working memory, so `analyze_shell` stopped matching it and this
    # check FAILED with "the exemption is dead code reading as coverage" until
    # the entry was deleted. That failure is the mechanism, not a defect.
}


def live_roster() -> set[str]:
    """Agent names in the live team-state agent_status roster, at check time."""
    r = subprocess.run(
        bash_cmd(PROJECT_ROOT / "core" / "scripts" / "team-state-read.sh",
                 "--field", "agent_status", "--json"),
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"team-state-read failed rc={r.returncode}: {r.stderr[:200]}")
    data = json.loads(r.stdout)
    if not isinstance(data, dict) or not data:
        raise RuntimeError(f"agent_status resolved to an empty/unexpected shape: {type(data).__name__}")
    return set(data.keys())


def _docstring_nodes(tree: ast.AST) -> set[int]:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                out.add(id(body[0].value))
    return out


def analyze(path: Path, roster: set[str]) -> list[str]:
    """Return the live-roster agent names this file binds, if it also writes WM."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return []
    docs = _docstring_nodes(tree)

    writes_wm = False
    bound: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in WM_WRITER_NAMES:
            writes_wm = True
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            if any(w in node.value for w in WM_WRITER_PATHS):
                writes_wm = True
        # MIND_AGENT bound to a literal: {"MIND_AGENT": "x"} or env["MIND_AGENT"] = "x"
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and k.value == "MIND_AGENT"
                        and isinstance(v, ast.Constant) and isinstance(v.value, str)):
                    bound.add(v.value)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if (isinstance(tgt, ast.Subscript) and isinstance(tgt.slice, ast.Constant)
                        and tgt.slice.value == "MIND_AGENT"
                        and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, str)):
                    bound.add(node.value.value)
                elif (isinstance(tgt, ast.Name) and tgt.id in ("AGENT", "AGENT_NAME", "TEST_AGENT")
                        and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, str)):
                    bound.add(node.value.value)

    return sorted(bound & roster) if writes_wm else []


def _shell_code_lines(text: str) -> list[str]:
    """Drop whole-line comments so the predicate cannot match prose.

    Line-leading `#` only. An inline `#` cannot be stripped without a real
    quote-state scanner, and hand-rolling one is how this module's grep
    ancestors produced false positives (see AST, NOT GREP above). The predicate
    below is narrow enough that it does not need one: a comment mentioning the
    WM filename AND $AGENT_DIR on the SAME line is not a shape that occurs.
    """
    return [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]


def analyze_shell(path: Path) -> bool:
    """True when a shell test writes a WM path rooted at the AMBIENT agent dir.

    DELIBERATELY NARROW, and measured rather than guessed. Of the 36 shell tests
    in this directory, FOUR touch working memory at all: three name the WM
    filename and one more invokes a WM-writer wrapper. This shape catches the
    ONE that reaches a LIVE agent (`WM_FILE="$AGENT_DIR/session/working-memory.yaml"`
    after `source _paths.sh`) and excludes the other three — two that root their
    agent dir in `mktemp -d` (test-g3-worker-store-rails.sh, `AG="$TMP/agents/zeta"`;
    test-sentinel-clear-guarded.sh, which stubs a writer wrapper inside its own
    $TMP) and one that names the live file only inside a comment
    (test-assert-capture-complete.sh).

    It does NOT flag a bare `wm-set.sh` or `wm-append.sh` mention, and that omission is
    the load-bearing half: the sandboxed stub in test-sentinel-clear-guarded.sh
    has exactly that shape, so a writer-name predicate would be a false positive
    ON ARRIVAL — the precise failure the module docstring above exists to prevent.
    The cost is that a shell test which calls a WM writer under the ambient
    binding WITHOUT naming the path is not covered; no such file exists today.
    """
    try:
        lines = _shell_code_lines(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return False
    return any(
        WM_PATH_LITERAL in ln and any(tok in ln for tok in AMBIENT_AGENT_DIR_TOKENS)
        for ln in lines
    )


def main(argv: list[str]) -> int:
    scan_dir = TESTS_DIR
    if len(argv) > 1:
        scan_dir = Path(argv[1])
    try:
        roster = live_roster()
    except Exception as exc:
        print(f"FAIL: could not derive the live roster — {exc}")
        print("  A check that cannot see its population must not report success.")
        return 2

    offenders = []
    scanned = 0
    for p in sorted(scan_dir.glob("*.py")):
        scanned += 1
        bad = analyze(p, roster)
        if bad:
            offenders.append((p, bad))

    # Shell surface (). Counted separately: folding it into `scanned`
    # would silently change what that number has always meant.
    shell_scanned = 0
    shell_exempted = 0
    shell_offenders: list[Path] = []
    for p in sorted(scan_dir.glob("*.sh")):
        shell_scanned += 1
        if analyze_shell(p):
            if p.name in SHELL_ALLOWLIST:
                shell_exempted += 1   # what this check is CURRENTLY suppressing
            else:
                shell_offenders.append(p)

    # A stale allowlist entry is itself a failure: the fix landed and the
    # exemption is now suppressing nothing while still reading as coverage.
    stale_allowlist = [
        (name, goal) for name, goal in sorted(SHELL_ALLOWLIST.items())
        if (scan_dir / name).exists() and not analyze_shell(scan_dir / name)
    ]

    print(f"roster ({len(roster)}, derived at check time): {sorted(roster)}")
    print(f"scanned: {scanned} file(s) under {scan_dir}")
    print(f"shell-scanned: {shell_scanned} file(s), "
          f"{shell_exempted} matching but allowlisted to an owning goal")
    # Every failing class is REPORTED, then one verdict. An early return per
    # class would hide the others behind whichever happened to be checked
    # first — a report narrower than its own population, which is the defect
    # class this check exists to catch ().
    if offenders:
        print(f"FAIL: {len(offenders)} test file(s) write working memory for a LIVE roster agent:")
        for p, bad in offenders:
            print(f"  - {p.relative_to(PROJECT_ROOT) if PROJECT_ROOT in p.parents else p} -> {bad}")
        print("  Point the fixture at an OFF-ROSTER agent (see g-115-4887 and")
        print("  core/scripts/tests/test_post_state_update_gate_mode_only.py::_isolate_wm_cooldown).")
    if shell_offenders:
        print(f"FAIL: {len(shell_offenders)} shell test(s) write working memory "
              f"rooted at the AMBIENT $AGENT_DIR, i.e. whichever live agent runs them:")
        for p in shell_offenders:
            print(f"  - {p.relative_to(PROJECT_ROOT) if PROJECT_ROOT in p.parents else p}")
        print("  Point the test at a sandbox agent dir (see test-g3-worker-store-rails.sh,")
        print("  which builds AG=\"$TMP/agents/zeta\" under mktemp -d), or add an allowlist")
        print("  entry naming the goal that owns the fix.")
    if stale_allowlist:
        print(f"FAIL: {len(stale_allowlist)} SHELL_ALLOWLIST entr(y/ies) no longer match "
              f"the offender predicate — the fix landed, so the exemption is dead code "
              f"reading as coverage. Delete them:")
        for name, goal in stale_allowlist:
            print(f"  - {name} (was owned by {goal})")
    if offenders or shell_offenders or stale_allowlist:
        return 1
    print("PASS: no test writes working memory for a live-roster agent.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
