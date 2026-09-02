"""`aspirations-release.sh --reason-kind` — the TYPED half of the release negative ().

THE DEFECT THIS CLOSES. g-115-8163 needs to know which released goals were
released because THIS BOX CANNOT RUN THEM. `--reason` (the prior half) captured
the negative as FREE PROSE, so the only way to recover the locus subset was to
classify text — and g-115-8163 has already falsified that approach twice (echo's
bare-hostname match returning 60.8% of the open queue; zeta's 79-vs-85 correction
showing two published regexes bracket rather than measure the true count).

MEASURED before building (alpha, cc-10, 2026-09-02) over the 52 live release
reason strings then in the world queue:
  - over-matching locus regex  -> 8 matches, 3 true locus  => 62.5% FALSE POSITIVE
  - under-matching locus regex -> 3 matches, and it MISSES both Studio-gated
    locus rows outright, because they name no host at all
  - positive control: the unambiguous "box-locality gate ... absent on cc-07" row
    matches the over-matching regex, so an empty result would have been a
    measurement rather than a wrong-instrument zero
  - the sharpest false positive is self-refuting: a row whose text reads "this
    box can still run this goal ... NOT FOR LOCUS" MATCHES the locus regex

So the fix is not a better regex. The RELEASING AGENT already knows the category;
asking it for a token removes the inference instead of relocating it.

HERMETIC BY CONSTRUCTION: no daemon, no network, no world writes. The refusal and
validation paths run the REAL wrapper (they fire before `_runtime.sh` is sourced),
and the query assertions exercise the wrapper's own QUERY-construction lines in
isolation — the same extract-and-exercise technique test_release_source_forwarding.py
uses, so this file cannot drift from the wrapper's real text.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
_WRAPPER = PROJECT_ROOT / "core" / "scripts" / "aspirations-release.sh"
_DAEMON = PROJECT_ROOT / "mind_api" / "src" / "endpoints" / "aspirations_write.py"
BASH = "bash"


def _run_wrapper(*args: str) -> tuple[int, str]:
    # .as_posix(), never str(Path) — bash silently strips a WindowsPath's
    # backslashes (guard-581).
    p = subprocess.run(
        [BASH, _WRAPPER.as_posix(), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return p.returncode, (p.stdout + p.stderr)


def _wrapper_tokens() -> list[str]:
    """The shell vocabulary, read from the wrapper's own literal."""
    src = _WRAPPER.read_text(encoding="utf-8")
    m = re.search(r'^_REASON_KINDS="([^"]+)"', src, re.M)
    assert m, "could not locate _REASON_KINDS in aspirations-release.sh"
    return m.group(1).split()


def _daemon_tokens() -> list[str]:
    """The daemon vocabulary, read by AST rather than import.

    AST, not `import`: importing the endpoint module drags the whole daemon
    dependency graph into a test whose subject is a six-token literal.
    """
    tree = ast.parse(_DAEMON.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "RELEASE_REASON_KINDS"
            for t in node.targets
        ):
            value = node.value
            # `frozenset({...})` is a Call, not a literal, so literal_eval must
            # be given the SET INSIDE it. Handling the bare-set spelling too
            # keeps this pin working if the constant is ever relaxed.
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id in ("frozenset", "set")
                and value.args
            ):
                value = value.args[0]
            return sorted(ast.literal_eval(value))
    raise AssertionError("RELEASE_REASON_KINDS not found in aspirations_write.py")


# --------------------------------------------------------------------------
# 1. THE PARITY PIN. A shell constant and its daemon twin drift SILENTLY — the
#    wrapper keeps refusing a token the endpoint would have accepted, or ships
#    one the endpoint drops, and nothing errors either way (guard-742/547 class:
#    the daemon copy is the live one, so editing one side alone changes nothing
#    at runtime while looking entirely correct in the diff).
# --------------------------------------------------------------------------
def test_wrapper_and_daemon_token_sets_agree():
    assert sorted(_wrapper_tokens()) == _daemon_tokens()


def test_locus_and_capability_are_distinct_tokens():
    """Pins the design decision, not just the spelling.

    A credential barrier does NOT clear by moving boxes — the fleet shares its
    IAM principals (g-335-262-b) — so a consumer that merges `capability` into
    `locus` re-routes work to boxes that also cannot run it. Merging them would
    be the natural "simplification" for a later reader; this fails if they do.
    """
    tokens = _daemon_tokens()
    assert "locus" in tokens and "capability" in tokens


