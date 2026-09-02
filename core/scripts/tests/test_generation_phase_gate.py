# Regression tests for the domain-phase + demand-first generation gate
# (core/scripts/generation_phase_gate.py). Runnable two ways:
#   py -3 core/scripts/tests/test_generation_phase_gate.py     (standalone)
#   py -3 -m pytest core/scripts/tests/test_generation_phase_gate.py -q
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]          # project root
SCRIPT = Path(__file__).resolve().parents[1] / "generation_phase_gate.py"
sys.path.insert(0, str(SCRIPT.parent))

import generation_phase_gate as G  # noqa: E402

# A domain-free fixture reproducing the asp-013 SHAPE: a preparation window
# that has closed, an execution window that is open now, and a work category
# that only makes sense before the boundary. Names are generic per
# .claude/rules/domain-free-examples.md -- the incident's domain nouns stay in
# the goal record, not in a core test.
FIXTURE = """# Domain Calendar (fixture)

Prose above the block is ignored by the parser.

```yaml
phases:
  - id: preparation
    starts: 2026-01-01
    ends:   2026-06-30
    valid_categories:   [preparation-strategy, design]
    invalid_categories: [live-operations]
  - id: execution
    starts: 2026-07-01
    ends:   2026-12-31
    valid_categories:   [live-operations, support]
    invalid_categories: [preparation-strategy]
demand:
  actionable_types: [directive, escalation, question]
  max_unconsumed: 0
```

Prose below the block is ignored too.
"""

NOW_IN_EXECUTION = datetime(2026, 9, 2, 12, 0, 0)
NOW_IN_PREPARATION = datetime(2026, 3, 1, 12, 0, 0)


def _write(tmp: Path, text: str) -> Path:
    p = tmp / "domain-calendar.md"
    p.write_text(text, encoding="utf-8")
    return p


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, cwd=str(ROOT)
    )


# ---- outcome 1: the slot is parsed and the current phase resolves -----------
def test_fixture_calendar_parses_and_resolves_current_phase(tmp_path):
    cal = G.load_calendar(_write(tmp_path, FIXTURE))
    assert cal is not None, "fenced yaml block with phases must parse"
    assert len(cal["phases"]) == 2
    assert G.resolve_phase(cal, NOW_IN_EXECUTION)["id"] == "execution"
    assert G.resolve_phase(cal, NOW_IN_PREPARATION)["id"] == "preparation"


def test_end_date_covers_the_whole_final_day(tmp_path):
    # A bare `ends: 2026-06-30` must not expire at 00:00 on the 30th.
    cal = G.load_calendar(_write(tmp_path, FIXTURE))
    assert G.resolve_phase(cal, datetime(2026, 6, 30, 23, 30))["id"] == "preparation"


# ---- outcome 2: the asp-013 shape is refused --------------------------------
def test_phase_invalid_candidate_is_refused(tmp_path):
    """The incident shape: preparation work generated after the boundary."""
    cal = G.load_calendar(_write(tmp_path, FIXTURE))
    got = G.phase_check("preparation-strategy", cal, NOW_IN_EXECUTION)
    assert got["decision"] == "refuse"
    assert got["phase"] == "execution"
    assert "preparation-strategy" in got["reason"]


def test_same_candidate_allowed_inside_its_own_phase(tmp_path):
    cal = G.load_calendar(_write(tmp_path, FIXTURE))
    assert G.phase_check("preparation-strategy", cal, NOW_IN_PREPARATION)["decision"] == "allow"


def test_allow_list_refuses_an_undeclared_category(tmp_path):
    cal = G.load_calendar(_write(tmp_path, FIXTURE))
    got = G.phase_check("something-unlisted", cal, NOW_IN_EXECUTION)
    assert got["decision"] == "refuse" and "allow-list" in got["reason"]


def test_phase_check_is_case_insensitive(tmp_path):
    cal = G.load_calendar(_write(tmp_path, FIXTURE))
    assert G.phase_check("Preparation-Strategy", cal, NOW_IN_EXECUTION)["decision"] == "refuse"


def test_cli_phase_check_exit_codes(tmp_path):
    cal = _write(tmp_path, FIXTURE)
    bad = _run("phase-check", "--calendar", str(cal), "--category", "preparation-strategy",
               "--now", "2026-09-02T12:00:00", "--json")
    assert bad.returncode == 1, bad.stdout + bad.stderr
    assert json.loads(bad.stdout)["decision"] == "refuse"
    ok = _run("phase-check", "--calendar", str(cal), "--category", "live-operations",
              "--now", "2026-09-02T12:00:00", "--json")
    assert ok.returncode == 0 and json.loads(ok.stdout)["decision"] == "allow"


# ---- outcome 3: demand-first ordering ---------------------------------------
def _post(pid, typ, author, reply_to=None):
    return {"id": pid, "type": typ, "author": author, "timestamp": "2026-09-02T10:00:00",
            "text": "t", "reply_to": reply_to, "tags": []}


def test_unconsumed_actionable_demand_defers_generation(tmp_path):
    cal = G.load_calendar(_write(tmp_path, FIXTURE))
    posts = [_post("p1", "directive", "bravo@env"), _post("p2", "finding", "bravo@env")]
    got = G.demand_check(posts, "alpha@env", cal)
    assert got["decision"] == "defer"
    assert got["unconsumed_count"] == 1, "only the directive is demand; a finding is traffic"


