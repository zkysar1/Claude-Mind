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


def _run(tmp_path: Path, world: Path, *args: str, log_dir: Path | None = None):
    env = dict(os.environ)
    env.update({
        "MIND_WORLD": str(world),
        "MIND_META": str(tmp_path / "meta"),
        "MIND_AGENT": "testagent",
        "STORAGE_BACKEND": "local",
        # A NON-CLEAN run now retains its log (), so every test here
        # is a potential writer. Point that at THIS test's tmp dir: a gate's own
        # suite must not leave artifacts in the live tree. Measured before this
        # pin existed — 7 stray -*.log files in core/logs/.
        "DOMAIN_SUITE_LOG_DIR": str(log_dir or (tmp_path / "retained")),
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


def _announced(err: str) -> list[str]:
    return [ln for ln in err.splitlines() if ln.startswith("[domain-suite-gate] running")]


def test_it_says_it_is_running_the_suite_before_it_starts(tmp_path):
    # : worker Bodies that were not told the close runs the suite killed it
    # and wrote the status by hand. The line names the gate, what fired it and how
    # long to expect; from the second run it carries the last run's length here.
    world = _world(tmp_path, {"test_green.py": GREEN_TEST})
    rc, _doc, err = _run(tmp_path, world, "--since", OLD)
    assert rc == 0
    (line,) = _announced(err)
    assert "domain script(s) changed since the claim" in line and "tests/test_green.py" in line
    assert "Expect up to 15 min." in line  # no earlier run to go by
    assert isinstance(_baseline(world)["seconds"], int)

    rc, _doc, err = _run(tmp_path, world, "--since", OLD)
    assert rc == 0
    (line,) = _announced(err)
    assert "Expect up to 15 min; the last run on this box took under a minute." in line


def test_a_noop_announces_no_suite(tmp_path):
    # The control: nothing changed since the claim, so no suite runs and no line says one does.
    world = _world(tmp_path, {"test_green.py": GREEN_TEST})
    rc, doc, err = _run(tmp_path, world, "--since", FUTURE)
    assert rc == 0 and doc["decision"] == "noop"
    assert _announced(err) == []


def _baseline(world: Path) -> dict:
    return json.loads((world / "domain-suite-baseline.json").read_text(encoding="utf-8"))


def _age_all(world: Path, days: int = 7) -> None:
    old = time.time() - days * 24 * 3600
    for p in (world / "scripts").rglob("*"):
        if p.is_file():
            os.utime(p, (old, old))


def _since_1h() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 3600))


def _seed(tmp_path: Path, world: Path):
    """Seed the baseline the way a real close does: the reds sit in OLD files,
    and the unit touched only a module (pkg/config.py), never the red tests."""
    _age_all(world)
    (world / "scripts" / "pkg" / "config.py").write_text("VALUE = 2\n", encoding="utf-8")
    return _run(tmp_path, world, "--since", _since_1h())


def test_the_first_red_run_seeds_the_baseline_and_passes(tmp_path):
    # A world can be red before the gate exists (the dev world was: 2 reds
    # nobody had seen). The first run records what is red and passes, so the
    # gate never refuses a close for a red that predates it.
    world = _world(tmp_path, {"test_red.py": RED_TEST})
    rc, doc, _ = _seed(tmp_path, world)
    assert rc == 0
    assert doc["decision"] == "pass"
    assert "seeded baseline: 1 pre-existing red(s)" in doc["reason"]
    assert _baseline(world)["failing"] == ["tests/test_red.py::test_red"]


def test_seeding_never_launders_a_red_in_a_file_this_unit_touched(tmp_path):
    # Measured on the first live seed of a deployment (2026-08-29): 63 reds
    # recorded, 2 of them in a test file the closing unit had written 20 min
    # earlier. A red in a touched file is the unit's own, baseline or not.
    world = _world(tmp_path, {"test_red.py": RED_TEST, "test_old_red.py": "def test_old():\n    assert 0\n"})
    _age_all(world)
    (world / "scripts" / "tests" / "test_red.py").write_text(RED_TEST, encoding="utf-8")  # touched now
    rc, doc, err = _run(tmp_path, world, "--since", _since_1h())
    assert rc == 1
    assert doc["decision"] == "block"
    assert "cannot be called pre-existing" in doc["reason"]
    assert "tests/test_red.py::test_red" in doc["reason"]
    assert "1 other red(s) will be recorded" in doc["reason"]
    assert not (world / "domain-suite-baseline.json").exists()
    # Control: fix the touched red and the seed records the untouched one.
    (world / "scripts" / "tests" / "test_red.py").write_text(GREEN_TEST, encoding="utf-8")
    rc, doc, _ = _run(tmp_path, world, "--since", _since_1h())
    assert rc == 0 and "seeded baseline: 1 pre-existing red(s)" in doc["reason"]
    assert _baseline(world)["failing"] == ["tests/test_old_red.py::test_old"]


