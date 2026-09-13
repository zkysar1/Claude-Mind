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
def _post(pid, typ, author, reply_to=None, tags=None, text="t"):
    return {"id": pid, "type": typ, "author": author, "timestamp": "2026-09-02T10:00:00",
            "text": text, "reply_to": reply_to, "tags": list(tags or [])}


# Demand is ADDRESSED demand (): the routed tag is what makes a post
# this caller's business. Every pre-existing test below therefore routes its
# demand post explicitly; what each one pins (types, replies, widening, CLI)
# is unchanged.
TO_ALPHA = ["requires_action_by:alpha"]


def test_unconsumed_actionable_demand_defers_generation(tmp_path):
    cal = G.load_calendar(_write(tmp_path, FIXTURE))
    posts = [_post("p1", "directive", "bravo@env", tags=TO_ALPHA), _post("p2", "finding", "bravo@env", tags=TO_ALPHA)]
    got = G.demand_check(posts, "alpha@env", cal)
    assert got["decision"] == "defer"
    assert got["unconsumed_count"] == 1, "only the directive is demand; a finding is traffic even when routed"


def test_answered_demand_does_not_defer(tmp_path):
    cal = G.load_calendar(_write(tmp_path, FIXTURE))
    posts = [_post("p1", "directive", "bravo@env", tags=TO_ALPHA), _post("r1", "status", "alpha@env", reply_to="p1")]
    assert G.demand_check(posts, "alpha@env", cal)["decision"] == "allow"


def test_a_peers_reply_does_not_consume_my_demand(tmp_path):
    cal = G.load_calendar(_write(tmp_path, FIXTURE))
    posts = [_post("p1", "directive", "bravo@env", tags=TO_ALPHA), _post("r1", "status", "zeta@env", reply_to="p1")]
    assert G.demand_check(posts, "alpha@env", cal)["decision"] == "defer"


def test_own_posts_are_not_demand_on_self(tmp_path):
    cal = G.load_calendar(_write(tmp_path, FIXTURE))
    assert G.demand_check([_post("p1", "directive", "alpha@env", tags=TO_ALPHA)], "alpha@env", cal)["decision"] == "allow"


def test_agent_matches_across_env_suffixes(tmp_path):
    cal = G.load_calendar(_write(tmp_path, FIXTURE))
    posts = [_post("p1", "directive", "alpha@other-world", tags=["requires_action_by:alpha@other-world"])]
    assert G.demand_check(posts, "alpha", cal)["decision"] == "allow"


def test_default_actionable_set_has_no_severity_dependency():
    """guard-159: the live board carries no `severity` field on any post."""
    posts = [_post("p1", "escalation", "bravo@env", tags=TO_ALPHA)]
    got = G.demand_check(posts, "alpha@env", None)
    assert got["decision"] == "defer"
    assert "severity" not in json.dumps(got)


def test_domain_may_widen_the_actionable_set(tmp_path):
    cal = G.parse_calendar(FIXTURE.replace("[directive, escalation, question]", "[finding]"))
    assert G.demand_check([_post("p1", "finding", "b@e", tags=TO_ALPHA)], "alpha@e", cal)["decision"] == "defer"
    assert G.demand_check([_post("p1", "directive", "b@e", tags=TO_ALPHA)], "alpha@e", cal)["decision"] == "allow"


def test_cli_demand_check_reads_board_jsonl(tmp_path):
    cal = _write(tmp_path, FIXTURE)
    feed = tmp_path / "posts.jsonl"
    feed.write_text("\n".join(json.dumps(p) for p in
                              [_post("p1", "directive", "bravo@env", tags=TO_ALPHA), _post("p2", "claim", "bravo@env", tags=TO_ALPHA)]),
                    encoding="utf-8")
    r = _run("demand-check", "--calendar", str(cal), "--posts-file", str(feed),
             "--author", "alpha@env", "--json")
    assert r.returncode == 1, r.stdout + r.stderr
    assert json.loads(r.stdout)["unconsumed_count"] == 1


# ---- : demand is ADDRESSED demand, not every actionable post -------
def _flood(n, author="alpha@env", typ="escalation"):
    """The measured 2026-09-11 shape: a reducer's own worker Bodies escalating merge wedges at it."""
    return [_post(f"f{i}", typ, author, text="MERGE WEDGE on my box -- reducer, please look") for i in range(n)]


def test_unaddressed_actionable_posts_are_not_my_demand(tmp_path):
    cal = G.load_calendar(_write(tmp_path, FIXTURE))
    got = G.demand_check(_flood(88), "bravo@env", cal)
    assert got["decision"] == "allow", got["reason"]
    assert got["actionable_total"] == 88, "the pre-narrowing count stays auditable"
    assert got["unconsumed_count"] == 0 and got["addressed_count"] == 0


def test_one_addressed_unreplied_post_defers_amid_a_flood(tmp_path):
    cal = G.load_calendar(_write(tmp_path, FIXTURE))
    posts = _flood(88) + [_post("a1", "question", "echo@env", tags=["requires_action_by:bravo"])]
    got = G.demand_check(posts, "bravo@env", cal)
    assert got["decision"] == "defer"
    assert got["unconsumed_count"] == 1 and got["actionable_total"] == 89
    assert got["outstanding"][0]["id"] == "a1"
    assert got["outstanding"][0]["addressed_by"] == "requires_action_by"