def test_answered_demand_does_not_defer(tmp_path):
    cal = G.load_calendar(_write(tmp_path, FIXTURE))
    posts = [_post("p1", "directive", "bravo@env"), _post("r1", "status", "alpha@env", reply_to="p1")]
    assert G.demand_check(posts, "alpha@env", cal)["decision"] == "allow"


def test_a_peers_reply_does_not_consume_my_demand(tmp_path):
    cal = G.load_calendar(_write(tmp_path, FIXTURE))
    posts = [_post("p1", "directive", "bravo@env"), _post("r1", "status", "zeta@env", reply_to="p1")]
    assert G.demand_check(posts, "alpha@env", cal)["decision"] == "defer"


def test_own_posts_are_not_demand_on_self(tmp_path):
    cal = G.load_calendar(_write(tmp_path, FIXTURE))
    assert G.demand_check([_post("p1", "directive", "alpha@env")], "alpha@env", cal)["decision"] == "allow"


def test_agent_matches_across_env_suffixes(tmp_path):
    cal = G.load_calendar(_write(tmp_path, FIXTURE))
    posts = [_post("p1", "directive", "alpha@other-world")]
    assert G.demand_check(posts, "alpha", cal)["decision"] == "allow"


def test_default_actionable_set_has_no_severity_dependency():
    """guard-159: the live board carries no `severity` field on any post."""
    posts = [_post("p1", "escalation", "bravo@env")]
    got = G.demand_check(posts, "alpha@env", None)
    assert got["decision"] == "defer"
    assert "severity" not in json.dumps(got)


def test_domain_may_widen_the_actionable_set(tmp_path):
    cal = G.parse_calendar(FIXTURE.replace("[directive, escalation, question]", "[finding]"))
    assert G.demand_check([_post("p1", "finding", "b@e")], "alpha@e", cal)["decision"] == "defer"
    assert G.demand_check([_post("p1", "directive", "b@e")], "alpha@e", cal)["decision"] == "allow"


def test_cli_demand_check_reads_board_jsonl(tmp_path):
    cal = _write(tmp_path, FIXTURE)
    feed = tmp_path / "posts.jsonl"
    feed.write_text("\n".join(json.dumps(p) for p in
                              [_post("p1", "directive", "bravo@env"), _post("p2", "claim", "bravo@env")]),
                    encoding="utf-8")
    r = _run("demand-check", "--calendar", str(cal), "--posts-file", str(feed),
             "--author", "alpha@env", "--json")
    assert r.returncode == 1, r.stdout + r.stderr
    assert json.loads(r.stdout)["unconsumed_count"] == 1


# ---- outcome 4: fail-open when no calendar is declared -----------------------
def test_absent_calendar_fails_open(tmp_path):
    missing = tmp_path / "nope.md"
    assert G.load_calendar(missing) is None
    got = G.phase_check("anything", None)
    assert got["decision"] == "fail-open" and got["phase"] is None


def test_unparseable_calendar_fails_open(tmp_path):
    p = _write(tmp_path, "# Calendar\n\nno fenced yaml here at all\n")
    assert G.load_calendar(p) is None
    assert G.phase_check("anything", G.load_calendar(p))["decision"] == "fail-open"


def test_malformed_yaml_fails_open(tmp_path):
    p = _write(tmp_path, "```yaml\nphases: [ unclosed\n```\n")
    assert G.load_calendar(p) is None


def test_yaml_block_without_phases_is_not_a_calendar(tmp_path):
    p = _write(tmp_path, "```yaml\nsomething_else: 1\n```\n")
    assert G.load_calendar(p) is None


def test_time_outside_every_declared_window_fails_open(tmp_path):
    cal = G.load_calendar(_write(tmp_path, FIXTURE))
    got = G.phase_check("preparation-strategy", cal, datetime(2027, 5, 1))
    assert got["decision"] == "fail-open"


def test_cli_fail_open_exits_zero(tmp_path):
    r = _run("phase-check", "--calendar", str(tmp_path / "absent.md"),
             "--category", "anything", "--json")
    assert r.returncode == 0 and json.loads(r.stdout)["decision"] == "fail-open"


# ---- the wiring itself (the outcome-observation rot lesson, domain-hooks.md) --
def test_generation_lanes_actually_call_the_gate():
    """A hook wired only in prose rots invisibly. Pin the call sites."""
    for rel in (".claude/skills/generate-domain-goals/SKILL.md",
                ".claude/skills/create-aspiration/SKILL.md"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "generation_phase_gate.py" in text, f"{rel} must CALL the gate, not describe it"


def test_slot_is_registered_in_domain_hooks():
    text = (ROOT / "core/config/conventions/domain-hooks.md").read_text(encoding="utf-8")
    assert "`domain-calendar`" in text, "the slot must be in the Canonical Hook Slots table"


def test_gate_is_registered_in_gates_yaml():
    text = (ROOT / "core/config/gates.yaml").read_text(encoding="utf-8")
    assert "generation-phase-gate" in text


if __name__ == "__main__":
    import tempfile, traceback
    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in fns:
        with tempfile.TemporaryDirectory() as td:
            try:
                fn(Path(td)) if fn.__code__.co_argcount else fn()
                print(f"  PASS {name}")
            except Exception:
                failed += 1
                print(f"  FAIL {name}")
                traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