# --------------------------------------------------------------------------
# 2. THE QUERY. The property the change exists for, and the one a source-text
#    assertion would miss.
# --------------------------------------------------------------------------
def _build_query(goal_id: str, source_val: str, reason: str = "", kind: str = "") -> str:
    src = _WRAPPER.read_text(encoding="utf-8")
    m = re.search(r'^(QUERY="id=.*?)^rc=0', src, re.S | re.M)
    assert m, "could not locate the QUERY construction block in aspirations-release.sh"
    block = m.group(1)
    assert "&reason_kind=" in block, block
    script = (
        "rt_url_encode() { printf '%s' \"$1\"; }\n"
        f'GOAL_ID="{goal_id}"\n'
        f'SOURCE_VAL="{source_val}"\n'
        'MIND_SID=""\n'
        f'REASON_VAL="{reason}"\n'
        f'REASON_KIND_VAL="{kind}"\n'
        f"{block}\n"
        'printf "%s" "$QUERY"\n'
    )
    p = subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return p.stdout


def test_reason_kind_reaches_the_query():
    q = _build_query("g-115-8163", "world", reason="cannot run here", kind="locus")
    assert "reason_kind=locus" in q, q


def test_query_is_byte_identical_when_kind_is_absent():
    """Backward compatibility is the entire safety argument for a fleet-wide write path."""
    assert _build_query("g-115-1", "world") == "id=g-115-1&source=world"


def test_kind_is_omitted_when_only_reason_is_given():
    q = _build_query("g-115-1", "world", reason="partial advance")
    assert "reason_kind" not in q, q
    assert "reason=partial" in q, q


# --------------------------------------------------------------------------
# 3. REFUSALS. An unrecognised token must never reach the store — the whole
#    value of a typed field is that a consumer can trust the token.
# --------------------------------------------------------------------------
def test_invalid_kind_is_refused_and_names_the_vocabulary():
    rc, out = _run_wrapper("g-115-1", "--reason", "x", "--reason-kind", "locis")
    assert rc == 1, f"expected rc=1 for an invalid kind, got {rc}: {out}"
    assert "locus" in out, out


def test_kind_without_reason_is_refused():
    """A token with no evidence behind it is not a record."""
    rc, out = _run_wrapper("g-115-1", "--reason-kind", "locus")
    assert rc == 1, f"expected rc=1, got {rc}: {out}"
    assert "requires --reason" in out, out


def test_reason_kind_with_no_value_is_refused_with_exit_2():
    """ARITY GUARD (): rc=2 separates 'you invoked me wrong' from a
    transport failure. Under `set -e` a bare `shift 2` would exit 1 SILENTLY,
    which is byte-identical to the daemon being unreachable."""
    rc, out = _run_wrapper("g-115-1", "--reason-kind")
    assert rc == 2, f"expected rc=2, got {rc}: {out}"
    assert "--reason-kind" in out, out


@pytest.mark.parametrize(
    "kind", ["locus", "capability", "role", "not-due", "progress", "other"]
)
def test_every_vocabulary_token_is_accepted_by_the_validator(kind):
    """Guards the `case` pattern-match: ` $_REASON_KINDS ` with a hyphenated
    token is exactly where a glob-based membership test goes wrong."""
    q = _build_query("g-115-1", "world", reason="r", kind=kind)
    assert f"reason_kind={kind}" in q, q


def test_help_names_reason_kind():
    rc, out = _run_wrapper("--help")
    assert rc == 0, out
    assert "--reason-kind" in out, out


def test_unknown_flag_refusal_still_lists_reason_kind():
    """_ACCEPTED_FLAGS is ONE literal feeding both help and the refusal."""
    rc, out = _run_wrapper("g-115-1", "--bogus")
    assert rc == 2, out
    assert "--reason-kind" in out, out


# --------------------------------------------------------------------------
# 4. THE DAEMON'S OWN DEFENCE. The wrapper refuses bad tokens, but a direct API
#    caller bypasses it, so the endpoint must drop rather than store them.
# --------------------------------------------------------------------------
def test_daemon_drops_out_of_vocabulary_kind():
    src = _DAEMON.read_text(encoding="utf-8")
    assert (
        "entry_kind = reason_kind if reason_kind in RELEASE_REASON_KINDS else None"
        in src
    ), "the endpoint must filter through the vocabulary, not store the raw query value"


def test_absent_kind_is_stored_as_none_not_omitted():
    """Consumers MUST read absent as UNMEASURED (no penalty), never as a barrier
    — design caution (2) of g-115-8163. An explicit None keeps the key present
    so a consumer reading `.get("kind")` cannot confuse it with an older row."""
    src = _DAEMON.read_text(encoding="utf-8")
    assert '"kind": entry_kind,' in src


