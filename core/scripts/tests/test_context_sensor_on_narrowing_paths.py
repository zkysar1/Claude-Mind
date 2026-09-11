"""The context sensor must reach the scope-narrowing decision points, FROM A RUNNER ().

The defect these tests pin is not "the sensor is missing" — it existed, and five
.md files already instructed the LLM to run it. The defect is WHO EXECUTED it.
Measured on cc-10, 2026-09-10, before this change:

  * `core/scripts/context-budget-banner.sh` had ZERO callers in any .py or .sh
    file in the tree. Every reference was prose inside a .md, plus two audit
    scripts that parse the banner's SHAPE out of journal text without ever
    invoking it.
  * Exactly 1 of alpha's 163 journal entries contained a `CTX:` line at all,
    against 6 abbreviation claims that cite context/tight as their condition.

Meanwhile the harness injects `<total_tokens>N tokens left</total_tokens>` into
EVERY turn, and it can read 0 while the sensor reads zone=normal with 200,000
tokens of headroom (measured 2026-09-09, alpha, cc-04, SID ed7229e3 —
guard-6380). A misleading signal on every turn cannot be beaten by a correct
signal that depends on someone remembering to look, so the fix is two runner
call sites, and these tests exist to stop either one silently reverting to prose.

guard-6255 is why the sensor is a valid falsifier at all: context-budget.json is
written by the statusLine hook, an INDEPENDENT writer from the harness marker.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


oeb = _load("orchestrator_entry_battery", "orchestrator-entry-battery.py")
aoa = _load("abbreviated_obligation_audit", "abbreviated-obligation-audit.py")

# Taken VERBATIM from a live run on cc-10, 2026-09-10T19:37:28. Kept as a literal
# rather than regenerated, so a change to the banner's wording fails HERE with a
# readable diff instead of silently invalidating every stored citation.
LIVE_BANNER = (
    "CTX: raw 31% | of-autocompact 65% | zone normal | to-compact 170,000 tokens "
    "| env 600000/80 | updated 2026-09-10T19:37:28"
)


@pytest.fixture()
def fake_state_dir(tmp_path, monkeypatch):
    state = tmp_path / "agents" / "testagent" / "session"
    state.mkdir(parents=True)
    import _paths

    monkeypatch.setattr(_paths, "agent_state_dir", lambda name: state)
    return state


def _empty_wm(tmp_path):
    p = tmp_path / "wm.yaml"
    p.write_text("slots: {}\n", encoding="utf-8")
    return str(p)


# ---------------------------------------------------------------- battery ----


def test_entry_battery_emits_the_sensor_in_json_mode(fake_state_dir, tmp_path, capsys):
    oeb.run("testagent", _empty_wm(tmp_path), True)
    rep = json.loads(capsys.readouterr().out.splitlines()[0])
    assert rep["context_line"].startswith("CTX:"), rep.get("context_line")


def test_entry_battery_emits_the_sensor_in_text_mode(fake_state_dir, tmp_path, capsys):
    oeb.run("testagent", _empty_wm(tmp_path), False)
    out = capsys.readouterr().out
    assert any(ln.startswith("CTX:") for ln in out.splitlines()), out


def test_entry_battery_emits_the_sensor_even_when_it_fails_open(tmp_path, capsys, monkeypatch):
    """guard-614: structured output on EVERY exit path.

    _fail_open() routes through _emit(), so an errored battery must still carry
    the sensor. This is the path a post-compaction re-entry is most likely to
    take, which is exactly when the agent is most likely to believe the marker.
    """
    monkeypatch.delenv("MIND_AGENT", raising=False)
    oeb.run(None, None, True)
    rep = json.loads(capsys.readouterr().out.splitlines()[0])
    assert rep.get("error"), "precondition: no agent binding is an error condition"
    assert rep["context_line"].startswith("CTX:"), rep.get("context_line")


def test_the_battery_line_is_a_valid_citation_under_the_stricter_audit():
    """Cross-file contract, and the reason it is worth its own test.

    TWO audits parse this banner and they DISAGREE: BANNER_RE in
    abbreviated-obligation-audit.py anchors `^CTX:` and calls .match() per line,
    while banner_re in context-citation-audit.sh is unanchored and calls
    .search(). So a line that is merely PRESENT can still be an invalid citation
    under the stricter of the two. The battery therefore prints the banner
    unprefixed and on its own line; prefixing it would satisfy one audit and
    silently fail the other.
    """
    assert aoa.BANNER_RE.match(LIVE_BANNER), LIVE_BANNER
    assert aoa.BANNER_RE.match(LIVE_BANNER).group(1) == "normal"
    assert not aoa.BANNER_RE.match("[entry-battery] " + LIVE_BANNER), (
        "a prefixed banner must NOT match — that is why the emitters print it bare"
    )


def test_battery_passthrough_does_not_reformat_the_banner(monkeypatch):
    """The battery must forward whatever the banner said, byte for byte.

    guard-2676 (no transcription): re-deriving the line from context-budget.json
    here would be a second formatter that drifts from both audit regexes without
    anything failing.
    """
    sentinel = "CTX: raw 7% | of-autocompact 9% | zone fresh | to-compact 1 tokens | x | y"

    class _P:
        stdout = sentinel + "\n"

    monkeypatch.setattr(oeb.subprocess, "run", lambda *a, **k: _P())
    assert oeb._context_line() == sentinel


def test_battery_context_line_never_raises(monkeypatch):
    """Fail-open by contract: the battery must never block the loop."""

    def _boom(*a, **k):
        raise OSError("banner exploded")

    monkeypatch.setattr(oeb.subprocess, "run", _boom)
    line = oeb._context_line()
    assert line.startswith("CTX: unavailable"), line


# ---------------------------------------------------------------- release ----

RELEASE_SH = (SCRIPTS / "aspirations-release.sh").read_text(encoding="utf-8")


def test_release_calls_the_banner_BEFORE_the_release_fires():
    """Ordering is the whole value: a sensor printed after the goal is already
    back on the queue informs nothing about the decision that just happened."""
    call = RELEASE_SH.index("context-budget-banner.sh")
    fire = RELEASE_SH.index('rt_call POST /v1/aspirations/release')
    assert call < fire, "the banner call must precede the first release POST"


def test_release_prints_the_banner_unprefixed():
    """Same anchor property as the battery — see the stricter-audit test above."""
    assert "printf '%s\\n' \"$_ctx_line\" >&2" in RELEASE_SH, (
        "the banner line must be printed bare so BANNER_RE's ^CTX: anchor can match"
    )


def test_release_banner_goes_to_stderr_not_stdout():
    """stdout carries the goal JSON and has parsers."""
    idx = RELEASE_SH.index("_ctx_line")
    window = RELEASE_SH[idx : idx + 900]
    for line in window.splitlines():
        if "printf" in line and "_ctx_line" in line:
            assert ">&2" in line, line


# ------------------------------------------------------------- population ----


def _invocation_lines(path: Path, needle: str) -> list[str]:
    """Lines that INVOKE `needle`, with prose excluded.

    WHY THIS IS NOT A SUBSTRING SCAN. The first version skipped lines whose
    first non-space character was `#` — correct for shell, WRONG for Python: a
    module docstring is a STRING, not a comment, and both call sites in this
    repo carry a docstring paragraph naming the banner. Mutation-proved during
    the g-115-9588 verify: with BOTH real call sites replaced by a nonexistent
    script name, the test still PASSED, matching only the prose that explains
    the call. A regression guard that cannot fail is worse than none — it
    reports all-clear forever (guard-5501).

    WHY NOT A TOKENIZER EITHER. Dropping every STRING token was the obvious
    second attempt and it is also wrong, in the opposite direction: a script
    path handed to subprocess IS a string literal, so that predicate discards
    the genuine calls too. Its positive control failed immediately, which is the
    only reason the over-correction did not ship (guard-2421).

    THE ACTUAL DISCRIMINATOR is what the string DOES, and the AST has it: prose
    is a bare string-expression STATEMENT (`ast.Expr` wrapping a str constant —
    every docstring, and any string used as a comment), while a call passes its
    string as an OPERAND. `needle` contains hyphens, so in Python it can only
    ever live inside a string literal or a `#` comment; the AST never sees
    comments, and this drops the prose statements, leaving exactly the operands.

    Shell needs none of this: its mentions are ordinary `#` comments, and the
    same mutation showed the line-based skip handling them correctly.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if path.suffix != ".py":
        return [
            ln.strip()
            for ln in text.splitlines()
            if needle in ln and not ln.lstrip().startswith("#")
        ]
    import ast

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []  # unparseable file contributes nothing rather than a false hit
    prose_lines: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            prose_lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    lines = text.splitlines()
    hits = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if needle not in node.value or node.lineno in prose_lines:
            continue
        hits.append(lines[node.lineno - 1].strip())
    return hits


