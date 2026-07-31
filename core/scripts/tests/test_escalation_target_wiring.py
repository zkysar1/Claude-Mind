"""Regression guard for the escalation-aspiration wiring ().

Framework canaries must resolve their escalation aspiration per deployment via
`_escalation_target.resolve()` + `source_flag()` instead of hardcoding a literal.
`asp-115` is the UPSTREAM deployment's recurring-infrastructure queue and exists
in no other deployment, so a literal there files nothing downstream — and these
callers log the add-goal failure as data rather than raising, so the escalation
failure never escalates.

THE TWO-HALF SHAPE THIS FILE PINS
---------------------------------
A naive "zero occurrences of the literal" test is WRONG HERE, and would have
reported the fix as a regression. The sanctioned pattern ends in a fail-open
FALLBACK ARM that deliberately re-introduces the literal:

    except Exception:
        ESCALATION_ASP, _VIA, ESCALATION_SOURCE = "asp-115", "fallback:...", "world"

so that a broken resolver is never WORSE than the literal it replaced. Wiring 13
sites therefore ADDED ~13 literal occurrences: a raw grep went 25 -> 27 while the
actual defect went 25 -> 1. Hence two tests that pull in opposite directions:

  * test_no_literal_asp_id_as_primary_value  — the defect must stay at zero
  * test_sanctioned_fallback_arms_survive    — the fallbacks must NOT be deleted

The second exists because the cheapest way to make the first "pass harder" is to
strip the fail-open arms, which would convert a loud degradation into a crash on
the iteration-close path. Neither test is meaningful without the other.

Hermetic: pure filesystem scan + tmp-dir fixtures. No daemon, no credentials.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from _escalation_target import resolve, source_flag  # noqa: E402

# parents: [0]=tests [1]=scripts [2]=core [3]=REPO. parents[2] here made every
# scan walk `<repo>/core/core/scripts`, which does not exist — so the offender
# scan found zero files and PASSED VACUOUSLY. Caught on the first run only by
# test_scan_corpus_is_non_vacuous + test_sanctioned_fallback_arms_survive below;
# a single-direction "assert no offenders" test would have shipped green forever
# against an empty corpus (rb-245 class, in this file's own harness).
REPO = Path(__file__).resolve().parents[3]
SCAN_ROOTS = ("core/scripts", "mind_api/src")

# Floor for the scanned-file count. Its only job is to fail loudly if the scan
# root ever breaks again; well below the real corpus so normal churn never trips it.
MIN_SCANNED_FILES = 200

# A literal aspiration id is a DEFECT only when it is the primary value in a
# goal-filing path. These three classes are legitimate and must not be counted.
FALLBACK_ARM = re.compile(
    r'fallback|except|\|\|\s*_|\bor\s+"asp-|environ\.get|_out=|CACHE\s*=|DEFAULT_CANDIDATES')
DOC_OR_HELP = re.compile(r'help\s*=|usage|e\.g\.|default:|>&2|print\(|"""')
FILING_PATH = re.compile(
    r'add[-_]goal|aspirations_add_goal|TARGET_ASP|ASP_ID\s*=|target_asp\s*=|'
    r'aspiration_id.*=|ASPIRATION=')

# Sites NOT routed through the local resolver, each with its reason. Two kinds:
# WRONG-FIX (the local resolver is the wrong tool) and QUARANTINED (a real
# defect, owned by a named open goal). Quarantine entries are deletions-in-
# waiting: when the owning goal lands, remove the entry and the predicate
# tightens on its own. Mirrors run-invisible-suites.sh's known-red quarantine.
#
# The predicate is deliberately asp-NNN-wide, not asp-115-only.  was
# scoped to the literal asp-115 and closed it (25 -> 1); this scan then proved
# that scope too narrow — the defect is hardcoding a deployment-specific
# aspiration id in a write path, and the same bug wears other numbers.
ALLOWLIST = {
    # WRONG-FIX. Names an aspiration in the TARGET world of a cross-world
    # injection. Resolving it against THIS deployment's queue would silently
    # inject a locally-resolved id into a remote world — a wrong fix that passes
    # every grep. Correct answer is caller-specified or target-resolved.
    "core/scripts/cross-world-inject-goal.sh": "wrong-fix: names a REMOTE world's queue (g-115-4216)",
    # DOC. This script IS the add-goal CLI; the literal is a usage example
    # inside its own --help heredoc, not a filing call.
    "core/scripts/aspirations-add-goal.sh": "doc: usage example in the --help heredoc",
    # QUARANTINED — real hardcodes with non-asp-115 literals, owned by .
    "core/scripts/stall-goal-filer.py": "quarantined: TARGET_ASP_ID='asp-240' (g-115-4216)",
    "core/scripts/insight-trigger-gate.py": "quarantined: files into 'asp-001' (g-115-4216)",
    "core/scripts/inactivity-detector.py": "quarantined: DEFAULT_TARGET_ASP='asp-001' (g-115-4216)",
    "core/scripts/cargo-cult-detector.py": "quarantined: target_asp='asp-001' (g-115-4216)",
}