# --------------------------------------------------------------------------
# 5. THE ADOPTION NUDGE (, wiring pass). A capability nobody CALLS is
#    inert, and this goal's own history holds two instances of exactly that
#    (locus-sweep computed a signal nothing consumed; complexity_budget.py sat
#    with zero callers for two months). guard-399 fixes the shape: the bash
#    baseline comes first and prose is optional enrichment on top — so the
#    nudge lives in the ONE wrapper every call site funnels through, not in ten
#    prose copies (five of which are on the hot-path size budget).
#
#    Every test below runs with NO goal id, which keeps the file hermetic: the
#    advisory is textually ABOVE the `goal_id is required` check, so the wrapper
#    prints it and exits before `_runtime.sh` is ever sourced. That ordering is
#    load-bearing to this coverage and is pinned by its own test below — move
#    the advisory under the goal-id check and these tests would silently stop
#    exercising the production branch.
# --------------------------------------------------------------------------
def _run_split(*args: str) -> tuple[int, str, str]:
    """Like _run_wrapper but keeps the streams APART — the point of several
    assertions here is WHICH stream the text landed on."""
    p = subprocess.run(
        [BASH, _WRAPPER.as_posix(), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return p.returncode, p.stdout, p.stderr


_NUDGE = "--reason given WITHOUT --reason-kind"


def test_advisory_fires_when_a_reason_carries_no_kind():
    """The whole adoption mechanism: an untyped negative says so, at the moment
    the caller is writing it."""
    _, _, err = _run_split("--reason", "released for a locus reason")
    assert _NUDGE in err, err


def test_advisory_is_silent_when_the_kind_is_given():
    """POSITIVE CONTROL (guard-4166). A fix whose effect is that something
    APPEARS is only pinned if the control shows it NOT appearing — otherwise a
    nudge hard-coded to print always would pass the test above."""
    _, _, err = _run_split("--reason", "x", "--reason-kind", "progress")
    assert _NUDGE not in err, err


def test_advisory_is_silent_on_a_bare_release():
    """Deliberately scoped to 'a reason was written'. A bare release has no
    evidence to type, so nudging there would put noise on the highest-volume
    path and train readers to ignore the line."""
    _, _, err = _run_split()
    assert _NUDGE not in err, err


def test_advisory_goes_to_stderr_and_never_to_stdout():
    """Callers parse the JSON on stdout. An advisory that leaked there would
    corrupt every one of them — the guard-2410 output-channel class."""
    _, out, err = _run_split("--reason", "x")
    assert _NUDGE in err
    assert "Note:" not in out, out


def test_advisory_names_every_vocabulary_token():
    """Self-documenting by construction: it interpolates $_REASON_KINDS rather
    than restating the list, so a seventh token cannot appear in the vocabulary
    while the nudge keeps advertising six."""
    _, _, err = _run_split("--reason", "x")
    for token in _wrapper_tokens():
        assert token in err, f"{token!r} missing from the advisory: {err}"


def test_advisory_does_not_change_the_exit_code():
    """ADVISORY means advisory. Both invocations must fail identically on the
    missing goal id; a release that fails CLOSED would strand a claim, which is
    far worse than an untyped row."""
    rc_with, _, _ = _run_split("--reason", "x")
    rc_without, _, _ = _run_split()
    assert rc_with == rc_without, (rc_with, rc_without)


def test_advisory_precedes_the_goal_id_check():
    """Pins the ordering the four hermetic tests above depend on. Without this,
    moving the advisory below the goal-id check would leave them passing while
    covering a branch production never reaches (the probe-with-canonical-code-path
    'canonical binary is not canonical invocation' class)."""
    src = _WRAPPER.read_text(encoding="utf-8")
    nudge_at = src.index("ADOPTION NUDGE")
    goal_id_at = src.index('if [ -z "$GOAL_ID" ]')
    assert nudge_at < goal_id_at, "advisory must fire before the goal-id refusal"


# --------------------------------------------------------------------------
# 6. THE LIVE CALL SITE (guard-2285: "a check that a flag is ACCEPTED proves
#    nothing about whether any caller PASSES it — pin a live call site too,
#    otherwise the capability can sit unused").
# --------------------------------------------------------------------------
def test_iteration_close_release_hint_passes_reason_kind():
    """iteration-close.sh's blocked-branch hint is a real, LLM-facing release
    call site. It emitted a BARE release until g-115-8163's wiring pass — bare
    releases re-arm finished work at rank 1 on fresh metadata (g-115-5177) — so
    typing it fixes two defects at one site and gives the flag a real consumer."""
    src = (PROJECT_ROOT / "core" / "scripts" / "iteration-close.sh").read_text(
        encoding="utf-8"
    )
    hints = [
        line
        for line in src.splitlines()
        if "aspirations-release.sh" in line and "echo" in line
    ]
    assert hints, "expected at least one emitted release hint in iteration-close.sh"
    for line in hints:
        assert "--reason-kind" in line, f"untyped release hint: {line.strip()}"


def test_convention_documents_every_vocabulary_token():
    """The SSOT doc is where a reader looks up the vocabulary. It lives in an
    on-demand convention rather than the budgeted hot-path skills, which is the
    whole reason the wrapper carries the nudge instead of ten SKILL.md copies."""
    doc = (
        PROJECT_ROOT / "core" / "config" / "conventions" / "aspirations.md"
    ).read_text(encoding="utf-8")
    assert "Typed release negatives" in doc
    for token in _wrapper_tokens():
        assert f"`{token}`" in doc, f"{token!r} undocumented in conventions/aspirations.md"