def test_the_banner_has_at_least_one_runner_caller():
    """The regression that started this goal, stated as a population check.

    rb-9476: a scoped fix can be present, correct-looking and INERT — so assert
    the SELECTOR covers the population, not merely that a rule exists. Before
    g-115-9588 every reference to the banner lived in a .md (executed by a
    reader) or in an audit that only parses its output shape.

    PRECISION, because the obvious stronger claim is false and was retracted from
    that goal's outcome_note: run the CORRECTED predicate over the pre-change
    tree and it returns 0, so this assertion would fail there. Run the predicate
    as FIRST WRITTEN (skip `#`-leading lines only) over that same tree and it
    returns 1 — abbreviated-obligation-audit.py's DOCSTRING — so the guard as
    originally shipped would have passed on the broken tree. The guard is only
    as good as `_invocation_lines`; that is why the helper carries its own
    positive and negative cases below rather than being trusted from here.
    """
    roots = [REPO / "core" / "scripts", REPO / ".claude", REPO / "mind_api"]
    callers = []
    for root in roots:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if p.suffix not in (".py", ".sh") or not p.is_file():
                continue
            if p.name.startswith("context-budget-banner"):
                continue  # the script itself
            if "tests" in p.parts:
                continue  # a test invoking it is not production wiring
            for line in _invocation_lines(p, "context-budget-banner"):
                callers.append(f"{p.relative_to(REPO)}: {line[:100]}")
    assert callers, (
        "context-budget-banner.sh has NO runner caller — every consultation is "
        "back to being LLM prose, which is the exact g-115-9588 defect"
    )


