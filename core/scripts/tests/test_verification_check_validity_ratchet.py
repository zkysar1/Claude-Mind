"""Pins the STATIC classifier in core/scripts/verification-check-validity-ratchet.py.

WHY THIS FILE EXISTS AT ALL. The sweep it covers is a DETECTOR, and a detector
nobody has watched fire is indistinguishable from one that cannot fire. That is
not hypothetical here: on 2026-09-01 the sibling Layer-E detector
`anchor-tripwire.py` was found to have NO test anywhere in the tree after 57
green runs, and to have a predicate that reported OK on a fixture where the
constitutional anchor was writable. Shipping a new detective without a control
would repeat that defect on the same day it was measured.

THE THREE THINGS PINNED, in order of what would hurt most if it broke:

1. VOCABULARY NORMALIZATION IS APPLIED. `predicate.normalize_check` resolves 8
   type aliases and 5 not-machine-checkable aliases before dispatch, plus
   type-scoped field aliases. A scan that skips it reports VALID checks as
   broken — measured: the seed re-measurement for g-115-5195 counted
   `test_check`, `command_check` and `manual` as unknown types when all three
   are accepted vocabulary. Those exact three are cases below.

2. THE CLASSIFIER IS NOT STUCK IN ONE POSITION. Every bucket has at least one
   case, prose and unknown-type included, so a classifier hard-wired to "valid"
   fails here rather than silently reporting a clean corpus forever.

3. THE REQUIRED-FIELD TABLE REFUSES TO GO STALE. `_assert_table_complete` must
   RAISE when the live registry gains a type the table does not declare. An
   assertion that never fires is worth nothing, so the test simulates a tenth
   predicate type rather than merely asserting the current table passes.

Plus the standing constraint from the goal: the sweep must NEVER call
`predicate.evaluate()`, which executes commands (a corpus sweep using it timed
out at 300s and would run arbitrary `command_succeeds` bodies fleet-wide). That
is pinned behaviourally — evaluate is replaced with a raiser — not just by
grepping the source, so a future refactor that reintroduces it fails here.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
_MODULE_PATH = _SCRIPTS / "verification-check-validity-ratchet.py"


def _load():
    """Fresh module object per test — the classifier is pure over its table."""
    sys.path.insert(0, str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "verification_check_validity_ratchet", _MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


# (label, check, expected bucket)
CASES = [
    ("prose string", "the agent confirms it looks right", "prose"),
    ("prose list", ["not", "a", "dict"], "prose"),
    ("canonical file_check", {"type": "file_check", "path": "a.txt"}, "valid"),
    ("file_check missing path", {"type": "file_check"}, "malformed"),
    # --- type aliases: the class the un-normalized seed scan got wrong --------
    ("alias test_check", {"type": "test_check", "command": "x"}, "valid"),
    ("alias command_check", {"type": "command_check", "command": "x"}, "valid"),
    ("alias grep", {"type": "grep", "command": "x"}, "valid"),
    ("alias artifact_check", {"type": "artifact_check", "path": "a"}, "valid"),
    ("nmc alias manual", {"type": "manual"}, "valid"),
    ("nmc alias llm_verify", {"type": "llm_verify"}, "valid"),
    # --- field aliases -------------------------------------------------------
    ("field alias file->path", {"type": "file_check", "file": "a.txt"}, "valid"),
    ("field alias cmd->command", {"type": "command_check", "cmd": "x"}, "valid"),
    # --- unknown / malformed -------------------------------------------------
    ("bogus type", {"type": "wishful_thinking"}, "unknown_type"),
    ("no type field", {"description": "hi"}, "unknown_type"),
    ("metric_threshold no min/max",
     {"type": "metric_threshold", "command": "x"}, "malformed"),
    ("metric_threshold with min",
     {"type": "metric_threshold", "command": "x", "min": 1}, "valid"),
    ("vcs one-of satisfied",
     {"type": "vcs_commits_since", "after_ref": "git:HEAD"}, "valid"),
    ("vcs one-of missing",
     {"type": "vcs_commits_since", "repo": "."}, "malformed"),
    ("pr_merged partial", {"type": "pr_merged", "repo": "o/n"}, "malformed"),
    ("after_time partial",
     {"type": "after_time", "anchor": "2026-01-01T00:00:00"}, "malformed"),
]


@pytest.mark.parametrize("label,check,expected",
                         CASES, ids=[c[0] for c in CASES])
def test_classifier_buckets(mod, label, check, expected):
    bucket, _detail = mod._classify(check)
    assert bucket == expected, f"{label}: got {bucket}, want {expected}"


def test_every_bucket_is_reachable(mod):
    """Guards against a classifier stuck in one position (see docstring #2)."""
    seen = {mod._classify(c)[0] for _l, c, _e in CASES}
    assert seen == {"prose", "unknown_type", "malformed", "valid"}


def test_required_field_table_covers_live_registry(mod):
    """The shipped table must declare every canonical type in use today."""
    mod._assert_table_complete()


def test_table_completeness_assertion_actually_fires(mod):
    """An assertion that never fires is worth nothing — simulate a new type."""
    mod.PREDICATE_TYPES["brand_new_type_2027"] = lambda p: None
    try:
        with pytest.raises(RuntimeError, match="REQUIRED_FIELDS is stale"):
            mod._assert_table_complete()
    finally:
        mod.PREDICATE_TYPES.pop("brand_new_type_2027", None)


def test_classifier_never_calls_evaluate(mod):
    """predicate.evaluate() EXECUTES commands and must never touch the corpus.

    Behavioural, not a grep: evaluate is replaced with a raiser, so a refactor
    that routes classification through it fails here.
    """
    import predicate  # noqa: E402

    original = predicate.evaluate

    def _forbidden(*_a, **_kw):
        raise AssertionError(
            "the static sweep called predicate.evaluate() — it executes "
            "command_succeeds bodies and must never run over the corpus")

    predicate.evaluate = _forbidden
    try:
        for _label, check, _expected in CASES:
            mod._classify(check)
    finally:
        predicate.evaluate = original


def test_missing_required_resolves_field_aliases_before_testing(mod):
    """`file:` satisfies file_check's required `path` only after normalization."""
    from predicate import normalize_check  # noqa: E402
    assert mod._missing_required(
        normalize_check({"type": "file_check", "file": "a.txt"})) == []
    # ...and the un-normalized form is genuinely missing it, so the test above
    # is not vacuously true.
    assert mod._missing_required({"type": "file_check", "file": "a.txt"}) == ["path"]


def test_ratcheted_metric_is_a_count_not_a_ratio(mod):
    """audit-baselines.md forbids baselining a ratio; the rates are reported only.

    Pinning the KEY names rather than the values: if someone later ratchets
    `structured_share_bp`, the anti-detector zeta identified is back.
    """
    src = _MODULE_PATH.read_text(encoding="utf-8")
    assert '"ratcheted_metric": "unevaluatable_structured"' in src
    assert "reported_not_ratcheted" in src
