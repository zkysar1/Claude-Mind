"""Pins for the per-agent fleet capacity snapshot (, gap-086).

The snapshot exists because the manual version -- loop goal-selector.sh once per
agent -- TIMED OUT at 2m (exit 143) on 2026-08-04 and left the largest routed
backlog unmeasured. The obvious fix (script that loop) is the one thing this
tool must never do, so most of what is worth pinning here is a NEGATIVE:

1. IT MUST NOT INVOKE goal-selector.sh. Non-idempotent (guard-2261: mutates
   drain-lane-state.json; every 5th run force-picks an overdue recurring goal)
   and stochastic (guard-3562). A snapshot that moved five agents' drain state
   would be a mutation wearing a measurement's clothes. Pinned STRUCTURALLY via
   the AST, not by a grep over prose, because the docstring names the script
   repeatedly on purpose.
2. THE SHARED POOL REACHES EVERY AGENT'S `available`. Dropping that term is the
   guard-2596 inversion in its original form: routed-only counts read one agent
   as starved while the shared pool it could draw from held 62% of the
   population. `test_routed_only_comparison_inverts_the_recommendation` builds
   exactly that shape and asserts the two orderings disagree -- so a future edit
   that "simplifies" available back to routed fails with the reason attached.
3. CONSERVATION BALANCES, and a non-balancing run EXITS 1. The check is the only
   thing standing between a dropped bucket and a confident wrong number; it is
   also what caught this script's OWN roster defect during development (a
   per-box roster left 1,134 goals in `unknown_routing`), so it is load-bearing
   in practice and not decoration.
4. AN ABSENT BUCKET IS NOT A REAL ZERO. An agent whose private queue this box
   has never synced renders `n/r`, never `0` (guard-3948 family).

Every fixture below writes into pytest's `tmp_path` with WORLD_DIR and agent_dir
monkeypatched at the module level. Nothing here reads or writes a live queue --
the non-hermetic variant of exactly this mistake was a fresh-eyes finding on the
sibling learning-ratio suite earlier the same session.
"""
import ast
import io
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "fleet_capacity_snapshot_under_test", SCRIPTS / "fleet-capacity-snapshot.py")
fcs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fcs)

SOURCE = (SCRIPTS / "fleet-capacity-snapshot.py").read_text(encoding="utf-8")


class _Args:
    """Stand-in for the argparse namespace render() reads."""
    aspiration = None
    exclude_recurring = False
    exclude_hypothesis = False


def _write_queue(path, aspirations):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(a) + "\n" for a in aspirations),
                    encoding="utf-8")


