""" — utility_ratio must recompute on the whole-object retrieval_stats write.

The defect: `cmd_update_field` rejects any field name containing a dot and THEN
guards the utility_ratio recompute on `field.startswith("retrieval_stats.")`.
Guard first, so the recompute was unreachable from 2026-05-10 (g-115-566, the
fail-loud dotted rejection) until 2026-08-04. Measured consequence: utility_ratio
read 0.0 on 4,174 of 4,175 fleet records, so the archive sweep's "never archive
high-value experiences" protection (rc>=5 AND ur>=0.5) qualified NONE of them
while 174 records with rc>=5 had already been swept to archive.

The whole-object form -- `experience-update-field.sh <id> retrieval_stats
'<blob>'` -- is the ONLY shape that reaches the recompute, and it is the shape
verify-learning Section DPS and reflect-on-outcome both prescribe, precisely
BECAUSE the dotted form fails.

Two-sided by construction (guard-130). `experience-update-field.sh` is
daemon-only (rt_no_daemon_error, no CLI fallback), so the LIVE path is
mind_api/src/endpoints/experience_write.py; the CLI twin in
core/scripts/experience.py must not drift from it. The source pins below fail on
EITHER side being narrowed back to the dotted-only predicate -- a behavioural
test of the CLI alone would pass while the path that actually runs stayed dead.
"""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CLI_PATH = PROJECT_ROOT / "core" / "scripts" / "experience.py"
DAEMON_PATH = PROJECT_ROOT / "mind_api" / "src" / "endpoints" / "experience_write.py"

_RESYNC = (
    "Both implementations must carry the whole-object arm. Re-read "
    "core/scripts/experience.py cmd_update_field AND "
    "mind_api/src/endpoints/experience_write.py update_field together before "
    "changing either (guard-130, g-115-4969)."
)


# ---------------------------------------------------------------------------
# Source pins -- fail if the recompute is narrowed back to the dotted-only form
# ---------------------------------------------------------------------------

