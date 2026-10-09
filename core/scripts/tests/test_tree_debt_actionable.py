""" — the tree-debt aggregate is ACTIONABLE debt, not the raw
candidate count.

95.8% of the live tree's distill candidates are bare `low_utility` — a
trigger the framework's own distill gate documents as over-flagging
(rb-94 / guard-896 / g-115-1534: low retrieval signals under-instrumentation
or niche value, NOT bloat; the prescribed per-node outcome is
maintain_exempt, not distill). Counting them in the debt aggregate kept the
auto-backlog predicate (debt > debt_threshold*3 = 120) armed PERMANENTLY:
758 reported vs 12 genuinely over the Read cap.

The fix narrows the DEBT AGGREGATE ONLY, in three code-derived surfaces:
  1. `actionable_distill_count` in core/scripts/tree.py — the single
     predicate; bare `low_utility` is the only trigger it excludes.
  2. `tree-read.sh --debt` — the number the prose predicate sites consume
     (aspirations-loop-digest.md Phase 8.7/8.8, aspirations-consolidate
     Step 6, /tree SKILL.md backlog trigger) instead of hand-summing
     candidate lists.
  3. `--record-maintenance` post_run_debt (CLI + daemon mirror) — `total`/
     `cleared` are actionable debt; the raw population stays visible in
     `distill` (legacy raw key) + `distill_low_utility`.

What this file pins:
  A. the helper's predicate (excludes bare low_utility ONLY — read-cap arms
     and large_mediocre are genuinely actionable and must keep counting;
     the input is never filtered or mutated).
  B. the --debt output shape and VALUES against a seeded fixture, with the
     raw population visible and unchanged (CLI `read --debt` and the
     `tree-read.sh` wrapper).
  C. the record-maintenance debt math + the post_run_debt KEY ORDER (the
     byte-compat contract with the daemon mirror in
     mind_api/src/world/tree_write.py).
  D. tree-maintenance-read.py rendering of the narrowed aggregate (incl.
     the em-dash default for pre-g-115-5421 records, and the aggregate
     trend line).
  E. daemon-mirror VALUE parity on the same fixture world (the byte-compat
     key-order test in mind_api/tests/test_runtime_tree_write.py keeps the
     dump format honest; this test catches a value math that is ORDER-
     correct but counts wrong).

The fixture is built against the REAL core/config/tree.yaml thresholds
(min_retrievals=5, utility<0.3, min_votes=3, recency=45d, line>50 +
ur<0.5, token cap 25000 @ 0.8 via CHARS_PER_TOKEN=2.3, decompose leaf cap
K_max^(D-1)=64000) with generous margins, mirroring
test_distill_candidate_filters.py's pattern. The trigger label is the FIRST
firing criterion (crit1 before crit2 before crit3), so a large_mediocre node
must keep ur in [0.3, 0.5) — below 0.3 it fires crit1 and lands as
low_utility even with 500 lines (measured 2026-10-06: ur=0.1/500-line body
-> trigger low_utility). Crit3's token_trigger is 20,000 ESTIMATED TOKENS,
not chars: 20,000 * CHARS_PER_TOKEN (2.3) = 46,000 chars minimum. Expected
fixture classification:
  low1, low2  -> low_utility                 (crit1: 3 noise votes, fresh)
  big         -> large_mediocre              (crit2: 500 lines, ur=0.35, 20 votes)
  huge        -> oversized_append_grown      (crit3: 47,360 chars = 20,591 est
                                              tokens, 3 dated refresh sections)
  clean       -> not a candidate             (rc=2 < min_ret, ur=0.9 fresh)
  root        -> not a candidate             (rc=2 < min_ret; decompose is
                                              leaf-count structural, 5 leaves << cap)
=> distill raw 4, actionable 2, low_utility 2, decompose 0, total 2.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

import pytest

from _bash_helpers import BASH  # noqa: E402  (guard-580: never a bare "bash" argv[0])

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
REPO_ROOT = SCRIPT_DIR.parents[2]
TREE_PY = CORE_SCRIPTS / "tree.py"
TREE_READ_SH = CORE_SCRIPTS / "tree-read.sh"

_HAS_LIBYAML = bool(yaml) and hasattr(yaml, "CSafeDumper")

# ── tree.py import (spec loader; core/scripts must be importable first —
#    tree.py imports the sibling `_stdio`, ) ────────────────────
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))
_spec = importlib.util.spec_from_file_location("tree_engine_g5421", TREE_PY)
tree_engine = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tree_engine)

# ── tree-maintenance-read.py import (renderer under test D) ─────────────
_spec_r = importlib.util.spec_from_file_location(
    "tmr_g5421", CORE_SCRIPTS / "tree-maintenance-read.py")
tmr = importlib.util.module_from_spec(_spec_r)
_spec_r.loader.exec_module(tmr)

FRESH = date.today().isoformat()
BIG_BODY = "# Big mediocre node\n" + "content line\n" * 499        # 500 lines
# Crit3 fires at est_tokens >= 0.8 * 25000 = 20,000, and est_tokens =
# int(chars / CHARS_PER_TOKEN=2.3) — so the body needs >= 46,000 CHARS
# (20,000 * 2.3). "padding\n" is 8 chars/line -> 5900 lines = 47,200 chars
# + the 160-char header/refresh block = 47,360 chars -> 20,591 est tokens,
# 591 over the trigger (the 3,360-char first cut measured 1,460 est tokens
# and tripped NO criterion — the trigger is est tokens, not chars).
HUGE_BODY = "# Huge append-grown sweep node\n" + (
    "## Refresh 2026-08-01: values re-measured\n\n"
    "## Refresh 2026-08-05: values re-measured\n\n"
    "## Refresh 2026-08-09: values re-measured\n\n"
) + "padding\n" * 5900                                            # 47,360 chars


def _leaf_yaml(key, rc, th, tn, ur, last, file_):
    f = "    file: {}\n".format(file_) if file_ else "    file: null\n"
    lr = "    last_retrieved: {}\n".format(last) if last else ""
    return (
        "  {}:\n"
        "{}"
        "    summary: {}\n"
        "    depth: 1\n"
        "    parent: root\n"
        "    children: []\n"
        "    child_count: 0\n"
        "    retrieval_count: {}\n"
        "    times_helpful: {}\n"
        "    times_noise: {}\n"
        "    utility_ratio: {}\n"
        "{}"
    ).format(key, f, key, rc, th, tn, ur, lr)


def _fixture_tree_yaml():
    return (
        "nodes:\n"
        "  root:\n"
        "    file: null\n"
        "    summary: Root node\n"
        "    depth: 0\n"
        "    children: []\n"
        "    child_count: 0\n"
        "    retrieval_count: 2\n"
        "    times_helpful: 2\n"
        "    times_noise: 0\n"
        "    utility_ratio: 0.9\n"
        "    last_retrieved: {}\n"
        "{}"
        "{}"
        "{}"
        "{}"
        "{}"
        "last_updated: '2026-01-01'\n"
        "entity_index: {{}}\n"
    ).format(
        FRESH,
        _leaf_yaml("low1", 12, 0, 3, 0.0, FRESH, None),
        _leaf_yaml("low2", 8, 0, 3, 0.05, FRESH, None),
        _leaf_yaml("big", 20, 7, 13, 0.35, FRESH, "world/knowledge/tree/big.md"),
        _leaf_yaml("huge", 50, 40, 10, 0.8, FRESH, "world/knowledge/tree/huge.md"),
        _leaf_yaml("clean", 2, 2, 0, 0.9, FRESH, None),
    )


def _seed_world(base: Path, name: str) -> Path:
    world = base / name
    (world / "knowledge" / "tree").mkdir(parents=True)
    (world / "knowledge" / "tree" / "_tree.yaml").write_text(
        _fixture_tree_yaml(), encoding="utf-8")
    (world / "knowledge" / "tree" / "big.md").write_text(BIG_BODY, encoding="utf-8")
    (world / "knowledge" / "tree" / "huge.md").write_text(HUGE_BODY, encoding="utf-8")
    return world


def _run_cli(world: Path, args: list, stdin_text: str | None = None) -> subprocess.CompletedProcess:
    """Drive tree.py directly (same env idiom as the daemon byte-compat
    tests in mind_api/tests/test_runtime_tree_write.py: MIND_WORLD override,
    sys.executable, cwd=repo root)."""
    env = dict(os.environ)
    env["MIND_WORLD"] = str(world)
    env["MIND_META"] = str(world / "meta")
    env["MIND_AGENT"] = "alpha"
    (world / "meta").mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [sys.executable, str(TREE_PY), *args],
        input=stdin_text, text=True, env=env, cwd=str(REPO_ROOT),
        capture_output=True, timeout=60,
    )


# ── A. the helper's predicate ────────────────────────────────────────────

class TestActionableDistillCount:

    @staticmethod
    def _cand(trigger):
        return {"key": "k-" + trigger, "trigger": trigger}

    def test_excludes_only_bare_low_utility(self):
        cands = [self._cand(t) for t in
                 ("low_utility", "large_mediocre", "oversized_append_grown",
                  "oversized_not_append_grown", "low_utility")]
        assert tree_engine.actionable_distill_count(cands) == 3, (
            "the exclusion set is exactly {low_utility}: the read-cap arms "
            "and large_mediocre are genuinely actionable and must keep "
            "counting (widening the set makes the number go down without "
            "the per-node coherence judgment the gate requires)")

    def test_accepts_include_skipped_dict_shape(self):
        cands = [self._cand("low_utility"), self._cand("large_mediocre")]
        wrapped = {"candidates": cands,
                   "skipped": [{"node_key": "s", "skip_reason": "no_feedback"}]}
        assert tree_engine.actionable_distill_count(wrapped) == 1

    def test_never_filters_or_mutates_the_input(self):
        cands = [self._cand("low_utility"), self._cand("large_mediocre")]
        snapshot = list(cands)
        tree_engine.actionable_distill_count(cands)
        assert cands == snapshot, (
            "--distill-candidates callers keep seeing the FULL population; "
            "the narrowing is an aggregate, not a filter")

    def test_empty_and_missing_trigger_counts_as_actionable(self):
        # Empty list: nothing counts. A candidate with NO trigger field is
        # NOT in the exclusion set, so it COUNTS — the failure direction is
        # fail-toward-arming (an unknown trigger must not silently disappear
        # from the debt figure; that is exactly how the over-flag class hid).
        assert tree_engine.actionable_distill_count([]) == 0
        assert tree_engine.actionable_distill_count([{"key": "x"}]) == 1


# ── B. the --debt read surface (CLI + wrapper) against the fixture ──────

class TestReadDebtSurface:

    def test_read_debt_output(self, tmp_path):
        world = _seed_world(tmp_path, "debt")
        proc = _run_cli(world, ["read", "--debt"])
        assert proc.returncode == 0, (
            "tree.py read --debt failed:\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
        out = json.loads(proc.stdout)
        assert out == {
            "total": 2,
            "decompose": 0,
            "distill_actionable": 2,
            "raw": {"distill": 4, "distill_low_utility": 2},
            "threshold": 40,
            "backlog_trigger": 120,
            "backlog_armed": False,
            "cleared": True,
        }, (
            "fixture must classify low1/low2 as low_utility, big as "
            "large_mediocre, huge as oversized_append_grown, clean/root as "
            "not candidates. If this diff shows a different RAW distill "
            "count, the fixture drifted against the live tree.yaml thresholds "
            "(or the candidate gates changed) — re-derive the fixture "
            "intentionally before touching the aggregate.")
        # The pre-fix hand-sum of the same lists (4 + 0) sits under the 120
        # trigger by accident of fixture size; what matters here is the
        # arithmetic identity, pinned explicitly:
        assert out["total"] == out["decompose"] + out["distill_actionable"]
        assert out["raw"]["distill"] == out["distill_actionable"] + out["raw"]["distill_low_utility"]

    def test_read_debt_via_tree_read_sh(self, tmp_path):
        """tree-read.sh routes --debt to the direct-python path like the
        other *-candidates flags (g-115-5421 wiring)."""
        world = _seed_world(tmp_path, "debt-sh")
        env = dict(os.environ)
        env["MIND_WORLD"] = str(world)
        env["MIND_META"] = str(world / "meta")
        env["MIND_AGENT"] = "alpha"
        env["STORAGE_BACKEND"] = "local"
        (world / "meta").mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [BASH, TREE_READ_SH.as_posix(), "--debt"],
            env=env, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, (
            "tree-read.sh --debt failed:\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
        out = json.loads(proc.stdout)
        assert out["total"] == 2 and out["raw"]["distill"] == 4 and out["cleared"] is True


# ── C. the record-maintenance debt math + key order ─────────────────────

class TestRecordMaintenanceDebt:

    def test_debt_math_and_key_order(self, tmp_path):
        world = _seed_world(tmp_path, "rm")
        proc = _run_cli(world, ["update", "--record-maintenance"])
        assert proc.returncode == 0, (
            "tree.py update --record-maintenance failed:\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
        result = json.loads(proc.stdout)
        debt = result["post_run_debt"]
        # Byte-compat contract: the daemon mirror dumps this dict with
        # sort_keys=False, so INSERTION ORDER is part of the contract —
        # ordered equality, not set equality.
        assert list(debt.keys()) == ["distill", "distill_actionable",
                                     "distill_low_utility", "decompose",
                                     "total", "threshold", "cleared"]
        assert debt == {
            "distill": 4,                 # raw population (legacy key) — visible
            "distill_actionable": 2,
            "distill_low_utility": 2,
            "decompose": 0,
            "total": 2,                   # ACTIONABLE — the gate number
            "threshold": 40,
            "cleared": True,              # 2 <= 40 -> last_backlog_clear_at set
        }
        assert "last_backlog_clear_at" in result["maintenance"], (
            "actionable debt 2 <= threshold 40 must land last_backlog_clear_at")
        assert yaml is not None
        tree_on_disk = yaml.safe_load(
            (world / "knowledge" / "tree" / "_tree.yaml").read_text(encoding="utf-8"))
        assert "last_backlog_clear_at" in tree_on_disk["maintenance"]

    def test_with_run_record_carries_narrowed_debt(self, tmp_path):
        """The run-record JSONL append embeds the SAME post_run_debt block,
        so the log (and its renderer) see the narrowed aggregate too."""
        world = _seed_world(tmp_path, "rmrr")
        run_input = {"mode": "backlog", "started_at": "2026-05-01T12:00:00",
                     "decompose": {"actioned": 0, "deferred": 0},
                     "distill": {"actioned": 0}}
        proc = _run_cli(world, [
            "update", "--record-maintenance", "--with-run-record"],
            json.dumps(run_input))
        assert proc.returncode == 0, (
            "tree.py update --record-maintenance --with-run-record failed:\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
        result = json.loads(proc.stdout)
        assert result["run_record"]["appended"] is True
        log_lines = (world / "tree-maintenance-log.jsonl").read_text(
            encoding="utf-8").strip().splitlines()
        rec = json.loads(log_lines[-1])
        assert rec["post_run_debt"]["total"] == 2
        assert rec["post_run_debt"]["distill"] == 4
        assert rec["post_run_debt"]["cleared"] is True
        assert rec["candidates_pre_filter"]["distill"]["candidates_in"] == 4


# ── D. the log renderer (tree-maintenance-read.py) ──────────────────────

class TestRendererDebtLine:

    @staticmethod
    def _debt_line(rec):
        line = tmr._fmt_human_record(rec)
        return [l for l in line.splitlines() if l.startswith("post_run_debt:")][0]

    @staticmethod
    def _rec(post_run_debt):
        return {
            "run_id": "maint-20260801120000-alpha", "agent": "alpha", "mode": "test",
            "started_at": "2026-05-17T13:00:00", "ended_at": "2026-05-17T13:01:00",
            "candidates_pre_filter": {}, "llm_reported": {},
            "post_run_debt": post_run_debt,
        }

    def test_actionable_record_shows_raw_and_excluded(self):
        line = self._debt_line(self._rec({
            "distill": 5, "distill_actionable": 2, "distill_low_utility": 2,
            "decompose": 0, "total": 2, "threshold": 40, "cleared": True,
        }))
        assert "total=2 (actionable)" in line
        assert "raw_distill=5" in line
        assert "low_utility_excluded=2" in line
        assert "cleared=True" in line and "threshold=40" in line

    def test_pre_g115_5421_record_still_renders(self):
        """Records written before this change lack the new raw fields —
        .get() defaults (em dash) keep them rendering, not crashing."""
        line = self._debt_line(self._rec(
            {"total": 0, "cleared": 0, "threshold": 20}))
        assert "total=0 (actionable)" in line
        assert "raw_distill=—" in line
        assert "low_utility_excluded=—" in line

    def test_aggregate_trend_line_unchanged(self):
        """The aggregate trend line renders total/cleared from records; it
        is debt-shape agnostic (a legacy record must still flow through)."""
        agg = tmr._aggregate([self._rec(
            {"total": 2, "cleared": True, "threshold": 40})])
        text = tmr._fmt_human_aggregate(agg)
        assert "post_run_debt trend (oldest → newest):" in text
        assert "total=2  cleared=True" in text


# ── E. daemon-mirror VALUE parity ────────────────────────────────────────
# The byte-compat key-order test lives in mind_api/tests/
# test_runtime_tree_write.py (it seeds its own baseline world); this test
# pins that the daemon's record-maintenance DEBT MATH matches the CLI's on
# a world where the math is non-trivial (raw 4 vs actionable 2).

class _FakePaths:
    def __init__(self, world: Path, project_root: Path, meta: Path):
        self.world = world
        self.project_root = project_root
        self.meta = meta


class _FakeCtx:
    def __init__(self, world: Path, body: dict,
                 project_root: Path = REPO_ROOT, meta: Path | None = None):
        self.paths = _FakePaths(world, project_root, meta or world / "meta")
        self.body = json.dumps(body).encode("utf-8")
        self.headers = {"x-mind-agent": "alpha"}
        self.query = {}


@pytest.mark.skipif(not _HAS_LIBYAML,
                    reason="libyaml (CSafeDumper) required for daemon write path")
@pytest.mark.skipif(not TREE_PY.exists(), reason="core/scripts/tree.py missing")
class TestDaemonDebtValueParity:

    def test_record_maintenance_debt_matches_cli(self, tmp_path):
        from mind_api.src.world import tree_write

        cli_world = _seed_world(tmp_path, "cli")
        dae_world = _seed_world(tmp_path, "dae")

        cli_proc = _run_cli(cli_world, ["update", "--record-maintenance"])
        assert cli_proc.returncode == 0, cli_proc.stderr
        resp = tree_write.write(_FakeCtx(dae_world, {"op": "record-maintenance"},
                                        meta=tmp_path / "dae-meta"))
        body = json.loads(resp.body.decode("utf-8"))
        assert body.get("ok") is True, body
        cli_debt = json.loads(cli_proc.stdout)["post_run_debt"]
        dae_debt = body["result"]["post_run_debt"]
        assert dae_debt == cli_debt, (
            f"daemon/CLI debt math drift:\n  CLI    {cli_debt}\n  daemon {dae_debt}\n"
            "Re-sync mind_api/src/world/tree_write.py record-maintenance to "
            "cmd_record_maintenance (g-115-5421).")
        # Both sides must have computed the fixture's actionable aggregate,
        # not just agreed with each other (a shared wrong count passes the
        # equality above — guard the guard, guard-2298).
        assert dae_debt["total"] == 2 and dae_debt["distill"] == 4
        assert dae_debt["distill_low_utility"] == 2 and dae_debt["cleared"] is True
        # last_backlog_clear_at lands on disk on both.
        assert yaml is not None
        for w in (cli_world, dae_world):
            t = yaml.safe_load(
                (w / "knowledge" / "tree" / "_tree.yaml").read_text(encoding="utf-8"))
            assert "last_backlog_clear_at" in t["maintenance"], str(w)
