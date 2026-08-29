"""Pins core/scripts/domain-suite-gate.py ().

The gate refuses a status=completed close while the world's domain test suite
is red or uncollectable, IF a domain script changed since the goal's claim.
Every refusal below asserts the exact decision and is paired with a control on
the same fixture that MUST pass (guard-1082: `rc != 0` alone is satisfied by a
usage error). The gate runs as a subprocess against a tmp world through the
documented MIND_WORLD seam, and `--since` stands in for the claimed_at read so
no store is touched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
GATE = CORE_SCRIPTS / "domain-suite-gate.py"
PROJECT_ROOT = CORE_SCRIPTS.parent.parent

OLD = "2000-01-01T00:00:00"      # every file is newer than this → touched
FUTURE = "2999-01-01T00:00:00"   # nothing is newer than this → untouched

GREEN_TEST = "def test_ok():\n    assert 1 + 1 == 2\n"
BROKEN_IMPORT_TEST = "from pkg.config import mask  # noqa: F401\n\ndef test_never_runs():\n    assert False\n"
RED_TEST = "def test_red():\n    assert 1 == 2\n"


def _world(tmp_path: Path, tests: dict[str, str] | None = None, hook: str | None = None) -> Path:
    world = tmp_path / "world"
    scripts = world / "scripts"
    (scripts / "pkg").mkdir(parents=True)
    (scripts / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (scripts / "pkg" / "config.py").write_text("VALUE = 1\n", encoding="utf-8")
    if tests:
        (scripts / "tests").mkdir()
        for name, body in tests.items():
            (scripts / "tests" / name).write_text(body, encoding="utf-8")
    if hook is not None:
        (scripts / "run-domain-tests.sh").write_text(hook, encoding="utf-8")
    (tmp_path / "meta").mkdir(exist_ok=True)
    return world


def _run(tmp_path: Path, world: Path, *args: str):
    env = dict(os.environ)
    env.update({
        "MIND_WORLD": str(world),
        "MIND_META": str(tmp_path / "meta"),
        "MIND_AGENT": "testagent",
        "STORAGE_BACKEND": "local",
    })
    proc = subprocess.run(
        [sys.executable, str(GATE), "--goal", "g-999-01", "--source", "world", *args],
        cwd=str(PROJECT_ROOT), env=env, capture_output=True, text=True, timeout=300, check=False,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1, f"exactly one JSON line expected on stdout, got: {proc.stdout!r}"
    return proc.returncode, json.loads(lines[0]), proc.stderr


# ─── the block, and its control ───────────────────────────────────────────

def test_a_collection_error_in_a_touched_suite_refuses_the_close(tmp_path):
    world = _world(tmp_path, {"test_broken.py": BROKEN_IMPORT_TEST})
    rc, doc, err = _run(tmp_path, world, "--since", OLD)
    assert rc == 1
    assert doc["decision"] == "block"
    assert doc["rc"] == 2
    assert "COLLECT" in doc["reason"]
    assert any(t[0] == "tests/test_broken.py" for t in doc["touched"])
    assert "REFUSED status=completed for g-999-01" in err
    assert "Never override a collection error" in err
    assert "--override-domain-suite" in err


def test_a_green_touched_suite_passes(tmp_path):
    world = _world(tmp_path, {"test_green.py": GREEN_TEST})
    rc, doc, _ = _run(tmp_path, world, "--since", OLD)
    assert rc == 0
    assert doc["decision"] == "pass"
    assert doc["rc"] == 0
    assert doc["runner"].startswith("python -m pytest")


def _baseline(world: Path) -> dict:
    return json.loads((world / "domain-suite-baseline.json").read_text(encoding="utf-8"))


def test_the_first_red_run_seeds_the_baseline_and_passes(tmp_path):
    # A world can be red before the gate exists (the dev world was: 2 reds
    # nobody had seen). The first run records what is red and passes, so the
    # gate never refuses a close for a red that predates it.
    world = _world(tmp_path, {"test_red.py": RED_TEST})
    rc, doc, _ = _run(tmp_path, world, "--since", OLD)
    assert rc == 0
    assert doc["decision"] == "pass"
    assert "seeded baseline: 1 pre-existing red(s)" in doc["reason"]
    assert _baseline(world)["failing"] == ["tests/test_red.py::test_red"]


def test_a_new_red_after_the_seed_refuses_the_close(tmp_path):
    world = _world(tmp_path, {"test_red.py": RED_TEST})
    _run(tmp_path, world, "--since", OLD)  # seeds
    (world / "scripts" / "tests" / "test_red2.py").write_text("def test_red2():\n    assert 0\n", encoding="utf-8")
    rc, doc, err = _run(tmp_path, world, "--since", OLD)
    assert rc == 1
    assert doc["decision"] == "block"
    assert "1 NEW red(s)" in doc["reason"] and "tests/test_red2.py::test_red2" in doc["reason"]
    assert "REFUSED status=completed" in err
    # The baseline is untouched by a refusal: the old red stays the only entry.
    assert _baseline(world)["failing"] == ["tests/test_red.py::test_red"]


def test_pre_existing_reds_pass_and_the_baseline_ratchets_down(tmp_path):
    world = _world(tmp_path, {"test_red.py": RED_TEST, "test_red2.py": "def test_red2():\n    assert 0\n"})
    _run(tmp_path, world, "--since", OLD)  # seeds with two reds
    assert len(_baseline(world)["failing"]) == 2
    # Same reds again: pass. Fix one: pass, and the baseline shrinks to what still fails.
    rc, doc, _ = _run(tmp_path, world, "--since", OLD)
    assert rc == 0 and doc["decision"] == "pass" and "2 pre-existing red(s), none new" in doc["reason"]
    (world / "scripts" / "tests" / "test_red2.py").write_text(GREEN_TEST, encoding="utf-8")
    rc, doc, _ = _run(tmp_path, world, "--since", OLD)
    assert rc == 0 and doc["decision"] == "pass"
    assert _baseline(world)["failing"] == ["tests/test_red.py::test_red"]
    # Fix the last one: green writes an EMPTY baseline, so the red cannot come back unnoticed.
    (world / "scripts" / "tests" / "test_red.py").write_text(GREEN_TEST, encoding="utf-8")
    rc, doc, _ = _run(tmp_path, world, "--since", OLD)
    assert rc == 0 and doc["reason"] == "domain suite green"
    assert _baseline(world)["failing"] == []
    (world / "scripts" / "tests" / "test_red.py").write_text(RED_TEST, encoding="utf-8")
    rc, doc, _ = _run(tmp_path, world, "--since", OLD)
    assert rc == 1 and doc["decision"] == "block"


def test_a_collection_error_blocks_even_with_a_baseline(tmp_path):
    world = _world(tmp_path, {"test_red.py": RED_TEST})
    _run(tmp_path, world, "--since", OLD)  # seeds
    (world / "scripts" / "tests" / "test_broken.py").write_text(BROKEN_IMPORT_TEST, encoding="utf-8")
    rc, doc, _ = _run(tmp_path, world, "--since", OLD)
    assert rc == 1 and doc["decision"] == "block" and doc["rc"] == 2


def test_a_red_run_that_names_no_unit_blocks(tmp_path):
    # A runner that exits 1 without naming a failing unit cannot be ratcheted:
    # "cannot prove pre-existing" is a block, not a pass.
    world = _world(tmp_path, {"test_green.py": GREEN_TEST}, hook="#!/usr/bin/env bash\necho 'something went wrong'\nexit 1\n")
    rc, doc, _ = _run(tmp_path, world, "--since", OLD)
    assert rc == 1 and doc["decision"] == "block"
    assert "no failing unit could be identified" in doc["reason"]


def test_a_timeout_fails_open_with_a_warning(tmp_path):
    world = _world(tmp_path, {"test_green.py": GREEN_TEST}, hook="#!/usr/bin/env bash\nsleep 5\nexit 0\n")
    rc, doc, err = _run(tmp_path, world, "--since", OLD, "--timeout", "1")
    assert rc == 0
    assert doc["decision"] == "error"
    assert "exceeded 1s" in doc["reason"]
    assert "NOT verified" in err
    assert not (world / "domain-suite-baseline.json").exists()


def test_failing_ids_reads_both_output_shapes():
    import importlib.util
    spec = importlib.util.spec_from_file_location("domain_suite_gate_t", GATE)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(CORE_SCRIPTS))
    spec.loader.exec_module(mod)
    lines = [
        "FAILED tests/test_a.py::test_x - AssertionError: boom",
        "ERROR tests/test_b.py - ImportError",
        "[57/77] FAIL test_ssm_run_send_deny_backoff.sh (rc=1)",
        "  FAIL the tag alone decides whether it retries (A=3, B=3)",
        "FAIL pytest-batch",
        "74 passed in 1.0s",
    ]
    assert mod.failing_ids(lines) == {
        "tests/test_a.py::test_x", "tests/test_b.py", "test_ssm_run_send_deny_backoff.sh",
        "the", "pytest-batch",
    }


# ─── the trigger ──────────────────────────────────────────────────────────

def test_an_untouched_suite_is_a_noop_without_running_anything(tmp_path):
    world = _world(tmp_path, {"test_broken.py": BROKEN_IMPORT_TEST})
    rc, doc, _ = _run(tmp_path, world, "--since", FUTURE)
    assert rc == 0
    assert doc["decision"] == "noop"
    assert "no domain script modified since" in doc["reason"]
    assert "rc" not in doc


def test_a_world_without_domain_tests_is_a_noop(tmp_path):
    world = _world(tmp_path, tests=None)
    rc, doc, _ = _run(tmp_path, world, "--since", OLD)
    assert rc == 0
    assert doc["decision"] == "noop"
    assert "no domain test suite" in doc["reason"]


def test_only_code_files_count_as_touched(tmp_path):
    world = _world(tmp_path, {"test_broken.py": BROKEN_IMPORT_TEST})
    scripts = world / "scripts"
    # Age every code file past the window, then drop a fresh LOG and JSON
    # artifact beside them: a test run that writes its own results must not
    # re-trigger the gate on every later close.
    old = time.time() - 7 * 24 * 3600
    for p in scripts.rglob("*"):
        if p.is_file():
            os.utime(p, (old, old))
    (scripts / "calls.log").write_text("x\n", encoding="utf-8")
    (scripts / "results.json").write_text("{}\n", encoding="utf-8")
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 3 * 24 * 3600))
    rc, doc, _ = _run(tmp_path, world, "--since", since)
    assert rc == 0
    assert doc["decision"] == "noop"
    # Control on the same fixture: touching a .py inside the window fires it.
    (scripts / "pkg" / "config.py").write_text("VALUE = 2\n", encoding="utf-8")
    rc, doc, _ = _run(tmp_path, world, "--since", since)
    assert rc == 1 and doc["decision"] == "block"


# ─── the override ─────────────────────────────────────────────────────────

def test_an_override_passes_and_writes_one_ledger_row(tmp_path):
    world = _world(tmp_path, {"test_broken.py": BROKEN_IMPORT_TEST})
    rc, doc, _ = _run(tmp_path, world, "--since", OLD, "--override", "g-000-00: pre-existing, tracked")
    assert rc == 0
    assert doc["decision"] == "override"
    ledger = world / "domain-suite-overrides.jsonl"
    rows = [json.loads(ln) for ln in ledger.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(rows) == 1
    assert rows[0]["goal_id"] == "g-999-01"
    assert rows[0]["reason"] == "g-000-00: pre-existing, tracked"
    assert rows[0]["agent"] == "testagent"
    assert rows[0]["rc"] == 2


def test_an_override_on_a_green_suite_writes_no_ledger_row(tmp_path):
    world = _world(tmp_path, {"test_green.py": GREEN_TEST})
    rc, doc, _ = _run(tmp_path, world, "--since", OLD, "--override", "not needed")
    assert rc == 0 and doc["decision"] == "pass"
    assert not (world / "domain-suite-overrides.jsonl").exists()


# ─── the runner hook ──────────────────────────────────────────────────────

def test_the_world_runner_hook_takes_precedence_over_pytest(tmp_path):
    # A broken pytest suite beside a hook that exits 0: the hook is the
    # world's declared contract (domain-hooks.md Pattern B), so it wins.
    world = _world(tmp_path, {"test_broken.py": BROKEN_IMPORT_TEST},
                   hook="#!/usr/bin/env bash\necho HOOK-RAN\nexit 0\n")
    rc, doc, _ = _run(tmp_path, world, "--since", OLD)
    assert rc == 0
    assert doc["decision"] == "pass"
    assert doc["runner"] == "scripts/run-domain-tests.sh"


def test_a_failing_world_runner_hook_seeds_then_refuses_a_new_red(tmp_path):
    world = _world(tmp_path, {"test_green.py": GREEN_TEST},
                   hook="#!/usr/bin/env bash\necho 'FAIL tests/test_x.sh'\nexit 1\n")
    rc, doc, _ = _run(tmp_path, world, "--since", OLD)
    assert rc == 0 and doc["decision"] == "pass" and "seeded" in doc["reason"]
    assert doc["runner"] == "scripts/run-domain-tests.sh"
    assert _baseline(world)["failing"] == ["tests/test_x.sh"]
    (world / "scripts" / "run-domain-tests.sh").write_text(
        "#!/usr/bin/env bash\necho 'FAIL tests/test_x.sh'\necho '[2/2] FAIL tests/test_y.sh (rc=1)'\nexit 1\n", encoding="utf-8")
    rc, doc, _ = _run(tmp_path, world, "--since", OLD)
    assert rc == 1
    assert doc["decision"] == "block"
    assert "tests/test_y.sh" in doc["reason"] and "tests/test_x.sh" not in doc["reason"].split(":")[-1]
    assert any("FAIL tests/test_y.sh" in ln for ln in doc["tail"])


def test_the_runner_is_pinned_to_the_local_backend(tmp_path):
    hook = "#!/usr/bin/env bash\n[ \"${STORAGE_BACKEND:-}\" = local ] || { echo NOT-PINNED; exit 1; }\nexit 0\n"
    world = _world(tmp_path, {"test_green.py": GREEN_TEST}, hook=hook)
    env_backend = os.environ.get("STORAGE_BACKEND")
    try:
        os.environ["STORAGE_BACKEND"] = "own-cloud"
        rc, doc, _ = _run(tmp_path, world, "--since", OLD)
    finally:
        if env_backend is None:
            os.environ.pop("STORAGE_BACKEND", None)
        else:
            os.environ["STORAGE_BACKEND"] = env_backend
    # _run re-pins STORAGE_BACKEND=local in the subprocess env (guard-955), and
    # the gate pins it again for the runner; either way the hook must see local.
    assert rc == 0 and doc["decision"] == "pass"


# ─── the wiring ───────────────────────────────────────────────────────────

def test_iteration_close_calls_the_gate_before_the_status_write():
    src = (CORE_SCRIPTS / "iteration-close.sh").read_text(encoding="utf-8")
    verify_start = src.index("do_verify() {")
    gate_call = src.index('domain-suite-gate.py', verify_start)
    status_write = src.index('update_cmd=("bash" "$SCRIPT_DIR/aspirations-update-goal.sh"', verify_start)
    assert verify_start < gate_call < status_write, "the gate must run inside do_verify, before the status write"
    assert "--override-domain-suite)" in src, "iteration-close.sh must accept --override-domain-suite"
    # The recovery hint carries the override so a refused close can be retried verbatim.
    assert '--override-domain-suite \\"$OVERRIDE_DOMAIN_SUITE\\"' in src


def test_bad_since_is_a_usage_error_not_a_block(tmp_path):
    world = _world(tmp_path, {"test_green.py": GREEN_TEST})
    env = dict(os.environ)
    env.update({"MIND_WORLD": str(world), "MIND_META": str(tmp_path / "meta"), "STORAGE_BACKEND": "local"})
    proc = subprocess.run([sys.executable, str(GATE), "--goal", "g-999-01", "--since", "yesterday"],
                          cwd=str(PROJECT_ROOT), env=env, capture_output=True, text=True, check=False)
    assert proc.returncode == 2
    assert "not an ISO timestamp" in proc.stderr


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
