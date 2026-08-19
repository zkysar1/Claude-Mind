#!/usr/bin/env python3
"""Pins for the  notification-routing sweep.

WHAT THIS COVERS that test_notification_routing_gate.py does not: that file pins
`decide()`, the pure policy function. This file pins the WIRING added so the
direct email callers could use it -- `decide_and_log`, `post_suppression_
breadcrumb`, the CLI's --breadcrumb fail-open, and the two call-site properties
that are fragile in a way no policy test can see.

THE FAIL-SAFE DIRECTION IS INVERTED IN THIS SUBSYSTEM AND EVERY TEST BELOW
ASSERTS IN THAT DIRECTION. Everywhere else in this framework an ambiguous signal
resolves toward refusing; here it resolves toward SENDING, because the costs are
not symmetric (over-send = one unwanted email; under-send = a human-only alarm
reaching nobody, measured at six security alarms silent for five days on
g-335-1097). A test here that asserts "suppressed on error" would be pinning the
bug, so several tests below deliberately assert that a BROKEN component still
SENDS.

STORAGE_BACKEND is pinned local (guard-955): this box runs own-cloud, and
OwnCloudBackend derives its S3 key from customer_prefix+env_id+filename rather
than from any tmp override, so an unpinned subprocess write can truncate the
production store.
"""

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parent.parent

os.environ.setdefault("STORAGE_BACKEND", "local")
os.environ["STORAGE_BACKEND"] = "local"

sys.path.insert(0, str(CORE_SCRIPTS))
import notification_routing_gate as nrg  # noqa: E402

# guard-580: a bare "bash" argv[0] resolves via CreateProcess, which searches
# System32 BEFORE PATH and reaches the WSL launcher -- where it can block
# forever against a wedged LxssManager. BASH is the explicitly resolved
# interpreter; script paths go through .as_posix(), never str(), because bash
# silently strips the backslashes of a str(WindowsPath) (guard-581).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_helpers import BASH  # noqa: E402


# ---------------------------------------------------------------- decide_and_log


def test_decide_and_log_returns_label_reason_destination():
    label, reason, dest = nrg.decide_and_log(
        "completion", "Processor: success", "", caller="test")
    assert label == "suppress"
    assert reason
    # A SUPPRESS with no destination is the  defect; the caller is
    # entitled to assert on this field.
    assert dest, "SUPPRESS returned no destination"


def test_decide_and_log_always_send_categories_carry_no_destination():
    for cat in ("decision-needed", "user-digest"):
        label, _reason, dest = nrg.decide_and_log(cat, "x", "y", caller="test")
        assert label == "send", cat
        assert dest is None, "%s is ALWAYS_SEND; a destination implies re-routing" % cat


def test_decide_and_log_human_only_override_beats_fleet_handleable_category():
    label, reason, _dest = nrg.decide_and_log(
        "completion", "rotate the deploy credential", "", caller="test")
    assert label == "send"
    assert "human-only" in reason


def test_decide_and_log_never_raises_and_sends_on_bad_input():
    """The one failure mode that would defeat the design.

    Callers guard the IMPORT with try/except and send on failure. If this
    function raises AFTER a successful import, that guard has already been
    passed, and the exception propagates into a send path mid-notification --
    a notification reaching nobody, by a different road. So it must swallow and
    SEND.
    """
    label, reason, dest = nrg.decide_and_log(object(), "s", "b", caller="test")
    assert label == "send", "a raising input must resolve to SEND, not SUPPRESS"
    assert "fail-safe" in reason
    assert dest is None


def test_decide_and_log_survives_a_broken_gate_log(monkeypatch):
    """Observability is best-effort; the verdict is the contract."""
    import _gate_log

    def _boom(*_a, **_k):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(_gate_log, "log", _boom)
    label, _reason, _dest = nrg.decide_and_log("info", "s", "b", caller="test")
    assert label == "suppress", "a broken audit ledger must not change the verdict"


# ------------------------------------------------- post_suppression_breadcrumb


