"""Tests for the enforcement-triad scaffold generator (gap-035, ).

Every assertion that a string is ABSENT carries a positive control proving the
same probe FINDS it when it is there -- an absence test whose probe cannot
detect presence passes forever (guard-2298 / guard-1866).
"""

import ast
import json
import pathlib
import sys

import pytest

SCRIPT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import forge_enforcement_triad as F  # noqa: E402

SPEC = {"name": "widget-timeout", "tool": "Bash",
        "purpose": "Refuse calls that omit an explicit timeout.",
        "goal_id": "g-000-00"}


def test_render_emits_the_whole_shape_and_nothing_else():
    """A triad is six files. The failure mode this pins is a MISSING piece --
    a triad shipped without its Layer-C detective looks complete and is not."""
    files = F.render(SPEC)
    assert sorted(files) == sorted([
        ".claude/rules/widget-timeout-pattern.md",
        "core/scripts/_widget_timeout_predicate.py",
        "core/scripts/tests/test_widget_timeout_gate.py",
        "core/scripts/widget-timeout-audit.py",
        "core/scripts/widget-timeout-gate.py",
        "core/scripts/widget-timeout-gate.sh",
    ])


def test_every_emitted_python_file_parses():
    """The one defect that would make this generator worse than useless."""
    for path, content in F.render(SPEC).items():
        if path.endswith(".py"):
            ast.parse(content)  # raises SyntaxError on failure


def test_no_unsubstituted_placeholder_survives_anywhere():
    """POSITIVE CONTROL included: the probe must be able to SEE a brace."""
    rendered = F.render(SPEC)
    for path, content in rendered.items():
        assert "{name}" not in content, path
        assert "{slug}" not in content, path
    # control: the same substring probe finds a real one in a raw template
    assert "{name}" in "a {name} b", "probe cannot detect the pattern it asserts absent"


def test_slug_and_override_token_derivation():
    assert F.slug_of("widget-timeout") == "widget_timeout"
    assert F.override_token_of("widget-timeout") == "WIDGET_TIMEOUT_OVERRIDE"


def test_render_is_deterministic():
    """Purity: same spec in, byte-identical output. If this fails the generator
    has acquired a clock, a cwd dependence or a dict-ordering leak, and every
    other test here becomes untrustworthy."""
    assert F.render(SPEC) == F.render(dict(SPEC))


@pytest.mark.parametrize("bad,why", [
    ("Widget-Timeout", "uppercase"),
    ("widget_timeout", "snake_case"),
    ("widget timeout", "space"),
    ("widget-timeout-gate", "already carries the -gate suffix"),
])
def test_validate_refuses_names_that_cannot_work(bad, why):
    spec = dict(SPEC, name=bad)
    with pytest.raises(F.SpecError):
        F.render(spec)


def test_validate_accepts_the_good_name_that_the_bad_ones_are_variants_of():
    """POSITIVE CONTROL for the parametrised refusals above: without this, a
    validate() that refused EVERYTHING would pass all four."""
    F.render(dict(SPEC, name="widget-timeout"))


@pytest.mark.parametrize("missing", ["name", "tool", "purpose"])
def test_validate_requires_each_mandatory_key(missing):
    spec = {k: v for k, v in SPEC.items() if k != missing}
    with pytest.raises(F.SpecError):
        F.render(spec)


def test_unknown_tool_is_refused_rather_than_silently_producing_a_dead_gate():
    with pytest.raises(F.SpecError):
        F.render(dict(SPEC, tool="NoSuchTool"))


@pytest.mark.parametrize("tool,field", [
    ("Bash", "command"), ("Write", "content"),
    ("Edit", "new_string"), ("ScheduleWakeup", "prompt"),
])
def test_gate_reads_the_payload_field_that_matches_its_tool(tool, field):
    """A gate that reads the wrong field approves everything forever while
    looking wired -- the exact silent-inertness this shape's Layer C exists for."""
    gate = F.render(dict(SPEC, tool=tool))["core/scripts/widget-timeout-gate.py"]
    assert f'.get("{field}")' in gate
    assert f'!= "{tool}"' in gate


def test_predicate_stub_raises_rather_than_returning_none():
    """LOAD-BEARING. A stub that returned None would emit a gate that approves
    every payload while looking implemented -- indistinguishable from a clean
    corpus. It must fail loudly instead."""
    src = F.render(SPEC)["core/scripts/_widget_timeout_predicate.py"]
    assert "raise NotImplementedError" in src
    tree = ast.parse(src)
    decide = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "decide")
    returns = [n for n in ast.walk(decide) if isinstance(n, ast.Return)]
    assert returns == [], "the stub must not return; it must raise"


def test_wrapper_carries_the_hook_fire_sentinel_at_every_site():
    """The sentinel name appears in a comment AND inside a single bash line;
    the two drifting apart is why this is generated rather than hand-edited."""
    sh = F.render(SPEC)["core/scripts/widget-timeout-gate.sh"]
    assert sh.count("core/logs/hook-fires/widget-timeout-gate") == 2
    assert "widget-timeout-gate.py" in sh
    assert sh.rstrip().endswith("exit 0"), (
        "the wrapper's unconditional trailing `exit 0` IS the fail-open "
        "guarantee -- the Python's try/except cannot cover an import-time error")


def test_settings_entry_is_valid_json_naming_the_wrapper():
    entry = json.loads(F.settings_hook_entry(SPEC))
    assert entry["matcher"] == "Bash"
    cmd = entry["hooks"][0]["command"]
    assert cmd.endswith("core/scripts/widget-timeout-gate.sh")
    assert "$CLAUDE_PROJECT_DIR" in cmd


def test_generator_performs_no_file_io():
    """Purity guard by inspection: the module must not import os/pathlib for
    writing, and must contain no open()/write call. The .sh does the writing."""
    src = (SCRIPT_DIR / "forge_enforcement_triad.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = [n.func.id for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert "open" not in calls
    # control: the probe finds `open` when it is present
    assert "open" in [n.func.id for n in ast.walk(ast.parse("open('x')"))
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]


def test_audit_imports_the_same_predicate_the_gate_uses():
    """Layer A and Layer C disagreeing is the defect the shared module prevents."""
    r = F.render(SPEC)
    imp = "from _widget_timeout_predicate import"
    assert imp in r["core/scripts/widget-timeout-audit.py"]
    assert imp in r["core/scripts/widget-timeout-gate.py"]


def test_substitution_sites_constant_covers_every_emitted_file_kind():
    """Parity pin: if a future edit adds a template without registering its
    sites, this fails rather than drifting silently."""
    kinds = {"gate.sh", "gate.py", "predicate.py", "audit.py", "rule.md"}
    assert kinds == set(F.SUBSTITUTION_SITES)
