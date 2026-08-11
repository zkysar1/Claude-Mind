"""test_wrapper_surface.py -- gap-066 /  wrapper invocation-surface query.

THE TEST SET IS PRE-REGISTERED, NOT INVENTED. Every case below is a call that
ACTUALLY FAILED during g-115-4632 (9 misses in one session, bravo, hostname cc-05,
uname -r 6.8.0-136-generic) before the tool existed. That matters: gap-066 was
FORGE-READY on count and deliberately HELD (foxtrot, g-115-1955) because "the
procedure AS SCOPED would have caught NEITHER encounter it just counted." The only
honest answer to that hold is a test set fixed BEFORE the tool was scored against
it. Measured result: 8 of 9. The one that escapes (#6) is asserted as a KNOWN LIMIT
below rather than quietly dropped -- a tool that passes its own tests while leaving
the live failure mode untouched is the exact outcome the hold was protecting against.

These assert on REAL repo scripts, deliberately. A fixture-only suite would pass
while the parsers drifted against the wrappers they exist to read (guard-920: the
regression test must replicate the production shape). The cost is that renaming a
flag on one of these wrappers reds this file -- which is the point.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
PROJECT_ROOT = _HERE.parent.parent.parent
MOD_PATH = PROJECT_ROOT / "core" / "scripts" / "wrapper-surface.py"


def _load():
    spec = importlib.util.spec_from_file_location("_wrapper_surface", MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ws = _load()


# --- the 9 pre-registered misses ------------------------------------------

def test_miss1_goal_selector_rejects_top_flag():
    """`goal-selector.sh --top 8` -> the subcommand parser ate --top."""
    d = ws.describe("goal-selector.sh")
    assert "--top" not in (d["flags"] or []), "tool must not claim --top exists"
    assert "select" in d["subcommands"] and "blocked" in d["subcommands"]


def test_miss2_goal_selector_select_takes_no_flags():
    d = ws.describe("goal-selector.sh")
    assert d["flags"] == [] or d["flags"] is not None
    assert "--top" not in (d["flags"] or [])


def test_miss3_aspirations_query_has_no_goal_id_flag():
    """`--goal-id` does not exist; `--goal-field` is the form (guard-694)."""
    d = ws.describe("aspirations-query.sh")
    assert "--goal-id" not in d["flags"]
    assert "--goal-field" in d["flags"]


def test_miss4_aspirations_read_has_no_aspiration_flag():
    d = ws.describe("aspirations-read.sh")
    assert "--aspiration" not in d["flags"]
    assert "--id" in d["flags"] and "--source" in d["flags"]


def test_miss5_infra_health_has_no_read_subcommand():
    d = ws.describe("infra-health.sh")
    assert "read" not in d["subcommands"]
    assert {"check", "status", "stale"} <= set(d["subcommands"])


def test_miss6_KNOWN_LIMIT_per_subcommand_arity_not_covered():
    """`infra-health.sh status bitnet` -- `status` is real, the positional is not.

    Per-subcommand argument arity is one level deeper than this tool models, and
    saying so out loud is the point: an unstated limit is how a narrow tool passes
    its own suite while the live trap keeps firing.
    """
    d = ws.describe("infra-health.sh")
    assert "status" in d["subcommands"]
    assert "arity" not in d and "positionals" not in d


def test_miss7_board_post_reads_stdin_and_has_no_body_flag():
    """The single clearest win: a flag list ALONE cannot say 'not passed as a flag'."""
    d = ws.describe("board-post.sh")
    assert d["reads_stdin"] is True
    for absent in ("--body", "--message", "--text", "--subject"):
        assert absent not in d["flags"], f"{absent} must not be reported"
    assert "--channel" in d["flags"]


def test_miss8_tree_update_exposes_its_op_flags():
    d = ws.describe("tree-update.sh")
    assert {"--set", "--add-child", "--batch"} <= set(d["flags"])


def test_miss9_missing_script_is_not_found_not_empty_surface():
    """An absent script must NOT read as 'accepts nothing' (guard-487 fail-closed)."""
    d = ws.describe("tree-set-summary.sh")
    assert d.get("error") == "not_found"


# --- the two blind spots foxtrot's hold named ------------------------------

def test_delegation_layer_follows_sh_to_py():
    """Blind spot 1: the capability can live one layer down (guard-2381)."""
    d = ws.describe("goal-selector.sh")
    assert "goal-selector.py" in d["delegates_to"]
    assert "select" in d["subcommands"], "subcommands come from the .py, not the .sh"


def test_daemon_layer_names_the_enforcing_party():
    """Blind spot 2 / guard-2374: the wrapper is a client, the endpoint is the contract."""
    d = ws.describe("aspirations-read.sh")
    assert d["daemon"]["routed"] is True
    assert "/v1/aspirations/read" in d["daemon"]["endpoints"]
    assert "mind_api/src/endpoints/" in d["daemon"]["note"]


def test_daemon_layer_does_not_claim_to_know_endpoint_rules():
    """The pointer must stay a pointer -- claiming to have read the validator
    mechanically would be the over-reach guard-2374 warns about."""
    d = ws.describe("aspirations-read.sh")
    assert set(d["daemon"].keys()) == {"routed", "endpoints", "note"}


# --- the recursion rule that the first cut got wrong -----------------------

def test_py_delegates_are_terminal_not_expanded():
    """REGRESSION (found by dogfooding): expanding a .py's delegates made
    goal-selector.sh transitively reach ~250 scripts, so its subcommand list
    became every case arm in the codebase and its endpoint list became all 62.
    A surface containing the right answer among 200 wrong ones is not an answer."""
    d = ws.describe("goal-selector.sh")
    assert d["delegates_to"] == ["goal-selector.py"], d["delegates_to"]
    assert len(d["subcommands"]) < 20, f"subcommand explosion: {len(d['subcommands'])}"
    assert len(d["daemon"]["endpoints"]) < 10, d["daemon"]["endpoints"]


def test_non_daemon_wrapper_reports_no_daemon():
    """Discrimination control: the daemon layer must not fire on everything."""
    d = ws.describe("goal-selector.sh")
    assert d["daemon"]["routed"] is False
    assert d["daemon"]["endpoints"] == []


def test_unparseable_surface_is_distinct_from_empty():
    """None (unparseable) and [] (parsed, accepts nothing) are different answers
    and must never be collapsed -- the same three-way discipline g-335-629 states
    for 403 vs 404 vs present."""
    d = ws.describe("goal-selector.sh")
    assert d["flags_unparseable"] is False
    assert isinstance(d["flags"], list)


def test_render_marks_unparseable_loudly():
    out = ws.render({
        "wrapper": "x.sh", "path": "core/scripts/x.sh", "flags": None,
        "flags_unparseable": True, "subcommands": [], "reads_stdin": False,
        "stdin_sources": [], "delegates_to": [],
        "daemon": {"routed": False, "endpoints": [], "note": ""},
    })
    assert "UNPARSEABLE" in out
    assert "do NOT infer it is empty" in out


def test_engine_is_imported_not_reimplemented():
    """One source of truth for the flag parsers (communication-clarity rule 5).
    If this file ever grows its own sh_flags/py_flags, the two consumers can
    disagree about what a wrapper accepts -- which is the drift the module exists
    to prevent."""
    src = MOD_PATH.read_text(encoding="utf-8")
    assert "_load_engine" in src
    assert "skillmd-flag-audit.py" in src
    assert "def sh_flags" not in src and "def py_flags" not in src


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