def _iter_source_lines():
    """(relpath, lineno, line) for every executable line in the scan roots."""
    for root in SCAN_ROOTS:
        for path in sorted((REPO / root).rglob("*")):
            if path.suffix not in (".py", ".sh"):
                continue
            if "tests" in path.parts or "__pycache__" in path.parts:
                continue
            rel = path.relative_to(REPO).as_posix()
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            in_docstring = False
            for i, line in enumerate(lines, 1):
                if path.suffix == ".py":
                    if line.count('"""') % 2 == 1:
                        in_docstring = not in_docstring
                        continue
                    if in_docstring:
                        continue
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith("//"):
                    continue
                yield rel, i, line


def test_scan_corpus_is_non_vacuous():
    """Every scan test below is an ASSERT-NO-OFFENDERS, which an empty corpus
    satisfies trivially. Pin the corpus so a broken SCAN_ROOTS/REPO can never
    masquerade as a clean result (verify-before-assuming rule 5)."""
    for root in SCAN_ROOTS:
        assert (REPO / root).is_dir(), "scan root missing: %s" % (REPO / root)
    seen = {rel for rel, _, _ in _iter_source_lines()}
    assert len(seen) >= MIN_SCANNED_FILES, (
        "scan covered only %d files (floor %d) — the scan root is probably "
        "wrong, and every assert-no-offenders test here is passing vacuously."
        % (len(seen), MIN_SCANNED_FILES))


def test_no_literal_asp_id_as_primary_value():
    """No literal asp-NNN as the primary value in a goal-filing path."""
    offenders = []
    for rel, lineno, line in _iter_source_lines():
        if not re.search(r'"asp-\d{3}"|\basp-\d{3}\b', line):
            continue
        if FALLBACK_ARM.search(line) or DOC_OR_HELP.search(line):
            continue
        if not FILING_PATH.search(line):
            continue
        if rel in ALLOWLIST:
            continue
        offenders.append("%s:%d  %s" % (rel, lineno, line.strip()[:110]))

    assert not offenders, (
        "Literal aspiration id used as a primary value in a filing path.\n"
        "Route through _escalation_target.resolve()+source_flag() (python) or\n"
        "core/scripts/escalation-target.sh (shell). If the local resolver is\n"
        "genuinely the wrong tool (e.g. the id names a REMOTE world), add the\n"
        "file to ALLOWLIST above WITH the reason.\n  " + "\n  ".join(offenders))


def test_escalation_target_imports_are_fail_open():
    """Every import of _escalation_target must be inside a guarded try/except.

    Counterweight to the test above: the cheapest way to shrink a raw 'asp-115'
    grep is to delete the fail-open arms, which turns a loud degradation into an
    ImportError on the iteration-close path.

    This asserts the PROPERTY (each import site is guarded) rather than counting
    duplicate copies of the boilerplate. The count form is what a first draft of
    this file used, and it was wrong in a way that mattered: it pinned 13 copies
    of a try/except, so consolidating them into one shared helper — a legitimate
    and arguably better design — would have FAILED the guard while improving the
    code. A test that punishes the correct refactor teaches people to delete the
    test. (fresh-eyes F-2, g-115-4166.)
    """
    import ast

    unguarded = []
    for root in SCAN_ROOTS:
        for path in sorted((REPO / root).rglob("*.py")):
            if "tests" in path.parts or "__pycache__" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            rel = path.relative_to(REPO).as_posix()

            guarded = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Try):
                    for child in ast.walk(node):
                        if isinstance(child, ast.ImportFrom) and \
                                child.module == "_escalation_target":
                            guarded.add(child.lineno)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and \
                        node.module == "_escalation_target" and \
                        node.lineno not in guarded:
                    unguarded.append("%s:%d" % (rel, node.lineno))

    assert not unguarded, (
        "_escalation_target imported OUTSIDE a try/except — a resolver failure "
        "becomes an ImportError on the iteration-close path instead of a "
        "fail-open degradation to the upstream default:\n  "
        + "\n  ".join(unguarded))