def _recompute_guard_test(path: Path, func_name: str) -> ast.expr:
    """Return the test expression of the `if` that assigns utility_ratio.

    Parsed, never imported: the daemon module pulls in server-side deps that a
    unit test has no business spinning up.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != func_name:
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.If):
                continue
            for stmt in ast.walk(inner):
                if not isinstance(stmt, ast.Assign):
                    continue
                for tgt in stmt.targets:
                    if (isinstance(tgt, ast.Subscript)
                            and isinstance(tgt.slice, ast.Constant)
                            and tgt.slice.value == "utility_ratio"):
                        return inner.test
    raise AssertionError(
        f"no utility_ratio recompute found in {path.name}::{func_name} -- the "
        f"branch this pin protects has been deleted outright. " + _RESYNC
    )


def _guard_compares_field_to(test: ast.expr, literal: str) -> bool:
    """True when the guard contains `field == "<literal>"` anywhere in its tree."""
    for node in ast.walk(test):
        if not isinstance(node, ast.Compare):
            continue
        if not (isinstance(node.left, ast.Name) and node.left.id == "field"):
            continue
        if not (len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq)):
            continue
        cmp_to = node.comparators[0]
        if isinstance(cmp_to, ast.Constant) and cmp_to.value == literal:
            return True
    return False


def _guard_has_startswith(test: ast.expr, literal: str) -> bool:
    for node in ast.walk(test):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "startswith"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "field"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == literal):
            return True
    return False


@pytest.mark.parametrize(
    "path,func",
    [(CLI_PATH, "cmd_update_field"), (DAEMON_PATH, "update_field")],
    ids=["cli", "daemon"],
)
def test_recompute_guard_accepts_whole_object_write(path, func):
    """The load-bearing arm. Narrowing to dotted-only re-kills the branch."""
    test = _recompute_guard_test(path, func)
    assert _guard_compares_field_to(test, "retrieval_stats"), (
        f"{path.name}::{func} guards the utility_ratio recompute WITHOUT a "
        f"`field == \"retrieval_stats\"` arm. The dotted field name that the "
        f"startswith() arm matches is rejected earlier in the same code path, "
        f"so this branch is now dead and utility_ratio will silently stay 0.0 "
        f"on every record. " + _RESYNC
    )


@pytest.mark.parametrize(
    "path,func",
    [(CLI_PATH, "cmd_update_field"), (DAEMON_PATH, "update_field")],
    ids=["cli", "daemon"],
)
def test_recompute_guard_keeps_dotted_arm(path, func):
    """Kept deliberately: correct if the dotted rejection is ever relaxed."""
    test = _recompute_guard_test(path, func)
    assert _guard_has_startswith(test, "retrieval_stats."), (
        f"{path.name}::{func} dropped the `field.startswith(\"retrieval_stats.\")` "
        f"arm. It is unreachable today by design, and kept so the recompute "
        f"stays correct if the dotted-path rejection is relaxed. " + _RESYNC
    )


def test_cli_and_daemon_guards_agree():
    """guard-130: a one-sided change fixes nothing. Pin the pair, not each half."""
    cli = _recompute_guard_test(CLI_PATH, "cmd_update_field")
    daemon = _recompute_guard_test(DAEMON_PATH, "update_field")
    cli_shape = (_guard_compares_field_to(cli, "retrieval_stats"),
                 _guard_has_startswith(cli, "retrieval_stats."))
    daemon_shape = (_guard_compares_field_to(daemon, "retrieval_stats"),
                    _guard_has_startswith(daemon, "retrieval_stats."))
    assert cli_shape == daemon_shape, (
        f"recompute-guard drift: CLI accepts {cli_shape} but daemon accepts "
        f"{daemon_shape} (whole_object, dotted). The daemon is the LIVE path. "
        + _RESYNC
    )


# ---------------------------------------------------------------------------
# Behavioural -- the CLI end to end against an isolated world
# ---------------------------------------------------------------------------

def _seed(tmp_path, stats=None):
    """Write a minimal valid experience record and return (env, exp_jsonl)."""
    world = tmp_path / "world"
    meta = tmp_path / "meta"
    agent_dir = tmp_path / "alpha"
    (world / "board").mkdir(parents=True)
    meta.mkdir(parents=True)
    (agent_dir / "session").mkdir(parents=True)
    (agent_dir / "experience").mkdir(parents=True)

    content = agent_dir / "experience" / "exp-ur.md"
    content.write_text("trace body\n", encoding="utf-8")

    rec = {
        "id": "exp-ur",
        "type": "goal_execution",
        "category": "framework-hygiene",
        "summary": "a sufficiently long summary line for validation",
        "content_path": str(content),
    }
    if stats is not None:
        rec["retrieval_stats"] = stats
    exp_jsonl = agent_dir / "experience.jsonl"
    exp_jsonl.write_text(json.dumps(rec) + "\n", encoding="utf-8")

    env = dict(os.environ)
    env.update({
        # guard-955/rb-2983: an own-cloud subprocess derives its S3 key from
        # customer_prefix+env_id+filename, NOT from the MIND_* tmp overrides --
        # so without this pin a tmp write lands on the PRODUCTION key.
        "STORAGE_BACKEND": "local",
        "MIND_WORLD": str(world),
        "MIND_META": str(meta),
        "MIND_AGENT_DIR": str(agent_dir),
        "MIND_AGENT": "alpha",
    })
    return env, exp_jsonl


def _update_field(env, field, value):
    return subprocess.run(
        [sys.executable, str(CLI_PATH), "update-field", "exp-ur", field, value],
        capture_output=True, text=True, timeout=60, env=env,
    )


def _read(exp_jsonl):
    lines = [ln for ln in exp_jsonl.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return json.loads(lines[0])


def test_whole_object_write_recomputes_utility_ratio(tmp_path):
    """The regression itself: 2 useful of 4 retrievals must derive to 0.5."""
    env, exp_jsonl = _seed(tmp_path)
    r = _update_field(env, "retrieval_stats", json.dumps(
        {"retrieval_count": 4, "times_useful": 2, "times_noise": 0,
         "utility_ratio": 0.0, "last_retrieved": None}))
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    stats = _read(exp_jsonl)["retrieval_stats"]
    assert stats["utility_ratio"] == 0.5, (
        f"utility_ratio stayed {stats['utility_ratio']} after a whole-object "
        f"write of rc=4/tu=2 -- the recompute did not fire. " + _RESYNC
    )


def test_whole_object_write_qualifies_the_archive_protection_guard(tmp_path):
    """The consequence that made the dead branch matter, not just the branch.

    cmd_archive_sweep protects rc>=5 AND ur>=0.5. With the recompute dead, the
    second half was unsatisfiable for every record in the corpus.
    """
    env, exp_jsonl = _seed(tmp_path)
    r = _update_field(env, "retrieval_stats", json.dumps(
        {"retrieval_count": 8, "times_useful": 6}))
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    stats = _read(exp_jsonl)["retrieval_stats"]
    assert stats["retrieval_count"] >= 5 and stats["utility_ratio"] >= 0.5, (
        f"a record with 6 useful retrievals of 8 does not qualify for the "
        f"archive-sweep high-value protection: {stats}"
    )


def test_partial_whole_object_write_backfills_instead_of_crashing(tmp_path):
    """A whole-object write REPLACES the dict normalize_record backfilled.

    The recompute reads retrieval_count/times_useful with strict lookups, so
    without the re-normalize a payload omitting either raises KeyError inside
    the lock -- a traceback, not the clean validation error every other bad
    write produces.
    """
    env, exp_jsonl = _seed(tmp_path)
    r = _update_field(env, "retrieval_stats", json.dumps({"retrieval_count": 3}))
    assert r.returncode == 0, (
        f"partial retrieval_stats payload crashed instead of backfilling: "
        f"stdout={r.stdout}\nstderr={r.stderr}"
    )
    stats = _read(exp_jsonl)["retrieval_stats"]
    assert stats["times_useful"] == 0 and stats["utility_ratio"] == 0.0, stats


def test_dotted_field_still_rejected(tmp_path):
    """Do NOT 'fix' this by relaxing the dotted guard ( Option A)."""
    env, _ = _seed(tmp_path)
    r = _update_field(env, "retrieval_stats.times_useful", "3")
    assert r.returncode == 1, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert "BLOCKED" in r.stderr, r.stderr


def test_unrelated_field_write_leaves_utility_ratio_alone(tmp_path):
    """The widened arm must not make every write a recompute."""
    env, exp_jsonl = _seed(tmp_path, stats={
        "retrieval_count": 4, "times_useful": 2, "times_noise": 0,
        "utility_ratio": 0.99, "last_retrieved": None})
    r = _update_field(env, "category", "some-other-category")
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    rec = _read(exp_jsonl)
    assert rec["category"] == "some-other-category"
    assert rec["retrieval_stats"]["utility_ratio"] == 0.99, (
        "a write to an unrelated field triggered the retrieval_stats recompute"
    )
