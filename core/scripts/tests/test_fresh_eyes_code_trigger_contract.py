"""Contract pin: fresh-eyes-code's documented tag shape must be routable by
insight-trigger-gate.py's own filter.

g-115-5265. The two ends drifted silently for as long as both existed:
`.claude/skills/fresh-eyes-code/SKILL.md` Phase 5 named
`insight-trigger-gate.py` as its consumer while emitting no
`requires_action_by:` tag, and the gate skips exactly that case by documented
policy. Measured 2026-08-07 (alpha, hostname cc-04, uname -r
6.8.0-136-generic) over the live findings channel: 1139 fresh-eyes-code posts,
496 carrying invalidates|constrains, 15 carrying requires_action_by: — so 481
actionable findings were dropped in silence. Nothing errored, because a
finding that routes nowhere is indistinguishable from a finding with no
affected work.

Design notes:

* The tag shape is read out of SKILL.md's EMISSION form (the `--tags "..."`
  argument), not from prose describing it (guard-2224). Prose can claim a tag
  the skill never emits; the emission line is what actually ships.
* `test_prefix_shape_is_dropped` is the POSITIVE CONTROL. It asserts the
  PRE-FIX tag shape is refused by the gate. Without it this pin could pass on
  a gate that admits everything, which is the failure mode guard-2224 and
  guard-385 both name: a guard that returns "clean" on a broken input proves
  nothing.
"""
import importlib.util
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SKILL = REPO / ".claude" / "skills" / "fresh-eyes-code" / "SKILL.md"
GATE = REPO / "core" / "scripts" / "insight-trigger-gate.py"
SWEEP = REPO / "core" / "scripts" / "insight-trigger-sweep.py"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gate():
    return _load(GATE, "insight_trigger_gate")


@pytest.fixture(scope="module")
def sweep():
    return _load(SWEEP, "insight_trigger_sweep")


def _emitted_tag_shape():
    """The severity-routed --tags argument, straight out of Phase 5's emission form."""
    text = SKILL.read_text(encoding="utf-8")
    matches = re.findall(r'--tags "(insight_trigger[^"]*)"', text)
    assert matches, (
        "no insight_trigger --tags emission line found in fresh-eyes-code SKILL.md; "
        "Phase 5 was restructured — re-point this pin at the new emission form "
        "rather than deleting it"
    )
    assert len(matches) == 1, f"expected exactly one emission form, found {len(matches)}: {matches}"
    return matches[0]


def _render(shape, *, addressed):
    """Substitute Phase 5's placeholders into a concrete tag list."""
    route = "requires_action_by:echo," if addressed else ""
    rendered = (
        shape.replace("{route_tag}", route)
        .replace("{finding.severity}", "constrains")
        .replace("{finding.file}", "app/account/(authed)/agents/AgentManager.tsx")
        .replace("{source_tag}", "g-248-07")
    )
    assert "{" not in rendered, f"unsubstituted placeholder remains: {rendered}"
    return rendered.split(",")


def _finding(tags, msg_id):
    return {
        "id": msg_id,
        "author": "alpha",
        "tags": tags,
        "text": "synthetic finding body",
        "timestamp": "2026-08-07T21:00:00",
    }


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, gate):
    """Keep the pin off live dedup state — it asserts the TAG contract only."""
    monkeypatch.setattr(gate, "_already_processed", lambda _id: False)
    monkeypatch.setattr(gate, "_already_filed_in_aspirations", lambda _id: False)


def test_emission_form_carries_the_route_tag_placeholder():
    """The tag shape must be able to carry requires_action_by: at all."""
    shape = _emitted_tag_shape()
    assert "{route_tag}" in shape, (
        "fresh-eyes-code Phase 5 emits no requires_action_by: slot. "
        "insight-trigger-gate._collect_triggers skips triggers whose "
        "requires_action_by is absent, so every invalidates/constrains finding "
        "this skill publishes would route to nobody (g-115-5265)."
    )


def test_addressed_shape_routes_through_the_gate(gate):
    """The documented shape, addressed to a partner, must survive the gate."""
    tags = _render(_emitted_tag_shape(), addressed=True)
    collected = gate._collect_triggers([_finding(tags, "pin-addressed-1")], "echo")
    assert len(collected) == 1, (
        f"gate refused fresh-eyes-code's own documented tag shape: {tags}"
    )
    assert collected[0]["severity"] == "constrains"
    # The file-path affects: value survives the gate's generic prefix parser.
    # It was NOT the cause of the routing failure — recorded here so the
    # original (wrong) diagnosis cannot be re-derived from this file.
    assert collected[0]["affects"] == ["app/account/(authed)/agents/AgentManager.tsx"]


def test_prefix_shape_is_dropped(gate):
    """POSITIVE CONTROL — the pre-fix shape must still be refused.

    If this ever passes, the gate stopped requiring requires_action_by: and the
    pin above became vacuous.
    """
    tags = _render(_emitted_tag_shape(), addressed=False)
    assert not any(t.startswith("requires_action_by:") for t in tags)
    collected = gate._collect_triggers([_finding(tags, "pin-unaddressed-1")], "echo")
    assert collected == [], (
        "the unaddressed shape now routes — the gate's requires_action_by policy "
        "changed, so test_addressed_shape_routes_through_the_gate no longer "
        "proves anything. Re-derive the contract before deleting this control."
    )


def test_gate_skips_self_authored_findings(gate):
    """Why alpha/gamma paths are deliberately unaddressed: self-triggers never route."""
    tags = _render(_emitted_tag_shape(), addressed=True)
    collected = gate._collect_triggers([_finding(tags, "pin-self-1")], "alpha")
    assert collected == [], "gate admitted a self-authored trigger"


def test_sweep_deliberately_does_not_admit_these(sweep):
    """The separation half of the contract, pinned so it reads as intent.

    fresh-eyes-code emits no `action_type:`, so insight-trigger-sweep never
    admits its posts. That is by design — the sweep is for explicitly-addressed
    agent-to-agent routing. Widening the sweep's AFFECTS_RE to accept file
    paths would not change this by one post.
    """
    from datetime import datetime

    tags = _render(_emitted_tag_shape(), addressed=True)
    now = datetime.now()
    parsed = sweep._parse_trigger_msg(_finding(tags, "pin-sweep-1"), "findings", now, now)
    assert parsed is None, (
        "the sweep now admits fresh-eyes-code posts; the two-consumer split "
        "documented in SKILL.md Phase 5 is stale"
    )


def test_affects_re_rejects_file_paths_by_design(sweep):
    """Records the secondary, non-blocking asymmetry so it is not re-filed as the cause."""
    assert sweep.AFFECTS_RE.match("affects:g-335-1000")
    assert not sweep.AFFECTS_RE.match("affects:app/account/(authed)/agents/AgentManager.tsx")