def test_no_hardcoded_world_source_in_escalation_filing():
    """Filing calls must pass a resolved source, not a pinned 'world'.

    A resolved id filed with the wrong --source reproduces the original bug in
    a new costume: the id resolves, the store does not hold it, and the add
    fails aspiration_not_found again (see _escalation_target.source_flag).
    """
    offenders = []
    for rel, lineno, line in _iter_source_lines():
        if rel in ALLOWLIST or "_rt.py" in rel:
            continue  # _rt.py is the generic client; "world" is its API default
        hardcoded = ('source="world"' in line) or ('"--source", "world"' in line)
        if hardcoded and re.search(r"add[-_]goal|aspirations_add_goal", line):
            offenders.append("%s:%d  %s" % (rel, lineno, line.strip()[:110]))
    assert not offenders, (
        "Goal-filing call pins source='world' instead of source_flag().\n  "
        + "\n  ".join(offenders))


# --- Behavioural proof: the resolver actually diverges --------------------

def _write_queue(path, asp_ids):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join('{"id": "%s", "title": "t", "status": "active", "goals": []}\n' % a
                for a in asp_ids),
        encoding="utf-8")


def test_resolver_picks_asp_115_when_present(tmp_path):
    world, agent = tmp_path / "world", tmp_path / "agent"
    _write_queue(world / "aspirations.jsonl", ["asp-115", "asp-007"])
    _write_queue(agent / "aspirations.jsonl", ["asp-001"])
    asp, via = resolve(tmp_path / "core", world, agent)
    assert asp == "asp-115"
    assert via == "resolved:exists-in-queue"
    assert source_flag(asp, world, agent) == "world"


def test_resolver_falls_through_to_agent_queue_when_asp_115_absent(tmp_path):
    """The whole point: a deployment without asp-115 must NOT file into it."""
    world, agent = tmp_path / "world", tmp_path / "agent"
    _write_queue(world / "aspirations.jsonl", ["asp-007"])
    _write_queue(agent / "aspirations.jsonl", ["asp-001"])
    asp, via = resolve(tmp_path / "core", world, agent)
    assert asp == "asp-001", "must not fall back to the absent upstream literal"
    assert via == "resolved:exists-in-queue"
    # And the source must follow the id into the AGENT store, or the add fails
    # aspiration_not_found exactly as it did before the fix.
    assert source_flag(asp, world, agent) == "agent"


def test_resolver_returns_loud_default_when_nothing_exists(tmp_path):
    """Nothing resolvable -> keep the upstream default so the add fails LOUDLY."""
    world, agent = tmp_path / "world", tmp_path / "agent"
    _write_queue(world / "aspirations.jsonl", ["asp-999"])
    _write_queue(agent / "aspirations.jsonl", ["asp-998"])
    asp, via = resolve(tmp_path / "core", world, agent)
    assert asp == "asp-115"
    assert via == "fallback:none-exist"


# --- Shell wrapper contract ----------------------------------------------

def test_shell_wrapper_emits_parseable_pair():
    """escalation-target.sh must emit '<asp-id> <source>' on one line.

    Five shell callers parse it with ${_et%% *} / ${_et##* }, so the two-field
    single-line shape IS the contract.
    """
    from _runtime_bash import bash_cmd  # rb-1472: never a bare "bash" argv[0]
    script = (REPO / "core" / "scripts" / "escalation-target.sh").as_posix()
    proc = subprocess.run(bash_cmd(script), capture_output=True, text=True,
                          timeout=60, cwd=str(REPO))
    assert proc.returncode == 0, proc.stderr
    parts = proc.stdout.strip().split()
    assert len(parts) == 2, "expected '<asp-id> <source>', got %r" % proc.stdout
    assert re.fullmatch(r"asp-\d{3}", parts[0]), parts[0]
    assert parts[1] in ("world", "agent"), parts[1]