def test_requires_action_by_tag_matches_across_env_suffix(tmp_path):
    cal = G.load_calendar(_write(tmp_path, FIXTURE))
    posts = [_post("a1", "question", "echo@env", tags=["requires_action_by:bravo@ayoai-mind"])]
    assert G.demand_check(posts, "bravo", cal)["decision"] == "defer"


def test_bare_agent_tag_routes_but_goal_and_target_tags_do_not(tmp_path):
    """board.py: 'a bare <who> also routes'; `target:`/goal-id tags never address anyone."""
    cal = G.load_calendar(_write(tmp_path, FIXTURE))
    assert G.demand_check([_post("a1", "question", "zeta@env", tags=["bravo"])], "bravo@env", cal)["decision"] == "defer"
    got = G.demand_check([_post("a2", "question", "zeta@env", tags=["target:g-1-1", "g-369-08", "forward-to:bravo"])],
                         "bravo@env", cal)
    assert got["decision"] == "allow" and got["addressed_count"] == 0


def test_reply_to_my_post_is_addressed_to_me(tmp_path):
    cal = G.load_calendar(_write(tmp_path, FIXTURE))
    posts = [_post("m1", "status", "bravo@env"), _post("q1", "question", "echo@env", reply_to="m1")]
    got = G.demand_check(posts, "bravo@env", cal)
    assert got["decision"] == "defer" and got["outstanding"][0]["addressed_by"] == "reply-to-mine"
    posts.append(_post("r1", "status", "bravo@env", reply_to="q1"))
    assert G.demand_check(posts, "bravo@env", cal)["decision"] == "allow"


def test_first_line_address_counts_but_a_mention_does_not(tmp_path):
    cal = G.load_calendar(_write(tmp_path, FIXTURE))
    def verdict(text):
        got = G.demand_check([_post("a", "question", "zeta@env", text=text)], "bravo@env", cal)
        return got["decision"], (got["outstanding"][0]["addressed_by"] if got["outstanding"] else None)
    # vocatives and routing phrases at the head of the first line ADDRESS me
    assert verdict("bravo \u2014 standing off g-369-08, read this before you pick it") == ("defer", "first-line-address")
    assert verdict("bravo: review request on g-364-150") == ("defer", "first-line-address")
    assert verdict("URGENT, bravo \u2014 you claimed g-369-195 and the duplication gate fired") == ("defer", "first-line-address")
    assert verdict("@bravo: one thing") == ("defer", "first-line-address")
    assert verdict("RE-REVIEW REQUEST \u2014 g-357-41, addressed to bravo (the one eligible reviewer)") == ("defer", "first-line-address")
    assert verdict("PR REVIEW REQUEST -> bravo (guard-5226: resolved among agents)") == ("defer", "first-line-address")
    # mentions are NOT addresses (both measured on the 2026-09-11 export)
    assert verdict("RELAY (worked example msg-20260827-110602-bravo-6570 / g-364-104) \u2014 register ONE") == ("allow", None)
    assert verdict("OPEN INVITATION \u2014 sampler pass needed from a Body that is NOT alpha and NOT bravo") == ("allow", None)
    assert verdict("rally status\nbravo \u2014 this second line does not count") == ("allow", None)
    assert verdict("per bravo@ayoai-mind's post the rally is fine") == ("allow", None)
    assert verdict("the bravos and abravo") == ("allow", None)


def test_fleet_wide_directive_without_a_routing_tag_is_not_my_demand(tmp_path):
    """Untagged directives are consumed by aspirations-select's directive-honor path, never by replies."""
    cal = G.load_calendar(_write(tmp_path, FIXTURE))
    got = G.demand_check([_post("d1", "directive", "alpha@env", text="PRODUCT FOCUS renewed, fleet-wide")], "bravo@env", cal)
    assert got["decision"] == "allow" and got["actionable_total"] == 1 and got["addressed_count"] == 0


def test_no_caller_identity_fails_open(tmp_path):
    cal = G.load_calendar(_write(tmp_path, FIXTURE))
    posts = _flood(3) + [_post("a1", "question", "echo@env", tags=["requires_action_by:bravo"])]
    got = G.demand_check(posts, "", cal)
    assert got["decision"] == "allow" and "fail-open" in got["reason"]


def test_cli_replay_of_the_board_export_shape(tmp_path):
    """Keys of the live board-read --json export (2026-09-11): author channel id reply_to session_id tags text timestamp type."""
    cal = _write(tmp_path, FIXTURE)

    def row(i, typ, author, tags, text):
        return {"author": author, "channel": "coordination", "id": f"msg-{i}", "reply_to": None, "session_id": "s",
                "tags": tags, "text": text, "timestamp": "2026-09-11T10:00:00", "type": typ}

    rows = [row(i, "escalation", "alpha@ayoai-mind", ["merge-wedge", "alpha"], "MERGE WEDGE on DESKTOP") for i in range(5)]
    rows.append(row(9, "finding", "echo@ayoai-mind", ["requires_action_by:bravo", "echo"], "STARVED RECURRING SENSOR, routed to you"))
    rows.append(row(10, "question", "echo@ayoai-mind", ["requires_action_by:bravo", "echo"], "please re-run the census"))
    feed = tmp_path / "export.jsonl"
    feed.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    r = _run("demand-check", "--calendar", str(cal), "--posts-file", str(feed), "--author", "bravo", "--json")
    got = json.loads(r.stdout)
    assert r.returncode == 1 and got["unconsumed_count"] == 1, got
    assert got["actionable_total"] == 6 and got["addressed_reasons"] == {"requires_action_by": 1}


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