def test_breadcrumb_reports_failure_rather_than_raising(monkeypatch):
    """Must return (False, detail) so the caller can fall back to sending."""
    def _boom(*_a, **_k):
        raise OSError("board unreachable")

    monkeypatch.setattr(subprocess, "run", _boom)
    ok, detail = nrg.post_suppression_breadcrumb(
        "subj", "body", caller="test", reason="r")
    assert ok is False
    assert "exception" in detail


def test_breadcrumb_reports_failure_on_nonzero_exit(monkeypatch):
    class _P:
        returncode = 3
        stderr = "nope"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _P())
    ok, detail = nrg.post_suppression_breadcrumb("subj", caller="test")
    assert ok is False
    assert "nonzero" in detail


# ------------------------------------------------------------------------- CLI


def _cli(*args):
    return subprocess.run(
        [sys.executable, str(CORE_SCRIPTS / "notification_routing_gate.py")] + list(args),
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "STORAGE_BACKEND": "local"},
    )


def test_cli_exit_codes_are_the_verdict():
    assert _cli("--category", "decision-needed", "--subject", "s", "--quiet").returncode == 0
    assert _cli("--category", "completion", "--subject", "s", "--quiet").returncode == 1


def test_cli_unknown_category_sends():
    """Inverted fail-safe: nothing proves an unknown class is fleet-handleable."""
    assert _cli("--category", "not-a-real-category", "--subject", "s", "--quiet").returncode == 0


def test_cli_breadcrumb_failure_falls_back_to_send(tmp_path, monkeypatch):
    """The end-to-end half of the inverted fail-safe.

    A shell caller SKIPS its send on exit 1. So if --breadcrumb is passed and the
    breadcrumb does NOT land, exiting 1 would delete the notification instead of
    re-routing it. It must exit 0 (send) instead.

    Driven through main() in-process with a forced breadcrumb failure, because
    making board-post.sh genuinely fail from a subprocess is environment-dependent
    and would make this pin flaky rather than hermetic.
    """
    monkeypatch.setattr(nrg, "post_suppression_breadcrumb",
                        lambda *a, **k: (False, "forced"))
    rc = nrg.main(["--category", "completion", "--subject", "s",
                   "--breadcrumb", "--quiet"])
    assert rc == nrg.SEND, "an unlanded breadcrumb must flip SUPPRESS back to SEND"


def test_cli_breadcrumb_success_keeps_suppress(monkeypatch):
    monkeypatch.setattr(nrg, "post_suppression_breadcrumb",
                        lambda *a, **k: (True, "posted"))
    rc = nrg.main(["--category", "completion", "--subject", "s",
                   "--breadcrumb", "--quiet"])
    assert rc == nrg.SUPPRESS


# ------------------------------------------------------- shell wrapper contract


def test_wrapper_fails_open_to_send_on_a_non_verdict_rc():
    """A broken gate must let notifications THROUGH, never silence them.

    This is the defect that was live in notify-user/SKILL.md Step 1.5b until
    g-115-6422: it invoked `py -3 ...` directly and treated exit 1 as SUPPRESS,
    so an ImportError inside the module (which also exits 1) would have silently
    suppressed every notification.
    """
    proc = subprocess.run(
        [BASH, (CORE_SCRIPTS / "notification-routing-gate.sh").as_posix(),
         "--this-flag-does-not-exist"],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "STORAGE_BACKEND": "local"},
    )
    assert proc.returncode == 0, (
        "argparse error must fail OPEN to SEND, got rc=%d" % proc.returncode)


# ------------------------------------------------------ call-site fragility pins


# The word boundary in the gate's `\bbilling\b` pattern is what makes a billing
# alarm reach the human. These two prefixes are the ones billing-accuracy-guard.sh
# actually builds; the camelCase InfoType next to them does NOT match, so this is
# a live tripwire, not a theoretical one.
BILLING_TITLE_PREFIXES = ("Billing: ", "Billing accuracy: ")


def test_billing_titles_trip_the_human_only_class():
    for prefix in BILLING_TITLE_PREFIXES:
        label, reason, _d = nrg.decide_and_log(
            "blocker", prefix + "2 check(s) FAILED", "", caller="test")
        assert label == "send", (
            "%r no longer trips the human-only class — a money alarm would be "
            "SUPPRESSED. Fix the pattern or the title, do not delete this pin."
            % prefix)
        assert "billing" in reason.lower()


