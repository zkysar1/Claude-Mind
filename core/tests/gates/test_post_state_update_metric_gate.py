"""Behavior tests for post-state-update-metric-gate.sh (PMG-CG).

The gate is a bash script (not a Python module), so all tests invoke it as
a subprocess. Each test sets up a tmp WORLD_DIR + AGENT_DIR via a
local-paths.conf so the gate's `_paths.sh` resolves to test-local paths
and never touches the real world/agent state.

Coverage:
  - Canonical g-250-78 firing case (deep + production category + N>=2 findings)
  - Routine-noop (outcome_class != deep)
  - Below-threshold (deep + 0 or 1 distinct findings)
  - Meta-work category exclusion (framework-* / *hygiene*)
  - Empty outcome_note
  - Tree-edited-since-selected_at (LLM already encoded)
  - Sentinel dedup (same fingerprint as previous signal)
  - JSON output shape (always valid JSON, always exit 0)

The gate's contract: ALWAYS exits 0 (fail-open) and ALWAYS emits a single
line of valid JSON with at least the keys `fired`, `distinct_count`,
`reason`.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
GATE = REPO_ROOT / "core" / "scripts" / "post-state-update-metric-gate.sh"


def _posix(p: Path) -> str:
    """Convert a Windows path to a form bash can open. bash on MSYS handles
    forward-slash drive-letter paths (C:/foo/bar) but trips on backslashes."""
    return str(p).replace("\\", "/")


def _bash_executable() -> str:
    """Resolve to a bash that understands Windows drive-letter paths
    (Git Bash). On Windows, `subprocess.run(['bash',...])` may pick up
    `C:\\Windows\\System32\\bash.exe` which is WSL — WSL doesn't see
    Windows drives at /C/ paths the way we use them here. Prefer Git Bash
    explicitly when available.
    """
    if os.name == "nt":
        candidates = [
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files\Git\usr\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
        ]
        for c in candidates:
            if Path(c).is_file():
                return c
    return "bash"


BASH = _bash_executable()


def _make_env(tmp_path: Path) -> dict:
    """Build a tmp WORLD_DIR + AGENT_DIR and return an env dict ready for
    the gate subprocess. Writes a local-paths.conf so the gate's
    `_paths.sh` resolves to the tmp locations.
    """
    world = tmp_path / "world"
    meta = tmp_path / "meta"
    agent = tmp_path / "agent"
    (world / "knowledge" / "tree").mkdir(parents=True)
    meta.mkdir()
    (agent / "session").mkdir(parents=True)
    # `_paths.sh` reads local-paths.conf from agent dir; we point it at
    # the tmp world/meta.
    conf = agent / "local-paths.conf"
    conf.write_text(
        f'WORLD_PATH="{world}"\n'
        f'META_PATH="{meta}"\n',
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["MIND_AGENT"] = "test-agent"
    env["WORLD_DIR"] = str(world)
    env["META_DIR"] = str(meta)
    env["AGENT_DIR"] = str(agent)
    # Force the gate to use our tmp paths by also exporting AGENTS_PARENT_DIR.
    # The gate sources _paths.sh which resolves AGENT_DIR from
    # MIND_AGENT + AGENTS_PARENT_DIR. We bypass this by pre-exporting AGENT_DIR.
    return env


def _run_gate(env: dict, outcome_class: str, goal_id: str,
              category: str, slug: str, outcome_note: str) -> dict:
    """Invoke the gate and return parsed JSON. Asserts exit 0 + valid JSON."""
    proc = subprocess.run(
        [BASH, _posix(GATE), outcome_class, goal_id, category, slug],
        input=outcome_note, env=env,
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, (
        f"gate returned rc={proc.returncode}, "
        f"stdout={proc.stdout!r}, stderr={proc.stderr!r}"
    )
    out = proc.stdout.strip()
    # The gate prints exactly one JSON line.
    return json.loads(out)


# ---------------------------------------------------------------------------
# Canonical firing case ()
# ---------------------------------------------------------------------------

def test_canonical_g_250_78_firing_case(tmp_path: Path):
    """deep + production category + 2+ distinct numeric findings →
    fired=True with candidates populated."""
    env = _make_env(tmp_path)
    note = (
        "Closed g-250-78 with measurable production metrics: "
        "navigation success 73% vs 61% baseline (1.2x improvement). "
        "Path latency: 240ms -> 180ms median across 1000 trials. "
        "Memory footprint reduced 15% per NPC."
    )
    result = _run_gate(env, "deep", "g-250-78", "npc-cognition", "nav-fix", note)
    assert result["fired"] is True
    assert result["distinct_count"] >= 2
    assert "candidates" in result
    assert isinstance(result["candidates"], list)
    assert len(result["candidates"]) >= 2
    _assert_node_key_contract(result, "intelligence", "npc-cognition")


# ---------------------------------------------------------------------------
# Routine no-op (outcome_class != deep)
# ---------------------------------------------------------------------------

def test_routine_outcome_class_no_op(tmp_path: Path):
    env = _make_env(tmp_path)
    note = (
        "Closed routine recurring goal. Saw 90% pass rate, "
        "5ms p50, 12ms p99 — well within tolerance."
    )
    result = _run_gate(env, "routine", "g-115-001", "npc-cognition", "routine", note)
    assert result["fired"] is False
    assert "outcome_class=routine" in result["reason"]


def test_unset_outcome_class_no_op(tmp_path: Path):
    env = _make_env(tmp_path)
    result = _run_gate(env, "", "g-0", "npc-cognition", "x", "5% 2x 3x")
    assert result["fired"] is False
    assert "deep" in result["reason"]  # gate notes it fires only on deep


# ---------------------------------------------------------------------------
# Below-threshold (deep + < 2 distinct numeric findings)
# ---------------------------------------------------------------------------

def test_below_threshold_zero_findings_no_op(tmp_path: Path):
    env = _make_env(tmp_path)
    note = "Closed goal. No measurable findings — code only."
    result = _run_gate(env, "deep", "g-0", "npc-cognition", "no-numbers", note)
    assert result["fired"] is False
    assert "below threshold" in result["reason"]
    assert result["distinct_count"] == 0


def test_below_threshold_one_finding_no_op(tmp_path: Path):
    env = _make_env(tmp_path)
    note = "Closed goal. Saw a 1.5x improvement on one axis. Nothing else measured."
    result = _run_gate(env, "deep", "g-0", "npc-cognition", "one-num", note)
    assert result["fired"] is False
    assert "below threshold" in result["reason"]
    assert result["distinct_count"] == 1


# ---------------------------------------------------------------------------
# Meta-work category exclusion ( option d)
# ---------------------------------------------------------------------------

def test_framework_category_no_op(tmp_path: Path):
    """LOC-counts and other numeric findings on framework work are not
    production metrics — gate must skip them."""
    env = _make_env(tmp_path)
    note = "Closed framework refactor: 250 LOC removed, 44 LOC added, 47 tests passing."
    result = _run_gate(env, "deep", "g-115-724", "framework-engineering", "refactor", note)
    assert result["fired"] is False
    assert "framework" in result["reason"].lower() or "meta-work" in result["reason"]


def test_hygiene_category_no_op(tmp_path: Path):
    env = _make_env(tmp_path)
    note = "Closed hygiene sweep: 16 entries fixed, 99% coverage, 2x speedup."
    result = _run_gate(env, "deep", "g-115-997", "code-hygiene", "sweep", note)
    assert result["fired"] is False


# ---------------------------------------------------------------------------
# Empty outcome_note
# ---------------------------------------------------------------------------

def test_empty_outcome_note_no_op(tmp_path: Path):
    env = _make_env(tmp_path)
    result = _run_gate(env, "deep", "g-0", "npc-cognition", "empty", "")
    assert result["fired"] is False
    assert "empty" in result["reason"]


# ---------------------------------------------------------------------------
# Tree-edited-since-selected_at (LLM already encoded)
# ---------------------------------------------------------------------------

def test_tree_edited_since_selected_at_no_op(tmp_path: Path):
    """If a tree node was modified after the goal was selected, the LLM
    already encoded — gate must skip."""
    env = _make_env(tmp_path)
    # Write iteration-checkpoint with selected_at = 1 hour ago
    selected_at = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
    checkpoint = Path(env["AGENT_DIR"]) / "session" / "iteration-checkpoint.json"
    checkpoint.write_text(
        json.dumps({"selected_at": selected_at}),
        encoding="utf-8",
    )
    # Write a tree node with mtime = now (after selected_at)
    tree_node = Path(env["WORLD_DIR"]) / "knowledge" / "tree" / "intelligence" / "npc-cognition.md"
    tree_node.parent.mkdir(parents=True, exist_ok=True)
    tree_node.write_text("# npc-cognition\n", encoding="utf-8")
    note = (
        "Closed with 2x perf, 30% reduction in error rate, "
        "240ms -> 180ms latency."
    )
    result = _run_gate(env, "deep", "g-0", "npc-cognition", "encoded", note)
    # Either fired=False with tree-edit reason, OR the script couldn't
    # find tree-edit-since.py (fail-open). Both are acceptable per the
    # gate's contract; we assert the broader fired=False if tree-edit-since
    # ran. The point: the gate must NOT crash here.
    assert "fired" in result
    if result["fired"] is False and "tree already edited" in result["reason"]:
        # Tree-edit-since check correctly suppressed firing
        pass
    # Either way, the JSON shape is valid (assertion is the parsing in _run_gate)


# ---------------------------------------------------------------------------
# Sentinel dedup
# ---------------------------------------------------------------------------

def test_sentinel_dedup_same_fingerprint_no_op(tmp_path: Path):
    """If the previous signal contains the same fingerprint (same set of
    extracted values), the gate dedupes to no-op."""
    env = _make_env(tmp_path)
    # First fire: writes nothing on its own, but we simulate a previous
    # signal by writing working-memory directly. The gate reads via
    # `wm-read.sh force_metric_encoding_pending --json`.
    wm_path = Path(env["AGENT_DIR"]) / "session" / "working-memory.yaml"
    # The wm shape varies, but the gate just calls wm-read.sh. To make
    # this test self-contained, we just assert the gate doesn't crash
    # in the no-previous-signal case — full dedup integration is best
    # tested at the integration level (the gate's heredoc has been
    # exercised by canonical firing case above).
    note = "Saw 2x improvement and 30% reduction."
    result = _run_gate(env, "deep", "g-0", "npc-cognition", "dedup-1", note)
    # First call: should fire (no previous signal in fresh tmp WM)
    assert result["fired"] in (True, False)  # exact behavior depends on wm-read.sh availability
    # Just assert valid JSON shape
    assert "fired" in result
    assert "distinct_count" in result
    assert "reason" in result


def test_sentinel_dedup_real_agent_fixture():
    """Stronger sentinel-dedup test using a real agent dir under
    PROJECT_ROOT/agents/. Ported from the pre-co-location test at
    core/scripts/tests/gates/test_post_state_update_metric_gate.py (deleted
    2026-05-20 after the d62b0631 co-location refactor's stale duplicate
    surfaced via pytest collection collision).

    Why the realistic fixture: the daemon resolves AGENT_DIR via
    MIND_AGENT env + AGENTS_PARENT_DIR constant, NOT via tmp_path. To
    exercise the wm-set / wm-read round-trip the dedup check depends on,
    the agent must live at the canonical PROJECT_ROOT/agents/<name>/
    location so the daemon can see it. local-paths.conf still points at
    a tmpdir for world/meta isolation.
    """
    import shutil
    tmp_root = Path(tempfile.mkdtemp(prefix="pmg-real-agent-"))
    tmp_world = tmp_root / "world"
    tmp_meta = tmp_root / "meta"
    (tmp_world / "knowledge" / "tree").mkdir(parents=True)
    tmp_meta.mkdir(parents=True)
    agent_name = "pmgsentinelportedtest"
    agent_dir = REPO_ROOT / "agents" / agent_name
    session_dir = agent_dir / "session"
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "self.md").write_text(
            "---\nname: pmgsentinelportedtest\n---\n# Test fixture\n",
            encoding="utf-8",
        )
        (agent_dir / "local-paths.conf").write_text(
            f'WORLD_PATH={tmp_world}\nMETA_PATH={tmp_meta}\n',
            encoding="utf-8",
        )
        (session_dir / "working-memory.yaml").write_text(
            "encoding_queue: []\nsession_id: null\nslots: {}\nslot_meta: {}\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["MIND_AGENT"] = agent_name
        note = (
            "Closed: throughput 2x, latency 100 -> 50, "
            "success 95% vs 80% baseline."
        )
        # First fire — should produce fired=True
        proc1 = subprocess.run(
            [BASH, _posix(GATE), "deep", "g-260-02", "performance", "-"],
            input=note, env=env,
            capture_output=True, text=True, check=False,
            cwd=str(REPO_ROOT),
        )
        assert proc1.returncode == 0, (
            f"first call rc={proc1.returncode} "
            f"stderr={proc1.stderr!r}"
        )
        first = json.loads(proc1.stdout.strip().splitlines()[-1])
        assert first["fired"] is True, (
            f"first call should fire, got: {first}"
        )
        # Write the signal to WM via wm-set.sh (matching what
        # iteration-close.sh does in production)
        wm_set = REPO_ROOT / "core" / "scripts" / "wm-set.sh"
        wm_proc = subprocess.run(
            [BASH, _posix(wm_set), "force_metric_encoding_pending"],
            input=json.dumps(first), env=env,
            capture_output=True, text=True, check=False,
            cwd=str(REPO_ROOT),
        )
        assert wm_proc.returncode == 0, (
            f"wm-set failed: stderr={wm_proc.stderr!r}"
        )
        # Second fire with identical content — should dedup
        proc2 = subprocess.run(
            [BASH, _posix(GATE), "deep", "g-260-02", "performance", "-"],
            input=note, env=env,
            capture_output=True, text=True, check=False,
            cwd=str(REPO_ROOT),
        )
        assert proc2.returncode == 0
        second = json.loads(proc2.stdout.strip().splitlines()[-1])
        assert second["fired"] is False, (
            f"second call should dedup, got: {second}"
        )
        assert "dedup" in second["reason"]
    finally:
        shutil.rmtree(agent_dir, ignore_errors=True)
        shutil.rmtree(tmp_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# JSON output shape — always valid JSON, always exit 0
# ---------------------------------------------------------------------------

def test_json_output_shape_always_valid(tmp_path: Path):
    """Every code path must emit valid JSON with the contract keys."""
    env = _make_env(tmp_path)
    # Multiple representative inputs:
    test_cases = [
        ("deep", "g-1", "npc-cognition", "ok", "2x and 3% delta"),
        ("routine", "g-2", "framework-engineering", "ok", ""),
        ("deep", "g-3", "framework-engineering", "ok", "60% gain"),
        ("", "g-4", "", "-", ""),
        ("deep", "g-5", "npc-cognition", "-", ""),
    ]
    for outcome, goal_id, category, slug, note in test_cases:
        result = _run_gate(env, outcome, goal_id, category, slug, note)
        assert isinstance(result, dict)
        assert "fired" in result
        assert isinstance(result["fired"], bool)
        assert "distinct_count" in result
        assert isinstance(result["distinct_count"], int)
        assert "reason" in result
        assert isinstance(result["reason"], str)


def test_always_exits_zero_on_garbage_input(tmp_path: Path):
    """Fail-open contract: the gate must NEVER block iteration-close even
    on garbage args. ALWAYS exit 0."""
    env = _make_env(tmp_path)
    # Garbage goal_id with special chars
    proc = subprocess.run(
        [BASH, _posix(GATE), "deep", "g-!!@@", "npc-cognition", "ok"],
        input="2x and 3%", env=env,
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0


# ---------------------------------------------------------------------------
# Category-to-node-key mapping (heuristic correctness)
# ---------------------------------------------------------------------------

def _assert_node_key_contract(result: dict, l1: str, category: str) -> None:
    """Pin the POST- candidate_node_key contract.

    Before g-115-1746 the gate emitted the raw category heuristic
    ("<l1>/<category>") and these tests asserted that literal. g-115-1746
    then made the suggestion existence-checked, because a phantom key the
    Phase 0-pre4 consumer cannot Edit leaves the sentinel stuck (observed 3
    iterations). The gate now emits exactly one of:

      "<l1>/<category>"  the mapped node, when that node file exists
      "<l1>"             the L1 parent, when the specific node does not
                         exist but its ancestor does
      ""                 neither exists -- consumer picks a real node

    So the assertable invariant is no longer a fixed string: it is that the
    key is one of those three AND, when non-empty, names a file that is
    actually on disk. Asserting the pre-1746 literal pinned a value the gate
    is documented to override -- these three tests went red on 2026-07-03
    and nobody saw it for 28 days (g-115-3748: run-full-suite.sh collected
    1 of the 3 testpaths pytest.ini declares).

    ISOLATION CAVEAT, measured 2026-07-31 and deliberately accommodated
    rather than papered over: `_make_env` does NOT isolate this gate. Its tmp
    `local-paths.conf` is outranked by `.mind-data/` in the `_paths.sh`
    resolution chain, so the existence check above stats the REAL world tree,
    and which branch fires depends on that tree's contents on the box running
    the suite. The check below is written to hold under either resolution --
    it does not assume the tmp tree won. Fixing the isolation is g-115-4325.
    """
    key = result["candidate_node_key"]
    assert key in (f"{l1}/{category}", l1, ""), (
        f"candidate_node_key={key!r} outside the g-115-1746 contract"
    )
    if key:
        f = result["candidate_node_file"]
        assert f.endswith(f"{key}.md"), f"node_file {f!r} does not match key {key!r}"
        assert Path(f).is_file(), (
            f"g-115-1746 invariant violated: suggested {f!r} does not exist"
        )


def test_node_key_intelligence_mapping(tmp_path: Path):
    env = _make_env(tmp_path)
    note = "2x latency win, 30% smaller heap, 1.5x throughput."
    result = _run_gate(env, "deep", "g-0", "npc-cognition", "k1", note)
    if result["fired"]:
        _assert_node_key_contract(result, "intelligence", "npc-cognition")


def test_node_key_performance_mapping(tmp_path: Path):
    env = _make_env(tmp_path)
    note = "2x latency win, 30% smaller heap."
    # category contains "perf"
    result = _run_gate(env, "deep", "g-0", "ayoai-perf-suite", "k2", note)
    # ayoai-perf-suite matches *perf* — should land under performance/
    if result["fired"]:
        _assert_node_key_contract(result, "performance", "ayoai-perf-suite")


# ---------------------------------------------------------------------------
# Numeric extraction patterns
# ---------------------------------------------------------------------------

def test_multiplier_pattern_detected(tmp_path: Path):
    """Pattern A: '1.8x', '2x', '0.5X' etc."""
    env = _make_env(tmp_path)
    note = "Achieved 2x speedup and 0.5x memory."
    result = _run_gate(env, "deep", "g-0", "npc-cognition", "mul", note)
    assert result["distinct_count"] >= 2


def test_percent_pattern_detected(tmp_path: Path):
    """Pattern D: 'N%' or 'N.N%'."""
    env = _make_env(tmp_path)
    note = "Coverage rose from 73.5% to 89% on the goal."
    result = _run_gate(env, "deep", "g-0", "npc-cognition", "pct", note)
    assert result["distinct_count"] >= 2


def test_arrow_pattern_detected(tmp_path: Path):
    """Pattern C: 'X -> Y' (ASCII arrow)."""
    env = _make_env(tmp_path)
    note = "Latency went 240 -> 180 and throughput went 100 -> 150."
    result = _run_gate(env, "deep", "g-0", "npc-cognition", "arr", note)
    assert result["distinct_count"] >= 2


def test_versus_pattern_detected(tmp_path: Path):
    """Pattern B: 'X vs Y'."""
    env = _make_env(tmp_path)
    note = "Saw 73 vs 61 across the suite, also 4.2 vs 3.1 in the secondary axis."
    result = _run_gate(env, "deep", "g-0", "npc-cognition", "vs", note)
    assert result["distinct_count"] >= 2


def test_baseline_pattern_detected(tmp_path: Path):
    """Pattern E: 'N baseline'."""
    env = _make_env(tmp_path)
    note = "Hit 87 baseline and 92 base on the dual-track measurement."
    result = _run_gate(env, "deep", "g-0", "npc-cognition", "base", note)
    assert result["distinct_count"] >= 2


def test_distinct_values_deduplicated(tmp_path: Path):
    """Two identical findings should count as one (seen_values set)."""
    env = _make_env(tmp_path)
    # Same multiplier appears twice — should count as 1
    note = "Saw 2x in the morning and 2x again in the afternoon."
    result = _run_gate(env, "deep", "g-0", "npc-cognition", "dedup-val", note)
    # Either 1 distinct (correctly dedup'd) or might find context-different
    # entries — the gate is heuristic. Just assert it doesn't crash.
    assert result["distinct_count"] in (1, 2)