def test_a_new_red_after_the_seed_refuses_the_close(tmp_path):
    world = _world(tmp_path, {"test_red.py": RED_TEST})
    _seed(tmp_path, world)
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
    _seed(tmp_path, world)  # seeds with two reds
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
    _seed(tmp_path, world)
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
        "[6/77] FAIL pytest batch (rc=1)",
        "  FAIL the tag alone decides whether it retries (A=3, B=3)",  # inner assertion: NOT an id
        "FAIL tests/test_x.sh",
        "  pytest-batch",  # the runner's trailing list has no FAIL prefix: not an id
        "74 passed in 1.0s",
    ]
    assert mod.failing_ids(lines) == {
        "tests/test_a.py::test_x", "tests/test_b.py", "test_ssm_run_send_deny_backoff.sh",
        "pytest batch", "tests/test_x.sh",
    }
    # Measured on the first live seed (2026-08-29): the inner line produced the id "the",
    # which would have laundered every later inner failure starting with that word.
    assert "the" not in mod.failing_ids(lines)


# ─── the credential tripwire (), and its controls ─────────────────

def _private_file(tmp_path: Path) -> Path:
    """A deployment's .env.local beside its world: mode 0600, credential-shaped name."""
    p = tmp_path / ".env.local"
    p.write_text("TOKEN=real\n", encoding="utf-8")
    p.chmod(0o600)
    return p


def _clobbering_hook(secret: Path) -> str:
    return f"#!/usr/bin/env bash\nprintf 'TOKEN=placeholder\\n' > {secret}\nexit 0\n"


def test_a_green_suite_that_rewrites_a_credential_file_refuses_the_close(tmp_path):
    """The 2026-08-29 clobber: the hook exits GREEN, but a mocked refresh persisted
    over the live token file. Green is exactly the point — rc alone passes."""
    secret = _private_file(tmp_path)
    world = _world(tmp_path, {"test_green.py": GREEN_TEST}, hook=_clobbering_hook(secret))
    rc, doc, err = _run(tmp_path, world, "--since", OLD)
    assert rc == 1
    assert doc["decision"] == "block"
    assert doc["rc"] == 0
    assert doc["clobbered"] == [str(secret)]
    # : the refusal now names what the instrument can support — CONTENTS
    # differ, established by sha256 rather than by an mtime bump.
    assert "CHANGED CONTENT" in doc["reason"] and "sha256" in doc["reason"]
    assert "guard-5541" in err and "does not apply" in err


def test_the_credential_tripwire_is_not_lifted_by_an_override(tmp_path):
    secret = _private_file(tmp_path)
    world = _world(tmp_path, {"test_green.py": GREEN_TEST}, hook=_clobbering_hook(secret))
    rc, doc, _ = _run(tmp_path, world, "--since", OLD, "--override", "g-999-02: pre-existing")
    assert rc == 1 and doc["decision"] == "block"
    assert not (world / "domain-suite-overrides.jsonl").exists()


def test_a_suite_that_only_reads_a_credential_file_passes(tmp_path):
    secret = _private_file(tmp_path)
    hook = f"#!/usr/bin/env bash\ncat {secret} > /dev/null\nexit 0\n"
    world = _world(tmp_path, {"test_green.py": GREEN_TEST}, hook=hook)
    rc, doc, _ = _run(tmp_path, world, "--since", OLD)
    assert rc == 0 and doc["decision"] == "pass"
    assert "clobbered" not in doc