def _goal(gid, **kw):
    g = {"id": gid, "status": "pending", "priority": "MEDIUM"}
    g.update(kw)
    return g


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Hermetic world + per-agent queues. Never touches the live stores."""
    monkeypatch.setattr(fcs, "WORLD_DIR", tmp_path / "world")
    monkeypatch.setattr(fcs, "agent_dir", lambda a: tmp_path / "agents" / a)
    return tmp_path


def _world_queue(root):
    return root / "world" / "aspirations.jsonl"


def _agent_queue(root, agent):
    return root / "agents" / agent / "aspirations.jsonl"


# --------------------------------------------------------------------------
# 1. The structural pin: no selector invocation.
# --------------------------------------------------------------------------

def test_never_invokes_the_goal_selector():
    """guard-2261 + guard-3562: measuring must not move the subject.

    AST-based rather than a grep: the module docstring names goal-selector.sh
    several times BY DESIGN (explaining why it is not called), so a text search
    would either fail on the docs or be relaxed into uselessness.

    The predicate is the INVOCATION surface, not "any executed string". The
    first draft flagged every Call whose args mentioned the selector and duly
    failed on `out.append("top_pick ... goal-selector.sh ...")` -- the honesty
    banner, which is required output, not a call. Widening a matcher until the
    corpus stops complaining is how a check becomes unfalsifiable, so this
    narrows to what "invokes" actually means: a string reaching subprocess,
    os.system/os.popen, or any run/Popen/call/check_output callee.
    """
    tree = ast.parse(SOURCE)

    def _is_exec_call(node):
        f = node.func
        if isinstance(f, ast.Attribute):
            if f.attr in ("run", "Popen", "call", "check_call", "check_output",
                          "system", "popen", "spawnv", "execv"):
                return True
            return isinstance(f.value, ast.Name) and f.value.id in ("subprocess", "os")
        return isinstance(f, ast.Name) and f.id in ("system", "popen")

    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _is_exec_call(node)):
            continue
        for arg in ast.walk(node):
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                if "goal-selector" in arg.value or "goal_selector" in arg.value:
                    offenders.append((node.lineno, arg.value))
    assert offenders == [], f"selector reached an invocation: {offenders}"

    # Positive control: the predicate must actually fire on the shape it is
    # meant to catch. Without this the narrowing above could be over-narrowed
    # to vacuity and nothing would notice.
    bad = ast.parse('subprocess.run([bash, "core/scripts/goal-selector.sh"])')
    caught = [n for n in ast.walk(bad)
              if isinstance(n, ast.Call) and _is_exec_call(n)
              and any(isinstance(a, ast.Constant) and isinstance(a.value, str)
                      and "goal-selector" in a.value for a in ast.walk(n))]
    assert caught, "predicate is vacuous -- it no longer catches a real invocation"


def test_the_docstring_still_explains_why_not():
    """Positive control for the test above.

    Without this, deleting every mention of the selector would make the AST pin
    pass vacuously while the next author re-adds the loop for want of a reason
    not to. The rationale is the load-bearing artifact; the AST check only
    guards the code.
    """
    assert "guard-2261" in SOURCE and "guard-3562" in SOURCE
    assert "goal-selector.sh" in fcs.__doc__


def test_subprocess_is_only_used_for_team_state():
    """The single permitted shell-out, so a new one is a deliberate decision."""
    tree = ast.parse(SOURCE)
    runs = [n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "run"]
    assert len(runs) == 1, f"expected exactly one subprocess.run, found {len(runs)}"


# --------------------------------------------------------------------------
# 2. The guard-2596 anti-inversion property.
# --------------------------------------------------------------------------

def test_shared_pool_reaches_every_agents_available(world):
    _write_queue(_world_queue(world), [{
        "id": "asp-1", "status": "active", "goals": [
            _goal("g-1", intended_agent="alpha"),
            _goal("g-2", intended_agent="alpha"),
            _goal("g-3", intended_agent="either"),
            _goal("g-4"),                       # null routing -> shared
        ]}])
    data = fcs.collect(["alpha", "echo"])
    assert data["shared_pool"]["executable"] == 2
    assert data["agents"]["alpha"]["routed_executable"] == 2
    assert data["agents"]["echo"]["routed_executable"] == 0

    cons = fcs.conservation(data)
    assert cons["balances"], cons

    # echo has ZERO routed work but is not idle: the shared pool is available.
    rendered = fcs.render(data, {}, cons, _Args())
    echo_line = [ln for ln in rendered.splitlines() if ln.startswith("echo")][0]
    available_col = int(echo_line.split()[6])
    assert available_col == 2, echo_line


def test_routed_only_comparison_inverts_the_recommendation(world):
    """The measured 2026-08-04 shape, reproduced small.

    alpha holds all the DIRECTED work; echo holds none. A routed-only reading
    says "echo is starved, route work to it". Counting what each agent can
    actually PICK UP says they are equally supplied, because the shared pool
    dwarfs the routed split. The two orderings must disagree -- that
    disagreement is the whole reason the AVAILABLE column exists.
    """
    goals = [_goal(f"g-a{i}", intended_agent="alpha") for i in range(5)]
    goals += [_goal(f"g-s{i}", intended_agent="either") for i in range(50)]
    _write_queue(_world_queue(world),
                 [{"id": "asp-1", "status": "active", "goals": goals}])

    data = fcs.collect(["alpha", "echo"])
    routed_gap = (data["agents"]["alpha"]["routed_executable"]
                  - data["agents"]["echo"]["routed_executable"])
    shared = data["shared_pool"]["executable"]
    avail_alpha = data["agents"]["alpha"]["routed_executable"] + shared
    avail_echo = data["agents"]["echo"]["routed_executable"] + shared

    assert routed_gap == 5                      # routed-only: five-to-zero
    assert avail_echo / avail_alpha > 0.9       # available: near parity
    assert fcs.conservation(data)["balances"]


# --------------------------------------------------------------------------
# 3. Conservation.
# --------------------------------------------------------------------------

def test_unknown_routing_is_counted_never_dropped(world):
    """A retired-agent or typo'd intended_agent must show up, not vanish.

    Silently dropping it is what makes a conservation line lie: the count still
    balances against a population the run quietly redefined.
    """
    _write_queue(_world_queue(world), [{
        "id": "asp-1", "status": "active", "goals": [
            _goal("g-1", intended_agent="charlie"),      # not in the roster
            _goal("g-2", intended_agent="alpha"),
        ]}])
    data = fcs.collect(["alpha"])
    assert data["unknown_routing"] == {"charlie": 1}
    cons = fcs.conservation(data)
    assert cons["balances"] and cons["unknown_routing"] == 1


def test_carved_out_goals_stay_in_the_conservation_sum(world):
    """A carve-out removes goals from the COLUMNS, not from the population."""
    _write_queue(_world_queue(world), [{
        "id": "asp-1", "status": "active", "goals": [
            _goal("g-1", intended_agent="alpha", recurring=True),
            _goal("g-2", intended_agent="alpha"),
        ]}])
    data = fcs.collect(["alpha"], exclude_recurring=True)
    assert data["carved_out"] == {"recurring": 1}
    assert data["agents"]["alpha"]["routed"] == 1
    cons = fcs.conservation(data)
    assert cons["balances"], cons


def test_hypothesis_carve_out_is_independent(world):
    _write_queue(_world_queue(world), [{
        "id": "asp-1", "status": "active", "goals": [
            _goal("g-1", intended_agent="alpha", hypothesis_id="hyp-x"),
            _goal("g-2", intended_agent="alpha", recurring=True),
        ]}])
    data = fcs.collect(["alpha"], exclude_hypothesis=True)
    assert data["carved_out"] == {"hypothesis": 1}
    assert data["agents"]["alpha"]["routed"] == 1     # the recurring one stays
    assert fcs.conservation(data)["balances"]


def test_aspiration_scope_narrows_the_population_itself(world):
    _write_queue(_world_queue(world), [
        {"id": "asp-1", "status": "active",
         "goals": [_goal("g-1", intended_agent="alpha")]},
        {"id": "asp-2", "status": "active",
         "goals": [_goal("g-2", intended_agent="alpha")]},
    ])
    data = fcs.collect(["alpha"], aspiration="asp-2")
    assert data["population"] == 1
    assert fcs.conservation(data)["balances"]


def test_non_balancing_run_exits_1(world, monkeypatch, capsys):
    """A dropped bucket must not be reportable as a result."""
    _write_queue(_world_queue(world), [{
        "id": "asp-1", "status": "active",
        "goals": [_goal("g-1", intended_agent="alpha")]}])
    monkeypatch.setattr(fcs, "_discover_agents", lambda: ["alpha"])
    monkeypatch.setattr(fcs, "_read_team_state", lambda: {})
    real_conservation = fcs.conservation
    monkeypatch.setattr(
        fcs, "conservation",
        lambda d: {**real_conservation(d), "balances": False, "residual": 7})
    rc = fcs.main([])
    assert rc == 1
    assert "DOES NOT BALANCE" in capsys.readouterr().out


# --------------------------------------------------------------------------
# 4. Absent bucket vs real zero.
# --------------------------------------------------------------------------

def test_unsynced_private_queue_renders_nr_not_zero(world):
    _write_queue(_world_queue(world), [{
        "id": "asp-1", "status": "active",
        "goals": [_goal("g-1", intended_agent="alpha")]}])
    _write_queue(_agent_queue(world, "alpha"), [{
        "id": "asp-p", "status": "active", "goals": [_goal("g-p1")]}])
    # zeta's dir is never created -> its queue is not on this box.
    data = fcs.collect(["alpha", "zeta"])
    assert data["agents"]["alpha"]["private_queue_readable"] is True
    assert data["agents"]["zeta"]["private_queue_readable"] is False

    rendered = fcs.render(data, {}, fcs.conservation(data), _Args())
    zeta_line = [ln for ln in rendered.splitlines() if ln.startswith("zeta")][0]
    assert "n/r" in zeta_line, zeta_line
    assert "LOWER BOUND" in rendered


def test_private_queue_goals_are_owned_by_their_agent(world):
    """A private queue needs no routing field -- ownership is structural."""
    _write_queue(_world_queue(world), [])
    _write_queue(_agent_queue(world, "alpha"), [{
        "id": "asp-p", "status": "active",
        "goals": [_goal("g-p1", priority="HIGH"),
                  _goal("g-p2", status="completed")]}])
    data = fcs.collect(["alpha"])
    assert data["agents"]["alpha"]["private"] == 2
    assert data["agents"]["alpha"]["private_executable"] == 1
    assert data["agents"]["alpha"]["private_high"] == 1
    assert data["shared_pool"]["total"] == 0        # never leaks into shared
    assert fcs.conservation(data)["balances"]


def test_deferred_and_blocked_goals_are_not_executable(world):
    """`routed` counts assignment; `r-exec` counts pickability. Not the same."""
    _write_queue(_world_queue(world), [{
        "id": "asp-1", "status": "active", "goals": [
            _goal("g-1", intended_agent="alpha"),
            _goal("g-2", intended_agent="alpha", defer_reason="human_blocked: x"),
            _goal("g-3", intended_agent="alpha", blocker_ref="blk-1"),
            _goal("g-4", intended_agent="alpha", status="blocked"),
            _goal("g-5", intended_agent="alpha", status="in-progress"),
        ]}])
    data = fcs.collect(["alpha"])
    assert data["agents"]["alpha"]["routed"] == 5
    assert data["agents"]["alpha"]["routed_executable"] == 1
    assert data["excluded_from_executable"] == {
        "deferred": 1, "blocker_ref": 1, "status:blocked": 1, "in-progress": 1}
    assert fcs.conservation(data)["balances"]


def test_retired_aspirations_are_out_of_scope_entirely(world):
    _write_queue(_world_queue(world), [
        {"id": "asp-dead", "status": "archived",
         "goals": [_goal("g-x", intended_agent="alpha")]},
        {"id": "asp-1", "status": "active",
         "goals": [_goal("g-1", intended_agent="alpha")]},
    ])
    data = fcs.collect(["alpha"])
    assert data["population"] == 1
    assert fcs.conservation(data)["balances"]


# --------------------------------------------------------------------------
# 5. The honesty banners are part of the contract, not decoration.
# --------------------------------------------------------------------------

def test_output_declares_top_pick_unmeasured_and_refuses_throughput(world):
    _write_queue(_world_queue(world), [{
        "id": "asp-1", "status": "active",
        "goals": [_goal("g-1", intended_agent="alpha")]}])
    data = fcs.collect(["alpha"])
    rendered = fcs.render(data, {}, fcs.conservation(data), _Args())
    assert "NOT MEASURED" in rendered
    assert "INVENTORY, NOT THROUGHPUT" in rendered
    assert "MUST NOT be summed" in rendered


def test_json_payload_carries_the_same_disclaimers(world, monkeypatch, capsys):
    _write_queue(_world_queue(world), [{
        "id": "asp-1", "status": "active",
        "goals": [_goal("g-1", intended_agent="alpha")]}])
    monkeypatch.setattr(fcs, "_discover_agents", lambda: ["alpha"])
    monkeypatch.setattr(fcs, "_read_team_state", lambda: {})
    rc = fcs.main(["--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["top_pick"] is None
    assert "guard-2261" in payload["top_pick_not_measured_reason"]
    assert "guard-3016" in payload["not_a_throughput_signal"]
    assert payload["conservation"]["balances"] is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


def test_a_record_lost_at_parse_makes_the_run_not_a_result(world, monkeypatch, capsys):
    """A parse drop is INVISIBLE to the conservation identity, by construction.

    A line json.loads cannot parse never increments `population` AND never lands
    in a bucket, so both sides of the identity lose it together: `balances` stays
    True and `residual` stays 0 while the census is short. That is precisely why
    the drop is counted separately -- the conservation line is this tool's
    advertised trust signal, and this is the one class it cannot see.

    The `BALANCES` assertion at the end is the point of the test, not an
    afterthought: it pins that the identity STILL reads clean on a short census,
    so a future edit that tries to detect parse drops through `residual` alone
    fails here with the reason attached.
    """
    q = _world_queue(world)
    _write_queue(q, [{"id": "asp-1", "status": "active",
                      "goals": [_goal("g-1", intended_agent="alpha")]}])
    monkeypatch.setattr(fcs, "_discover_agents", lambda: ["alpha"])
    monkeypatch.setattr(fcs, "_read_team_state", lambda: {})

    # POSITIVE CONTROL (guard-4166): the same corpus, well-formed, exits 0.
    # Without it this test would pass against a main() that exited 1 always.
    assert fcs.main([]) == 0
    capsys.readouterr()

    with io.open(q, "a", encoding="utf-8") as fh:
        fh.write("{not json\n")

    rc = fcs.main([])
    out = capsys.readouterr().out
    assert rc == 1, "a census that silently lost a record is not a result"
    assert "DROPPED AT PARSE" in out
    assert "BALANCES" in out, (
        "the identity must still read clean -- if it does not, the drop became "
        "visible to `residual` and this test is no longer pinning what it claims")


def test_a_failed_team_state_read_is_not_rendered_as_nobody_busy(world, monkeypatch):
    """`None` (read failed) and `{}` (read fine, nobody busy) are different facts.

    Rendering both as an empty in_flight column reports "nobody in the fleet is
    mid-execution" from no evidence -- the guard-3016 reading rule violated in
    the one column that reads as live state, in a file that already prints `n/r`
    for an unsynced private queue and NOT MEASURED for top_pick.

    The `empty` half is the positive control: the two renders must DIFFER, so a
    regression that collapses None back to {} cannot pass by making both say n/r.
    """
    _write_queue(_world_queue(world), [{"id": "asp-1", "status": "active",
                                        "goals": [_goal("g-1", intended_agent="alpha")]}])
    monkeypatch.setattr(fcs, "_discover_agents", lambda: ["alpha"])
    data = fcs.collect(["alpha"], None, False, False)
    cons = fcs.conservation(data)

    failed = fcs.render(data, None, cons, _Args())
    empty = fcs.render(data, {}, cons, _Args())

    assert "team-state read FAILED" in failed
    assert "team-state read FAILED" not in empty
    assert failed != empty, "read-failed and genuinely-empty must not render alike"