def test_billing_camelcase_infotype_does_not_match_which_is_why_the_pin_exists():
    """Positive control for the pin above (guard-2421).

    Without this, `test_billing_titles_trip_the_human_only_class` would still
    pass if the pattern were widened to match everything, and the pin would be
    silently vacuous.
    """
    label, _reason, _d = nrg.decide_and_log(
        "blocker", "BillingAccuracyAlert", "", caller="test")
    assert label == "suppress", (
        "the human-only match is no longer word-boundary-scoped; the billing "
        "pin above is now vacuous")


def test_live_billing_script_still_uses_a_pinned_title_prefix():
    """The pin above is about strings; this checks the live script still emits them.

    Skips LOUDLY when the world dir is not resolvable (world/ is an EXTERNAL,
    gitignored path, so this leg cannot run everywhere) — a silent pass here
    would read as coverage the test does not have.
    """
    try:
        from _paths import WORLD_DIR
    except Exception as exc:  # noqa: BLE001
        import pytest
        pytest.skip("world path not resolvable: %s" % exc)

    script = Path(WORLD_DIR) / "scripts" / "billing-accuracy-guard.sh"
    if not script.is_file():
        import pytest
        pytest.skip("billing-accuracy-guard.sh not present on this box")

    src = script.read_text(encoding="utf-8", errors="replace")
    titles = re.findall(r'title\s*=\s*\(?\s*f?"([^"]{0,40})', src)
    assert titles, "no `title =` assignments found — the regex or the script moved"
    assert all(any(t.startswith(p) for p in BILLING_TITLE_PREFIXES) for t in titles), (
        "a billing title no longer starts with a pinned prefix: %r. The gate "
        "matches \\bbilling\\b, so this would SUPPRESS a money alarm." % titles)


# --------------------------------- coverage-detective completeness (fresh-eyes)


def test_coverage_detective_reports_rc2_when_the_world_root_is_unresolvable():
    """An unresolvable world root must be LOUD (rc 2), never a clean rc 0.

    Found by /fresh-eyes-code on the same day the detective shipped. `_roots()`
    swallowed the `_paths` ImportError with a bare `pass`, so the world root
    silently vanished from the scan while `errors` stayed empty — main() then
    returned 0 and the report read "0 unrouted". Measured: files_scanned fell
    1401 -> 1195 and the three world/scripts senders were never examined.

    That is the rb-245 false clean the module's own docstring is written
    against, and the asymmetry is what hid it: `not root.is_dir()` recorded an
    error and exited 2, while the RESOLUTION failure exited 0. This pins the
    loud direction. It deliberately asserts on the SCAN SHAPE (fewer files +
    a non-empty errors list), not on an exact count, so it does not become a
    brittle population pin.
    """
    import importlib.util as _u

    class _BlockPaths:
        def find_spec(self, name, path=None, target=None):
            if name == "_paths":
                raise ImportError("simulated: _paths unimportable")
            return None

    blocker = _BlockPaths()
    saved = sys.modules.pop("_paths", None)
    sys.meta_path.insert(0, blocker)
    try:
        spec = _u.spec_from_file_location(
            "_cov_probe", CORE_SCRIPTS / "notification-routing-coverage.py")
        mod = _u.module_from_spec(spec)
        spec.loader.exec_module(mod)

        roots, root_errors = mod._roots()
        assert not any("world" in str(r) for r in roots), (
            "probe did not actually block the import — the world root is still "
            "present, so this test would pass vacuously")
        assert root_errors, "world root dropped with NO error recorded (rb-245 false clean)"

        res = mod.scan()
        assert res["errors"], "scan() did not propagate the root-resolution error"
        rc = 2 if res["errors"] else (1 if res["findings"] else 0)
        assert rc == 2, "unresolvable world root must exit 2, got %d" % rc
    finally:
        sys.meta_path.remove(blocker)
        if saved is not None:
            sys.modules["_paths"] = saved