def test_private_files_keys_on_name_not_mode_and_skips_stores(tmp_path):
    """Measured on the motivating deployment: every bland-named 0600 file under the
    roots was a peer-written store or doc (forged-skills.yaml, program.md), so mode is
    a false-block source under concurrent Bodies and names are the signal."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("domain_suite_gate_t2", GATE)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(CORE_SCRIPTS))
    spec.loader.exec_module(mod)
    (tmp_path / "forged-skills.yaml").write_text("x", encoding="utf-8")  # 0600, bland name: no
    (tmp_path / "forged-skills.yaml").chmod(0o600)
    (tmp_path / "api-token.json").write_text("x", encoding="utf-8")      # readable, named: yes
    (tmp_path / "api-token.json").chmod(0o644)
    (tmp_path / ".env.local").write_text("x", encoding="utf-8")          # named: yes
    (tmp_path / "server.pem").write_text("x", encoding="utf-8")          # named by suffix: yes
    (tmp_path / "token.lock").write_text("x", encoding="utf-8")          # lock: never
    (tmp_path / "credential-recheck-metrics.jsonl").write_text("x", encoding="utf-8")  # a store: never
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / ".env").write_text("x", encoding="utf-8")        # not DIRECTLY under a root
    got = mod.private_files([tmp_path])
    assert set(got) == {str(tmp_path / "api-token.json"), str(tmp_path / ".env.local"), str(tmp_path / "server.pem")}
    before = dict(got)
    (tmp_path / "api-token.json").write_text("xy", encoding="utf-8")
    assert mod.rewritten_private_files(before, mod.private_files([tmp_path])) == [str(tmp_path / "api-token.json")]


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


def test_the_refusal_never_invites_a_pre_existing_override(tmp_path):
    """Every blocking branch has ALREADY excluded pre-existing reds, so the
    refusal must not offer that as a rationale. It used to, and the fleet
    complied exactly as instructed: measured 2026-08-30 across 8 live workers,
    7 of 12 refusals were overridden and EVERY justification claimed the reds
    were "pre-existing" — one of them reading "all 47 NEW reds are pre-existing
    failures". The operators were honest; the prompt named the one condition
    that cannot hold at that line. This pins the text that replaced it.
    """
    world = _world(tmp_path, {"test_red.py": RED_TEST})
    _seed(tmp_path, world)
    (world / "scripts" / "tests" / "test_red2.py").write_text("def test_red2():\n    assert 0\n", encoding="utf-8")
    rc, doc, err = _run(tmp_path, world, "--since", OLD)
    assert rc == 1 and doc["decision"] == "block"
    assert "NOT pre-existing" in err
    assert "do not override by calling them that" in err
    # The retired instruction must not come back.
    assert "Only when the red is PRE-EXISTING" not in err
    # Controls: it must still say HOW to override, and name the causes that
    # ARE available — a refusal that only forbids teaches nothing.
    assert "--override-domain-suite" in err
    assert "concurrent unit" in err and "flaky" in err


def test_the_override_ledger_records_the_blocking_units_verbatim(tmp_path):
    """`reason` is free prose no reader can check. `blocking_units` is the set
    the block was actually about, untruncated (`why` cuts off at 6 with " ..."),
    so a later audit can test an override's claim against what was really red.
    """
    world = _world(tmp_path, {"test_red.py": RED_TEST})
    _seed(tmp_path, world)
    (world / "scripts" / "tests" / "test_red2.py").write_text("def test_red2():\n    assert 0\n", encoding="utf-8")
    rc, doc, _ = _run(tmp_path, world, "--since", OLD, "--override", "g-000-00: partner broke it")
    assert rc == 0 and doc["decision"] == "override"
    row = json.loads((world / "domain-suite-overrides.jsonl").read_text(encoding="utf-8").strip())
    assert row["blocking_units"] == ["tests/test_red2.py::test_red2"]
    assert row["baseline_recorded_at"], "the baseline this override was judged against"
    # Control: the genuinely pre-existing red is NOT in the blocking set, which
    # is what makes a "pre-existing" claim checkable rather than rhetorical.
    assert "tests/test_red.py::test_red" not in row["blocking_units"]


def test_a_collection_error_override_records_no_blocking_units(tmp_path):
    """A collection error identifies no unit, so the field is empty rather than
    absent or guessed. Empty is the honest answer AND the fingerprint of the one
    override the gate says is never legitimate."""
    world = _world(tmp_path, {"test_broken.py": BROKEN_IMPORT_TEST})
    rc, doc, _ = _run(tmp_path, world, "--since", OLD, "--override", "g-000-00: forced")
    assert rc == 0 and doc["decision"] == "override"
    row = json.loads((world / "domain-suite-overrides.jsonl").read_text(encoding="utf-8").strip())
    assert row["blocking_units"] == []
    assert row["baseline_recorded_at"] is None


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


# ─── the retained run log () ────────────────────────────────────
#
# The gate used to write its run to a mktemp file, read it once, and unlink it in
# a `finally` — so on a red run the ONLY surviving evidence was `tail`, the last
# 25 lines. The live domain runner emits one line per unit over ~97 units, so a
# failure partway through fell outside that window and was destroyed before
# anyone read the refusal. Measured 2026-09-09 on cc-04: one real red, at
# [17/93], whose failure text no longer existed. These two tests pin the fix and
# its control; the control is the load-bearing half, because retaining on EVERY
# run would trade a lost diagnostic for an unbounded log directory.

_MANY_LINE_HOOK = (
    "#!/usr/bin/env bash\n"
    "echo 'EARLY-MARKER-line-1'\n"
    "for i in $(seq 2 60); do echo \"[$i/60] PASS filler_$i.sh\"; done\n"
    "echo '[61/61] FAIL tests/test_late.sh (rc=1)'\n"
    "exit 1\n"
)


def test_a_red_run_retains_its_full_log_and_names_it(tmp_path):
    log_dir = tmp_path / "retained"
    world = _world(tmp_path, {"test_green.py": GREEN_TEST}, hook=_MANY_LINE_HOOK)
    _run(tmp_path, world, "--since", OLD, log_dir=log_dir)   # seeds the baseline
    (world / "scripts" / "run-domain-tests.sh").write_text(
        _MANY_LINE_HOOK.replace("test_late.sh", "test_new_red.sh"), encoding="utf-8")
    rc, doc, err = _run(tmp_path, world, "--since", OLD, log_dir=log_dir)
    assert rc == 1 and doc["decision"] == "block"

    retained = Path(doc["log"])
    assert retained.is_file(), f"the block named a log that does not exist: {doc['log']}"
    body = retained.read_text(encoding="utf-8")

    # The discriminator: the retained log holds a line the tail CANNOT hold.
    # Asserting only that the file exists would pass against a file containing
    # nothing but the same 25 lines, which is the defect wearing a new name.
    assert "EARLY-MARKER-line-1" in body
    assert not any("EARLY-MARKER-line-1" in ln for ln in doc["tail"]), \
        "fixture too small — the marker must fall outside the tail for this to discriminate"
    assert "[61/61] FAIL tests/test_new_red.sh" in body
    assert f"FULL RUN LOG (retained): {retained}" in err


def test_a_green_run_retains_no_log(tmp_path):
    # The control. Retention is for runs a human must diagnose; a clean run has
    # nothing to diagnose and must leave the directory empty.
    log_dir = tmp_path / "retained"
    world = _world(tmp_path, {"test_green.py": GREEN_TEST})
    rc, doc, _ = _run(tmp_path, world, "--since", OLD, log_dir=log_dir)
    assert rc == 0 and doc["decision"] == "pass"
    assert doc.get("log") is None
    assert not log_dir.exists() or not list(log_dir.iterdir())


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


# --- the baseline's per-box scope is ENFORCED, not merely claimed () ---
#
# The gate's scope comment said the baseline is per box because the write is "a
# plain write to the world mirror; on a synced world it stays local". The write
# IS plain, but the sync WALK picks the file up afterwards, so on an own-cloud
# deployment two boxes rewrote one shared S3 object. merge_handler_for returns
# None for it (governed-store-write-classes class (b), fence-only), so the
# resulting fence never self-resolves: measured both-diverged for 123 consecutive
# mirror sweeps on cc-07 and 417 on cc-02. Because the consumer is a ratchet
# GATE, a foreign box's baseline mis-gates closes in BOTH directions -- admitting
# a regression the other box already carried, or blocking on a red never seen here.
#
# These pin the cure, which lives in a DIFFERENT file
# (owncloud_sync._EXCLUDE_NAMES) from the gate that depends on it -- exactly the
# split that let the premise rot unnoticed.

def _both_legs_machine_local(name: str) -> bool:
    """The full predicate, BOTH legs. Calling _is_machine_local alone is the
    documented trap (guard-2471): it deliberately does not test _EXCLUDE_DIRS,
    and one leg returns a confidently wrong answer rather than an error."""
    sys.path.insert(0, str(CORE_SCRIPTS))
    import owncloud_sync as ocs
    root = Path("/nonexistent-world")
    target = root / name
    rel = target.relative_to(root)
    leg_dirs = any(seg in ocs._EXCLUDE_DIRS for seg in rel.parts[:-1])
    leg_name = ocs._is_machine_local(target.name, "world",
                                     full_path=target, root_path=root)
    return leg_dirs or leg_name


def test_baseline_is_machine_local():
    assert _both_legs_machine_local("domain-suite-baseline.json"), (
        "domain-suite-baseline.json must be machine-local, or two boxes rewrite "
        "one shared S3 object and the class-(b) fence wedges permanently")


@pytest.mark.parametrize("shared", ["reasoning-bank.jsonl", "guardrails.jsonl",
                                    "aspirations.jsonl", "pipeline.jsonl"])
def test_shared_stores_still_sync(shared):
    """guard-3018 control, and the load-bearing half: adding a SHARED,
    authoritative-in-S3 file to the machine-local sets means one box's writes
    never reach S3 and every peer diverges permanently, with nothing erroring.
    A test asserting only the target would pass just as well if the whole set
    had been widened."""
    assert not _both_legs_machine_local(shared), (
        f"{shared} became machine-local -- that is silent cross-box data loss, "
        f"not a scope fix")


def test_baseline_pull_cannot_clobber_the_only_good_copy():
    """A machine-local file is never pushed, so a force-pull before an in-lock
    read would overwrite the only good (local) copy with whatever stale object
    the remote holds (guard-881). refresh_would_clobber must gate it now, and
    must NOT gate a genuinely synced store."""
    sys.path.insert(0, str(CORE_SCRIPTS))
    import owncloud_sync as ocs
    root = Path("/nonexistent-world")

    class _Be:
        _roots = [(root, "world")]

    assert ocs.refresh_would_clobber(_Be(), root / "domain-suite-baseline.json") is True
    assert ocs.refresh_would_clobber(_Be(), root / "guardrails.jsonl") is False


def test_scope_comment_no_longer_asserts_the_falsified_premise():
    """The prose is what routed every prior reader away from the defect, so pin
    it: a comment claiming a plain write "stays local" on a synced world is the
    exact false premise this fix removed (guard-4526 -- correct the artifacts
    written under the old premise in the same change)."""
    src = (CORE_SCRIPTS / "domain-suite-gate.py").read_text(encoding="utf-8")
    # The phrase may survive ONCE, quoted as the premise that was falsified --
    # that history is the point (guard-4526). What must never return is the
    # phrase stated as CURRENT fact, i.e. a second occurrence or a surviving
    # occurrence with no correction beside it.
    stale = "on a synced world it stays local"
    assert src.count(stale) <= 1, (
        "the falsified scope premise is asserted again in domain-suite-gate.py")
    if stale in src:
        assert "was FALSE" in src, (
            "the old premise is quoted but nothing marks it false -- a reader "
            "will take the quote for the current rule")
    assert "_EXCLUDE_NAMES" in src, (
        "the comment must name the mechanism that actually enforces per-box "
        "scope, so the next reader can find it")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


# ─── : touch vs clobber ─────────────────────────────────────────
#
# The gate hard-refused every world/scripts close on a (size, mtime_ns) window diff
# that cannot tell a destructive rewrite from a bare touch, cannot tell the suite
# from a concurrent writer, and offered no override. Two investigations (cc-04, 86
# tests; cc-08, 82 tests + a full canonical runner pass under a 1s watcher) found no
# domain test writing the live credential file, both times with contents byte-
# identical. These pin the split that makes the refusal falsifiable.

def _gate_mod(name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, GATE)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(CORE_SCRIPTS))
    spec.loader.exec_module(mod)
    return mod


def test_classify_content_change_blocks(tmp_path):
    """POSITIVE CONTROL (guard-2421): the protection that matters must still fire.
    A real content change is content_changed — the only class that hard-refuses."""
    mod = _gate_mod("dsg_cls_1")
    f = tmp_path / ".env.local"
    f.write_text("KEY=aaa", encoding="utf-8")
    before = mod.private_files([tmp_path])
    f.write_text("KEY=bbb", encoding="utf-8")
    got = mod.classify_private_changes(before, mod.private_files([tmp_path]))
    assert got["content_changed"] == [str(f)]
    assert got["touched"] == [] and got["unverifiable"] == []


def test_classify_touch_does_not_block(tmp_path):
    """A bare touch — mtime moves, sha256 identical — is `touched`, never
    content_changed. This is the measured case that produced the wedge."""
    mod = _gate_mod("dsg_cls_2")
    f = tmp_path / ".env.local"
    f.write_text("KEY=aaa", encoding="utf-8")
    before = mod.private_files([tmp_path])
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns + 10**9, st.st_mtime_ns + 10**9))
    after = mod.private_files([tmp_path])
    assert after[str(f)][:2] != before[str(f)][:2], "mtime must actually move"
    got = mod.classify_private_changes(before, after)
    assert got["touched"] == [str(f)]
    assert got["content_changed"] == []


def test_classify_identical_rewrite_is_a_touch(tmp_path):
    """A byte-identical round-trip rewrite (a provisioner writing the same values
    back) must not block — both observed incidents were exactly this shape."""
    mod = _gate_mod("dsg_cls_3")
    f = tmp_path / "api-token.json"
    f.write_text("KEY=same", encoding="utf-8")
    before = mod.private_files([tmp_path])
    st = f.stat()
    f.write_text("KEY=same", encoding="utf-8")          # same bytes, new mtime
    os.utime(f, ns=(st.st_atime_ns + 10**9, st.st_mtime_ns + 10**9))
    got = mod.classify_private_changes(before, mod.private_files([tmp_path]))
    assert got["content_changed"] == []
    assert got["touched"] == [str(f)]


def test_classify_vanished_is_content_changed(tmp_path):
    """A credential file that disappears during the window is unambiguous."""
    mod = _gate_mod("dsg_cls_4")
    f = tmp_path / ".env.local"
    f.write_text("KEY=aaa", encoding="utf-8")
    before = mod.private_files([tmp_path])
    f.unlink()
    got = mod.classify_private_changes(before, mod.private_files([tmp_path]))
    assert got["content_changed"] == [str(f)]


def test_classify_unhashable_is_unverifiable_not_clean(tmp_path):
    """When the digest cannot be established the answer is UNKNOWN, and it must not
    silently fold into `touched` — an unreadable file is exactly where a confident
    'unchanged' would be wrong."""
    mod = _gate_mod("dsg_cls_5")
    mod.PRIVATE_HASH_MAX_BYTES = 0          # every non-empty file is past the ceiling
    f = tmp_path / ".env.local"
    f.write_text("KEY=aaa", encoding="utf-8")
    before = mod.private_files([tmp_path])
    assert before[str(f)][2] is None, "precondition: digest declined"
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns + 10**9, st.st_mtime_ns + 10**9))
    got = mod.classify_private_changes(before, mod.private_files([tmp_path]))
    assert got["unverifiable"] == [str(f)]
    assert got["content_changed"] == [] and got["touched"] == []


def test_classify_unmoved_file_is_in_no_class(tmp_path):
    """NEGATIVE CONTROL: a file nobody touched appears in none of the three lists."""
    mod = _gate_mod("dsg_cls_6")
    f = tmp_path / ".env.local"
    f.write_text("KEY=aaa", encoding="utf-8")
    before = mod.private_files([tmp_path])
    got = mod.classify_private_changes(before, mod.private_files([tmp_path]))
    assert got == {"content_changed": [], "touched": [], "unverifiable": []}


def test_digest_is_never_emitted_in_a_message(tmp_path):
    """guard-724 / guard-1563: the digest is compared, never surfaced. The classifier
    returns PATHS only — no digest, no file content, reaches a caller."""
    mod = _gate_mod("dsg_cls_7")
    f = tmp_path / ".env.local"
    f.write_text("SECRET=hunter2", encoding="utf-8")
    before = mod.private_files([tmp_path])
    f.write_text("SECRET=changed", encoding="utf-8")
    got = mod.classify_private_changes(before, mod.private_files([tmp_path]))
    flat = repr(got)
    assert "hunter2" not in flat and "changed" not in flat.replace("content_changed", "")
    for bucket in got.values():
        assert all(isinstance(p, str) for p in bucket)


def test_a_suite_that_only_touches_a_credential_file_no_longer_blocks(tmp_path):
    """THE WEDGE, end to end (). A hook that rewrites the credential file
    with IDENTICAL bytes moves (size, mtime_ns) exactly as a clobber would. Under the
    old shape-only predicate this hard-refused the close with no override and no
    reproducible cause — the state that blocked every world/scripts close. Contents
    are provably intact, so it must warn and PASS."""
    secret = _private_file(tmp_path)
    identical = "#!/usr/bin/env bash\nprintf 'TOKEN=real\\n' > %s\nexit 0\n" % secret
    world = _world(tmp_path, {"test_green.py": GREEN_TEST}, hook=identical)
    rc, doc, err = _run(tmp_path, world, "--since", OLD)
    assert rc == 0, "a byte-identical rewrite must not refuse the close"
    assert doc["decision"] == "pass"
    assert "clobbered" not in doc
    assert "TOUCHED" in err and "sha256 UNCHANGED" in err
    assert secret.read_text(encoding="utf-8") == "TOKEN=real\n"
