"""Tests for core/scripts/context-diet-report.py ().

THE POINT OF THIS FILE IS THE POLARITY CONTROL. Every sibling ratchet in this
tree assumes lower-is-better, and g-115-6472's second verification outcome
requires `regressed` on preamble bytes GROWING *and* on iterations-per-compaction
FALLING — opposite directions through one code path. A ratchet that silently
ignored the direction argument would still seed, still record history, still look
healthy, and would bless every throughput regression it exists to catch. So
`test_polarities_disagree_on_identical_movement` is the load-bearing case: it
feeds the SAME 100->200 movement to both polarities and pins that the verdicts
differ. Every case here was verified RED by mutation before being committed
(guard-3534).

Two test shapes, deliberately:
  - `decide()` is PURE, so polarity is branch-tested with no I/O at all.
  - the end-to-end case runs the script as a SUBPROCESS with MIND_WORLD/
    MIND_META pointed at tmp dirs and STORAGE_BACKEND=local — the same shape
    test_temp_citation_ratchet.py uses, and mandatory on an own-cloud box
    (guard-955: a tmp write inheriting own-cloud collides on the PRODUCTION S3
    key, because the key derives from customer_prefix+env_id+filename and NOT
    from the MIND_WORLD override).
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

_SCRIPTS = Path(__file__).resolve().parent.parent
SCRIPT = _SCRIPTS / "context-diet-report.py"
sys.path.insert(0, str(_SCRIPTS))


def _load():
    # The module name carries hyphens, so a plain `import` cannot reach it.
    spec = importlib.util.spec_from_file_location("cdr", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cdr():
    return _load()


# --------------------------------------------------------------------------
# Polarity A — preamble bytes: lower is better, regresses by GROWING.
# --------------------------------------------------------------------------

def test_preamble_seeds_on_first_sight(cdr):
    assert cdr.decide(None, 358849, True)[0] == "seeded"


def test_preamble_regresses_on_growth(cdr):
    assert cdr.decide(358849, 400000, True)[0] == "regressed"


def test_preamble_regression_never_moves_baseline_the_wrong_way(cdr):
    # The whole value of a ratchet: a regression must not become the new normal.
    assert cdr.decide(358849, 400000, True)[1] == 358849


def test_preamble_ratchets_down_on_shrink(cdr):
    verdict, baseline, _ = cdr.decide(358849, 300000, True)
    assert (verdict, baseline) == ("ratcheted", 300000)


def test_preamble_stable_when_unchanged(cdr):
    assert cdr.decide(300000, 300000, True)[0] == "stable"


# --------------------------------------------------------------------------
# Polarity B — closes per compaction: higher is better, regresses by FALLING.
# --------------------------------------------------------------------------

def test_throughput_regresses_on_fall(cdr):
    assert cdr.decide(0.88, 0.50, False)[0] == "regressed"


def test_throughput_regression_never_moves_baseline_the_wrong_way(cdr):
    assert cdr.decide(0.88, 0.50, False)[1] == 0.88


def test_throughput_ratchets_up_on_rise(cdr):
    verdict, baseline, _ = cdr.decide(0.88, 1.60, False)
    assert (verdict, baseline) == ("ratcheted", 1.60)


# --------------------------------------------------------------------------
# THE CONTROL. Without this, a single-direction ratchet passes every case above
# that it happens to be pointed at, and silently blesses the other half.
# --------------------------------------------------------------------------

def test_polarities_disagree_on_identical_movement(cdr):
    lower = cdr.decide(100, 200, True)[0]
    higher = cdr.decide(100, 200, False)[0]
    assert lower == "regressed"
    assert higher == "ratcheted"
    assert lower != higher, (
        "the same movement must not read the same to both polarities; "
        "if these agree, the polarity argument is being ignored")


# --------------------------------------------------------------------------
# Guards.
# --------------------------------------------------------------------------

def test_unavailable_metric_is_skipped_not_zero(cdr):
    """An unmeasurable metric must NOT ratchet as 0.

    A worker with no readable transcript has no closes-per-compaction. Seeding
    that as 0 would set a floor nothing can ever regress below, permanently
    disarming the detector (rb-245: an absence of measurement is not a
    measurement of zero).
    """
    r = cdr.ratchet("k", None, False, "T0", {})
    assert r["verdict"] == "skipped"
    assert r["baseline"] is None


def test_ledger_basename_is_merge_protected():
    """The ledger must be class (a), not fence-only.

    context-diet-report.py runs on every box for every agent, so cross-box
    concurrent appends are the normal case here, not the rare one. A class-(b)
    store has no reconciler below the write: a both-diverged freeze drops real
    rows in EITHER direction — the measured fate of the sibling
    complexity-ledger.jsonl before it was enrolled (14 rows per side, 7 shared,
    7 unique to each, so no direction-pick could avoid losing 7).
    """
    from coordination_merge import merge_handler_for
    assert merge_handler_for(Path("meta") / "context-diet-ledger.jsonl") is not None


def test_dynamic_absence_is_not_reported_as_zero(cdr):
    """A missing transcript must report unavailable, never zeroes.

    guard-1760/guard-1641: a lane that did not run and a lane that found nothing
    print the same silence unless you make them differ.
    """
    d = cdr.collect_dynamic(None)
    assert d["available"] is False
    assert "reason" in d
    assert "compactions" not in d


# --------------------------------------------------------------------------
# Static collection.
# --------------------------------------------------------------------------

def test_static_preamble_excludes_path_scoped_rules(cdr, tmp_path):
    """A rule carrying `paths:` front matter loads conditionally, so it is not
    part of the FIXED per-turn preamble and must not be counted as such."""
    root = tmp_path / "repo"
    (root / ".claude" / "rules").mkdir(parents=True)
    (root / ".claude" / "skills").mkdir(parents=True)
    (root / "CLAUDE.md").write_text("x" * 100)
    (root / ".claude" / "rules" / "always.md").write_text("y" * 50)
    (root / ".claude" / "rules" / "scoped.md").write_text(
        "---\npaths:\n  - 'src/**'\n---\nbody")

    st = cdr.collect_static(root)
    assert st["rules_files"] == 2
    assert st["rules_path_scoped_files"] == 1
    assert st["preamble_bytes"] == 100 + 50   # CLAUDE.md + the unscoped rule only


def test_readiness_judges_on_the_measured_ratio_not_the_optimistic_one(cdr):
    """The verdict must use the MEASURED dense ratio.

    The two ratios straddle the answer for the live corpus (~143.5k tokens dense
    vs ~89.7k prose against a 125k window), so which one the verdict reads is the
    whole result. `.claude/rules/self.md` labels ~4 B/tok an unverified estimate
    and records 2.48/2.51 measured; a third measurement (2.57) is cited in the
    script. Judging on the optimistic figure would be re-baselining a detector to
    make it green — the brief's explicit prohibition.
    """
    # Sized to fit under the prose ratio but not the dense one.
    bytes_ = int(cdr.LOCAL_WINDOW_TOKENS * 3.0)
    rd = cdr.readiness({"preamble_bytes": bytes_})
    assert rd["preamble_tokens_prose_est"] < cdr.LOCAL_WINDOW_TOKENS
    assert rd["preamble_tokens_dense_est"] > cdr.LOCAL_WINDOW_TOKENS
    assert rd["fits_125k"] is False, "verdict must follow the measured ratio"


# --------------------------------------------------------------------------
# End-to-end, subprocess, isolated world/meta.
# --------------------------------------------------------------------------

def _run(tmp_path, extra=()):
    """Run the script in an isolated world/meta.

    Takes pytest's `tmp_path` rather than opening a TemporaryDirectory here: a
    context manager whose `return` sits inside the `with` deletes the tree before
    the caller can assert on it, which reads as "the script wrote nothing" when
    the script wrote correctly. Caught that way once while building this file.
    """
    world, meta = tmp_path / "world", tmp_path / "meta"
    world.mkdir(exist_ok=True)
    meta.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["MIND_WORLD"] = str(world)
    env["MIND_META"] = str(meta)
    env["STORAGE_BACKEND"] = "local"      # guard-955 — mandatory, not optional
    env.pop("VERIFY_LEARNING_DRIFT_HARD_GATE", None)
    r = subprocess.run([sys.executable, str(SCRIPT), "--json", *extra],
                       capture_output=True, text=True, encoding="utf-8", env=env)
    assert r.returncode == 0, f"exited {r.returncode}: {r.stderr}"
    return json.loads(r.stdout), meta


def test_end_to_end_emits_all_three_sections_and_persists(tmp_path):
    """Outcome 1: static + dynamic + readiness printed, ledger row appended,
    audit-baselines ratchet row present."""
    report, meta = _run(tmp_path)

    for section in ("static", "dynamic", "readiness", "ratchets"):
        assert section in report, f"missing {section} section"
    assert report["static"]["preamble_bytes"] > 0
    assert report["readiness"]["local_window_tokens"] == 125_000

    ledger = meta / "context-diet-ledger.jsonl"
    assert ledger.exists(), "ledger row not appended"
    rows = [json.loads(x) for x in ledger.read_text().splitlines() if x.strip()]
    assert len(rows) == 1
    assert rows[0]["preamble_bytes"] == report["static"]["preamble_bytes"]

    baselines = yaml.safe_load((meta / "audit-baselines.yaml").read_text())
    assert "context_diet_preamble_bytes" in baselines, "ratchet row not written"
    row = baselines["context_diet_preamble_bytes"]
    assert row["last_verdict"] == "seeded"
    assert row["polarity"] == "lower_is_better"


def test_no_ledger_and_no_ratchet_flags_write_nothing(tmp_path):
    """The report must be runnable read-only — a diagnostic that mutates state
    every time it is consulted cannot be used to investigate a suspected
    regression without perturbing the thing being measured."""
    _, meta = _run(tmp_path, extra=("--no-ledger", "--no-ratchet"))
    assert not (meta / "context-diet-ledger.jsonl").exists()
    assert not (meta / "audit-baselines.yaml").exists()


# --------------------------------------------------------------------------
# Role detection — mode FIRST, then body artefacts (2026-08-17 follow-up).
# An assistant/reader session never runs the loop, so its close count is
# structurally zero; measured on cc-09, an assistant session that had served
# as a hand-driven worker Body still carried a body-manifest and was ratcheted
# against the worker baseline (0.0 vs 1 -> REGRESSED). Mode must win.
# --------------------------------------------------------------------------

def _role_env(cdr, tmp_path, monkeypatch, sid="sid-1"):
    state = tmp_path / "session"
    sess = tmp_path / "sessions" / sid
    state.mkdir(parents=True)
    sess.mkdir(parents=True)
    monkeypatch.setattr(cdr, "agent_state_dir", lambda name: state)
    monkeypatch.setattr(cdr, "agent_session_dir", lambda name, s: sess)
    monkeypatch.setenv("MIND_SID", sid)
    return state, sess


def test_detect_role_assistant_mode_wins_over_body_artefacts(cdr, tmp_path, monkeypatch):
    state, sess = _role_env(cdr, tmp_path, monkeypatch)
    (sess / "body-manifest.yaml").write_text("body: worker\n")
    (sess / "working-memory.yaml").write_text("{}\n")
    (state / "running-session-id").write_text("sid-1\n")
    (state / "agent-mode").write_text("assistant\n")
    assert cdr.detect_role("alpha") == "assistant"
    (state / "agent-mode").write_text("reader\n")
    assert cdr.detect_role("alpha") == "assistant"


def test_detect_role_worker_and_reducer_when_mode_is_autonomous(cdr, tmp_path, monkeypatch):
    state, sess = _role_env(cdr, tmp_path, monkeypatch)
    (state / "agent-mode").write_text("autonomous\n")
    (sess / "body-manifest.yaml").write_text("body: worker\n")
    assert cdr.detect_role("alpha") == "worker"
    (sess / "body-manifest.yaml").unlink()
    (state / "running-session-id").write_text("sid-1\n")
    assert cdr.detect_role("alpha") == "reducer"
    (state / "running-session-id").write_text("other-sid\n")
    assert cdr.detect_role("alpha") == "unknown"


def test_detect_role_uses_paths_helpers_not_literal_agents_join():
    """CLAUDE.md "Agent-dir Resolution": never join PROJECT_ROOT / "agents" by hand."""
    src = SCRIPT.read_text(encoding="utf-8")
    body = src.split("def detect_role(", 1)[1].split("\ndef ", 1)[0]
    assert '"agents"' not in body and "'agents'" not in body
    assert "agent_state_dir(" in body and "agent_session_dir(" in body