def test_both_narrowing_paths_still_invoke_the_banner():
    """Per-PATH guard, and the one that actually protects this fix.

    The population test above asks "does ANY runner call the banner", which is
    the right question for the original defect but cannot go red when one of two
    call sites is removed — correctly so, since the other still satisfies it.
    That makes it un-mutation-provable with a single-target harness, and it would
    stay green while HALF the fix silently reverted.

    This asserts per path instead. It is deliberately NOT an exact-set
    assertion: a third caller added later is fine, a missing one is not.
    """
    for rel in ("core/scripts/aspirations-release.sh",
                "core/scripts/orchestrator-entry-battery.py"):
        found = _invocation_lines(REPO / rel, "context-budget-banner")
        assert found, f"{rel} no longer invokes context-budget-banner.sh"


def test_a_prose_only_mention_does_not_count_as_a_caller(tmp_path):
    """The mutation this test file failed, pinned as a case.

    A module whose DOCSTRING names the banner, with no call anywhere, must
    contribute zero invocation lines. Without this, the population check above
    is unfalsifiable — which is exactly what was measured before the fix.
    """
    prose_only = tmp_path / "prose_only.py"
    prose_only.write_text(
        '"""Some module.\n\n    Run context-budget-banner.sh before deciding.\n    """\n'
        "# also context-budget-banner.sh in a comment\n"
        "VALUE = 1\n",
        encoding="utf-8",
    )
    assert _invocation_lines(prose_only, "context-budget-banner") == []

    # POSITIVE CONTROL, in BOTH shapes the live call sites use — a bare argument
    # and a Path operand. Without this half the predicate could pass by matching
    # nothing at all, which is the over-correction that the tokenizer attempt hit.
    real_call = tmp_path / "real_call.py"
    real_call.write_text(
        "import subprocess\n"
        '"""Docstring mentioning context-budget-banner.sh, which must NOT count."""\n'
        'subprocess.run(["x", "core/scripts/context-budget-banner.sh"])\n'
        'other(SCRIPT_DIR / "context-budget-banner.sh")\n',
        encoding="utf-8",
    )
    found = _invocation_lines(real_call, "context-budget-banner")
    assert len(found) == 2, (
        f"positive control: both genuine calls MUST be detected, got {found!r}"
    )
    assert not any("Docstring" in ln for ln in found), found
