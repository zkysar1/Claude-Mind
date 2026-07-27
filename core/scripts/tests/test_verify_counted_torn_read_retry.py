#!/usr/bin/env python3
"""test_verify_counted_torn_read_retry.py —  regression test.

Pins the bounded-read-retry behavior added to `_verify_counted` in
loop-state-bump-counters.py (g-115-1495).

Origin of bug: the g-115-1470 self-heal lets iteration-close.sh detect a
silent bump no-op via `--verify-counted` and re-fire once. But `_verify_counted`
did a single bare read and fail-open'd to exit 0 ("indeterminate -> no re-fire")
on ANY read exception. A torn read here correlates with the SAME OneDrive+daemon
write contention that causes the original bump no-op, so the verify read often
failed in the SAME window the bump did (correlated failures) -> exit 0 ->
re-fire skipped -> no ledger entry -> the goal stayed permanently uncounted
(g-115-1486 in alpha's loop_state). The write path is wrapped in
loop_state_cas_retry; the verify read had no equivalent robustness — that
asymmetry was the bug.

Fix (g-115-1495): bounded read retry (_VERIFY_READ_RETRIES attempts,
_VERIFY_READ_BACKOFF_S between). A transient torn read that recovers within the
budget yields the DEFINITIVE answer (0 counted / 1 absent); only a genuinely
unreadable state after all retries falls through to the conservative exit 0.

These tests exercise `_verify_counted` IN-PROCESS (the subprocess-based
verify tests in test_loop_state_bump_idempotency.py cannot inject a transient
read failure) via a flaky wm_path stand-in.

Refs: g-115-1495 (this fix), g-115-1470 (the self-heal it hardens),
g-115-1486 (the permanently-lost goal), g-115-664 (idempotent re-fire),
g-115-1394 (loop_state_cas_retry on the write path),
core/scripts/loop-state-bump-counters.py.
"""
import importlib.util
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("FAIL: PyYAML not installed", file=sys.stderr)
    sys.exit(2)


SCRIPT_DIR = Path(__file__).resolve().parent.parent  # core/scripts/
HELPER = SCRIPT_DIR / "loop-state-bump-counters.py"


def _load_module():
    """Load the hyphenated helper as an importable module.

    core/scripts/ must be on sys.path so the helper's `from _paths import ...`
    and `from _fileops import ...` resolve. The helper's import guard does
    `sys.exit(0)` if those fail — convert that landmine into a loud error so a
    broken test env can't masquerade as a passing (no-op) test.
    """
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    spec = importlib.util.spec_from_file_location("loop_state_bump_counters", HELPER)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit as e:
        raise RuntimeError(
            "loop-state-bump-counters.py called sys.exit() on import "
            f"(yaml/_paths/_fileops import guard tripped, code={e.code}) — "
            "cannot test _verify_counted in-process"
        )
    return mod


MOD = _load_module()
# Zero the backoff so the retry loop spins without real sleeps (keeps the
# suite fast); the retry COUNT (_VERIFY_READ_RETRIES) is what we assert against.
MOD._VERIFY_READ_BACKOFF_S = 0


class _FlakyPath:
    """A wm_path stand-in whose read_text() raises OSError (a torn read while a
    writer holds the file) for the first `fail_times` calls, then returns
    `content`. Tracks `calls` so a test can assert exactly how many reads the
    retry loop performed."""

    def __init__(self, fail_times, content):
        self.fail_times = fail_times
        self.content = content
        self.calls = 0

    def read_text(self, encoding="utf-8"):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise OSError("simulated torn read")
        return self.content


def _wm_yaml(counted):
    """Serialize a minimal WM whose loop_state.counted_goals_this_session is
    `counted` (a list)."""
    return yaml.safe_dump(
        {"slots": {"loop_state": {"counted_goals_this_session": counted}}},
        sort_keys=False,
    )


def test_transient_torn_read_recovers_to_absent():
    """THE FIX: a torn read that recovers within the retry budget returns the
    DEFINITIVE answer. Goal absent -> exit 1 (re-fire). Pre-fix the first torn
    read fail-open'd to 0 and the self-heal never fired (g-115-1486)."""
    retries = MOD._VERIFY_READ_RETRIES
    assert retries >= 2, f"retry budget must be >= 2 to test recovery, got {retries}"
    fp = _FlakyPath(fail_times=retries - 1, content=_wm_yaml(["g-other"]))
    rc = MOD._verify_counted(fp, "g-absent")
    assert rc == 1, f"expected 1 (absent -> re-fire) after transient torn reads, got {rc}"
    assert fp.calls == retries, (
        f"expected exactly {retries} read attempts (fail {retries - 1}, succeed on "
        f"last), got {fp.calls}"
    )


def test_transient_torn_read_recovers_to_present():
    """A torn read that recovers and finds the goal counted -> exit 0
    (bump landed, no re-fire)."""
    fp = _FlakyPath(fail_times=2, content=_wm_yaml(["g-present"]))
    rc = MOD._verify_counted(fp, "g-present")
    assert rc == 0, f"expected 0 (present -> no re-fire) after recovery, got {rc}"
    assert fp.calls == 3, f"expected 3 read attempts (fail 2, succeed 3rd), got {fp.calls}"


def test_persistent_torn_read_falls_through_to_zero():
    """A read that NEVER recovers within the budget falls through to the
    conservative exit 0 (genuinely indeterminate) — preserving the original
    'a transient glitch must not trigger a spurious re-fire' intent for the
    truly-unreadable case."""
    retries = MOD._VERIFY_READ_RETRIES
    fp = _FlakyPath(fail_times=999, content=_wm_yaml(["g-x"]))
    rc = MOD._verify_counted(fp, "g-x")
    assert rc == 0, f"expected 0 (indeterminate after exhausting retries), got {rc}"
    assert fp.calls == retries, (
        f"expected exactly {retries} read attempts before giving up, got {fp.calls}"
    )


def test_clean_read_no_wasted_retry():
    """A clean first read costs exactly one attempt — the retry loop must not
    waste reads when there is no contention. Goal absent -> exit 1."""
    fp = _FlakyPath(fail_times=0, content=_wm_yaml(["g-other"]))
    rc = MOD._verify_counted(fp, "g-absent")
    assert rc == 1, f"expected 1 (absent), got {rc}"
    assert fp.calls == 1, f"clean read must take exactly 1 attempt, got {fp.calls}"


def _run(test_fn):
    try:
        test_fn()
        print(f"PASS: {test_fn.__name__}")
        return True
    except AssertionError as e:
        print(f"FAIL: {test_fn.__name__} — {e}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"ERROR: {test_fn.__name__} — {e}", file=sys.stderr)
        return False


def main() -> int:
    tests = [
        test_transient_torn_read_recovers_to_absent,
        test_transient_torn_read_recovers_to_present,
        test_persistent_torn_read_falls_through_to_zero,
        test_clean_read_no_wasted_retry,
    ]
    results = [_run(t) for t in tests]
    if all(results):
        print("\n════════════════════════════════════════════")
        print(f"  ALL {len(results)} TESTS PASS — verify-counted torn-read retry pinned")
        print("════════════════════════════════════════════")
        return 0
    fail_count = sum(1 for r in results if not r)
    print(f"\nFAIL: {fail_count}/{len(results)} test(s) failed", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
