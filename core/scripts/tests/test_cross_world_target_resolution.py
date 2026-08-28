"""Regression tests for per-machine target-world resolution in the cross-world
transport scripts (g-115-4191).

WHAT BROKE. `cross-world-inject-goal.sh` and `cross-world-post.sh` each hardcoded

    WORLD_MAP[ayoai]="<a Windows absolute path>"

so both died before writing anything on every Linux box. Because these are the
SANCTIONED transport for promotion-cycle rule 2 (keep framework development out
of production), that rule stayed technically obeyed and practically unexecutable
fleet-wide. It went unnoticed because, from inside the loop, "correctly declined
to build framework code locally" and "successfully routed it to the dev world"
look identical -- the failure had no distinguishable signature.

WHY THESE ASSERTIONS. Four of the six pin things that were ALREADY WRONG once:

  * no-absolute-literal  -- asserts a COUNT OF ZERO across both files rather than
    checking the one known-bad string. A fixed list is satisfied by fixing the
    listed item; the invariant is "no hardcoded box-specific path anywhere here".
  * origin-derived       -- `cross-world-post.sh` had its ORIGIN fixed 2026-07-30
    and `cross-world-inject-goal.sh` did NOT, so the latter forged `omni@zds-mind`
    as its provenance stamp from every deployment. Classic guard-2078
    generalization remainder: a fix applied to one instance of a shared mechanism.
  * resolver-parity      -- the two resolvers are byte-identical BY INTENT. These
    files demonstrably drift apart (see above), and parity is the cheapest
    mechanical check that a future fix lands in both.
  * exit-3-not-1         -- "peer not hosted on this box" is the COMMON, EXPECTED
    case and must stay branchable, matching peer-board-post.sh's contract. If it
    collapsed back into die()'s exit 1 a caller could not tell "not hosted here"
    from "malformed arguments".

Resolution order under test (core/config/conventions/cross-deployment-channel.md):
    1. $PEER_WORLD_<ENV_ID>   env-id upper-cased, hyphens -> underscores
    2. peer_world_path:       in core/config/environments/<env-id>.yaml
There is deliberately no default and no fallback -- an absent path is
diagnosable, a wrong one writes into the WRONG world (guard-955 / rb-2983).
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _runtime_bash import bash_cmd  # noqa: E402  (guard-580: never a bare "bash")

PROJECT_ROOT = Path(__file__).resolve().parents[3]
INJECT = "core/scripts/cross-world-inject-goal.sh"
POST = "core/scripts/cross-world-post.sh"
SCRIPTS = [INJECT, POST]

EXIT_UNREACHABLE = 3

# The peer env-id under test, and the env-var name the transport derives from it.
#
# DERIVED, NOT WRITTEN OUT, and the reason is a downstream-only failure. Seed rule
# G2 rewrites the literal `MIND_` -> `MIND_` across `**/*` with
# apply_even_if_exempt, because it renames FUNCTIONAL env-var identifiers
# (MIND_AGENT/SID/WORLD/META) -- which is correct, and every MIND_AGENT below is
# meant to travel through it. But the transport's var name merely CONTAINS that
# pattern: it is `PEER_WORLD_` + the env-id upper-cased, so spelling that name out
# as a source literal gets its middle segment rewritten at every plant -- the
# brand token becomes MIND_ and the tail is already MIND, so the planted file
# asserts against a doubled name that nothing produces, while the script keeps
# deriving the real one at runtime from registry DATA the transform never touches.
# (This comment states the shape rather than the string on purpose: writing the
# literal here would be the very violation the block below asserts against, and G2
# would rewrite this paragraph into a sentence claiming a name is corrupted into
# itself -- guard-1855.) Measured at the ZDS v2.8.10 plant: all 8 tests here red
# downstream, 8 green in dev -- dev being the one place the transform never runs
# (; same class as  one file over).
#
# Composing it from the lowercase env-id is transform-safe by construction:
# `ayoai-mind` does not match `MIND_`, and `"PEER_WORLD_"` carries no brand. The
# upper-cased token therefore exists only at runtime, where no rewrite can reach
# it. A seed-manifest exemption would "fix" this too and is the WRONG remedy -- it
# would plant the branded literal downstream instead of removing it, and self-
# exclusion puts a permanent blind spot over the file most likely to hold the
# pattern (guard-1855).
#
# Deriving is also safe for the ASSERTION at test_unreachable_peer_exits_3_*, not
# only for the setup sites: guard-1628 forbids deriving an expected value FROM the
# source under test, and this derivation is independent of it -- the script reaches
# the same name through its own WORLD_ALIAS map and `tr` pipeline. Break either of
# those and this expectation does not move with the mutation, so the assertion
# still fails. Convention: cross-deployment-channel.md, resolution order step 1.
PEER_ENV_ID = "ayoai-mind"
PEER_WORLD_VAR = "PEER_WORLD_" + PEER_ENV_ID.upper().replace("-", "_")


def _run(rel_script, *args, env_extra=None):
    env = os.environ.copy()
    # guard-955 / rb-2983: never let a test inherit own-cloud and collide on a
    # production S3 key. These paths do not write to the store, but the pin is
    # unconditional so no future edit to this file can silently acquire one.
    env["STORAGE_BACKEND"] = "local"
    env.setdefault("ENVIRONMENT_ID", "ayoai-mind")
    env.setdefault("MIND_AGENT", "bravo")
    env.pop(PEER_WORLD_VAR, None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        bash_cmd(str(PROJECT_ROOT / rel_script), *args),
        capture_output=True, text=True, cwd=str(PROJECT_ROOT), env=env, timeout=120,
    )


def _inject_args(target="ayoai"):
    return ("--target", target, "--title", "Test: probe", "--description", "d",
            "--reason", "r", "--shared", "--dry-run")


def _post_args(target="ayoai"):
    return ("--target", target, "--inject-goal", '{"title":"probe","priority":"LOW"}',
            "--reason", "r", "--shared", "--dry-run")


ARGS_FOR = {INJECT: _inject_args, POST: _post_args}


@pytest.mark.parametrize("script", SCRIPTS)
def test_no_hardcoded_absolute_path_literal(script):
    """COUNT OF ZERO, not 'the known-bad string is gone'.

    A drive-letter path (``C:/``) or a bare POSIX absolute path assigned into the
    world map is box-specific by construction. Asserting zero means a NEW
    hardcode fails this test too; asserting the old literal's absence would not.
    """
    text = (PROJECT_ROOT / script).read_text(encoding="utf-8", errors="replace")
    code = [ln for ln in text.splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]
    offenders = [ln for ln in code if re.search(r'=\s*"[A-Za-z]:[/\\]', ln)]
    assert offenders == [], f"{script} hardcodes a drive-letter path: {offenders}"

    # And no WORLD_MAP assignment survives at all -- the map itself was the defect.
    map_assign = [ln for ln in code if re.search(r'\bWORLD_MAP\s*\[', ln)]
    assert map_assign == [], f"{script} still assigns into WORLD_MAP: {map_assign}"


@pytest.mark.parametrize("script", SCRIPTS)
def test_unreachable_peer_exits_3_with_actionable_diagnostic(script):
    """Not hosting a peer is NORMAL. It must exit 3 (branchable), not die()'s 1."""
    r = _run(script, *ARGS_FOR[script]())
    assert r.returncode == EXIT_UNREACHABLE, (
        f"{script}: expected exit {EXIT_UNREACHABLE} when the peer world is not "
        f"hosted here, got {r.returncode}.\nstderr:\n{r.stderr[:800]}"
    )
    err = r.stderr
    # The diagnostic must name BOTH remedies -- how to point at a hosted world,
    # and what to do when this box genuinely does not host it. The pre-fix message
    # ("Target world directory does not exist") named neither, and read as a hard
    # dead end; a real user decision was once filed as blocked on box topology.
    assert PEER_WORLD_VAR in err, "diagnostic omits the env-var remedy"
    assert "peer_world_path" in err, "diagnostic omits the registry remedy"
    # The third remedy must be the LOCAL board, and it must be a route that
    # actually works from a box in this state. See the two tests below for why
    # this assertion is no longer `"peer-board-post.sh" in err`.
    assert "board-post.sh --channel coordination" in err, (
        "diagnostic omits the local-board route, the only channel that works "
        "when target resolution has already failed (guard-2082)"
    )


@pytest.mark.parametrize("script", SCRIPTS)
def test_diagnostic_does_not_offer_peer_board_post_as_the_fallback(script):
    """peer-board-post.sh CANNOT be the fallback for a resolution failure.

    It resolves the target world through the SAME two sources this script just
    failed on -- `peer_board_post.py::peer_world_path()` reads
    $PEER_WORLD_<ENV_ID> then the registry `peer_world_path:` key -- so when
    this diagnostic prints, peer-board-post.sh is guaranteed to exit 3 for the
    identical reason. Measured 2026-08-28 (cc-07): rc=3 for all four registered
    peers, each naming the same registry key in its own remedy text.

    THIS ASSERTION REPLACES ONE THAT PINNED THE DEFECT. The prior version was
    `assert "peer-board-post.sh" in err` with the message "diagnostic omits the
    reachable-channel fallback" -- it REQUIRED the broken recommendation, and
    being a bare substring test it passed identically whether the script
    RECOMMENDED the command or WARNED AGAINST it. A substring cannot tell those
    apart, which is why this test keys on the recommending SHAPE (the command
    line the reader would copy) rather than on the name appearing at all.
    Naming it in a "NOT this, because ..." warning is correct and must stay
    allowed (guard-2435: a control has to be able to fail for the right reason).
    """
    r = _run(script, *ARGS_FOR[script]())
    assert r.returncode == EXIT_UNREACHABLE
    err = r.stderr
    offered = re.findall(r"^\s*(?:board message\s*:)?\s*bash \S*peer-board-post\.sh",
                         err, re.MULTILINE)
    assert offered == [], (
        f"{script}: the exit-3 diagnostic offers peer-board-post.sh as a runnable "
        f"fallback, but it shares this script's resolution precondition and will "
        f"exit 3 too. Offered lines: {offered}"
    )


@pytest.mark.parametrize("script", SCRIPTS)
def test_diagnostic_only_recommends_flags_board_post_actually_accepts(script):
    """THE RECURRENCE-3 CATCHER, and the reason this file needed a new test.

    Recurrences 1 and 2 were both "the sanctioned transport is dead fleet-wide,
    silently". Every existing test here pins the SCRIPT's behaviour against tmp
    fixtures, so both recurrences happened with the suite fully green -- the
    dead layer was never the script. The remaining silent-death shape is a
    diagnostic that confidently names a route the reader cannot run, which is
    exactly what the prior guidance did.

    So: every long flag the exit-3 diagnostic recommends for board-post.sh must
    be a flag board-post.sh actually parses. Caught by hand while writing this
    guidance -- `--requires-action-by` does not exist; guard-2082 means
    `requires_action_by:` as a TAG VALUE, not a flag.
    """
    r = _run(script, *ARGS_FOR[script]())
    err = r.stderr
    m = re.search(r"bash \S*board-post\.sh(?P<rest>(?:[^\n]*\\\n?|[^\n]*))+", err)
    assert m, "no board-post.sh invocation found in the diagnostic"
    recommended = set(re.findall(r"--[a-z][a-z-]+", m.group(0)))
    accepted = set(re.findall(r"--[a-z][a-z-]+",
                              (PROJECT_ROOT / "core/scripts/board-post.sh").read_text(encoding="utf-8")))
    accepted |= set(re.findall(r"--[a-z][a-z-]+",
                               (PROJECT_ROOT / "core/scripts/board.py").read_text(encoding="utf-8")))
    unknown = recommended - accepted
    assert unknown == set(), (
        f"{script}: exit-3 diagnostic recommends board-post.sh flag(s) that "
        f"board-post.sh does not accept: {sorted(unknown)}"
    )


@pytest.mark.parametrize("script", SCRIPTS)
def test_env_var_resolves_target(script, tmp_path):
    """$PEER_WORLD_<ENV_ID> is honoured, and the resolved dir is actually used."""
    peer = tmp_path / "peerworld"
    (peer / "board").mkdir(parents=True)
    (peer / "aspirations.jsonl").touch()
    r = _run(script, *ARGS_FOR[script](),
             env_extra={PEER_WORLD_VAR: str(peer)})
    assert r.returncode == 0, (
        f"{script}: env-var resolution should succeed.\n"
        f"rc={r.returncode}\nstdout:\n{r.stdout[:600]}\nstderr:\n{r.stderr[:600]}"
    )
    assert str(peer) in r.stdout, (
        f"{script}: resolved path absent from dry-run output -- the env var was "
        f"read but not used.\nstdout:\n{r.stdout[:600]}"
    )


@pytest.mark.parametrize("script", SCRIPTS)
def test_registry_peer_world_path_resolves_target(script, tmp_path, monkeypatch):
    """Fallback 2: peer_world_path: in the registry entry.

    Written to a REAL registry file and removed afterwards, because the scripts
    read the committed path directly. The finally-block restore is load-bearing:
    a leaked peer_world_path would make every later run on this box resolve to a
    deleted tmp dir.
    """
    peer = tmp_path / "peerworld"
    (peer / "board").mkdir(parents=True)
    (peer / "aspirations.jsonl").touch()
    reg = PROJECT_ROOT / "core" / "config" / "environments" / "ayoai-mind.yaml"
    original = reg.read_text(encoding="utf-8")
    try:
        reg.write_text(original + f"\npeer_world_path: {peer}\n", encoding="utf-8")
        r = _run(script, *ARGS_FOR[script]())
        assert r.returncode == 0, (
            f"{script}: registry resolution should succeed.\n"
            f"rc={r.returncode}\nstderr:\n{r.stderr[:600]}"
        )
        assert str(peer) in r.stdout
    finally:
        reg.write_text(original, encoding="utf-8")


def test_inject_goal_origin_is_derived_not_a_peer_identity(tmp_path):
    """guard-2078 remainder: inject-goal.sh hardcoded ``omni@zds-mind``.

    A literal origin is correct in at most one promotion tier and silently forges
    a peer identity in every other -- it stamped BOTH injected_by (G2) and
    cross_world_origin (G5). This mattered MORE once the resolution fix revived
    the transport: a dead script stamps nothing, so reviving it without fixing
    ORIGIN would have converted a silent no-op into silent misattribution.
    """
    peer = tmp_path / "peerworld"
    (peer / "board").mkdir(parents=True)
    (peer / "aspirations.jsonl").touch()
    r = _run(INJECT, *_inject_args(),
             env_extra={PEER_WORLD_VAR: str(peer),
                        "MIND_AGENT": "bravo", "ENVIRONMENT_ID": "ayoai-mind"})
    assert r.returncode == 0, f"rc={r.returncode}\nstderr:\n{r.stderr[:600]}"
    assert "omni@zds-mind" not in r.stdout, (
        "inject-goal.sh still stamps the hardcoded peer identity omni@zds-mind"
    )
    assert "bravo@ayoai-mind" in r.stdout, (
        "provenance is not derived from MIND_AGENT + ENVIRONMENT_ID"
    )

    # No literal ORIGIN assignment may return, in either script.
    for script in SCRIPTS:
        text = (PROJECT_ROOT / script).read_text(encoding="utf-8", errors="replace")
        bad = [ln for ln in text.splitlines()
               if re.match(r'^\s*ORIGIN\s*=\s*"[^"$]*@', ln)]
        assert bad == [], f"{script} reintroduced a literal ORIGIN: {bad}"


def test_this_file_holds_no_transform_corruptible_peer_var_literal():
    """COUNT OF ZERO for the branded env-var spelling -- in THIS file, not the scripts.

    Every other source-scan test here inspects the two transport scripts. This one
    inspects the test module itself, because the corruption it guards against is
    one a test file can only inflict on itself: the scripts derive the name at
    runtime and were never affected, while these assertions were written out and
    got rewritten under them at every plant. The whole failure is invisible in dev
    -- dev is the one deployment where the transform does not run -- so a test is
    the only thing that can carry the constraint forward.

    ASSEMBLED FROM FRAGMENTS, NOT WRITTEN OUT (guard-1855). Neither half carries
    the `MIND_` pattern on its own, so the needle exists only at runtime. Spelling
    it out would make this function the single remaining violation of the invariant
    it asserts, and G2 would silently rewrite the needle to match the corrupted
    text -- leaving a test that passes at every plant while checking nothing.
    """
    needle = "PEER_WORLD_" + "AYOAI" + "_MIND"
    text = Path(__file__).read_text(encoding="utf-8", errors="replace")
    hits = [f"L{i}: {ln.strip()[:100]}"
            for i, ln in enumerate(text.splitlines(), 1) if needle in ln]
    assert hits == [], (
        "this module spells out the branded peer env-var instead of deriving it "
        "from PEER_ENV_ID; the seed de-brand rewrites it at every downstream plant "
        f"and the assertions go red there while staying green here:\n" + "\n".join(hits)
    )


def test_peer_world_var_derivation_is_not_vacuous():
    """The derivation must actually PRODUCE the name the transport looks up.

    Without this, PEER_WORLD_VAR could be misderived (wrong separator, wrong case,
    missing prefix) and every setup site would agree with every other setup site
    while none of them matched the script -- the tests would pass as a closed loop.
    The expected value is assembled from fragments for the same reason as above.
    """
    assert PEER_WORLD_VAR == "PEER_WORLD_" + "AYOAI" + "_MIND"
    assert PEER_ENV_ID == "ayoai" + "-mind"
    # And the registry entry for that env-id must exist, or the id is a typo that
    # no amount of correct string-building would catch.
    assert (PROJECT_ROOT / "core" / "config" / "environments"
            / f"{PEER_ENV_ID}.yaml").is_file()


FLEET_AGENT_NAMES = ("omni", "alpha", "bravo", "charlie", "delta",
                     "echo", "foxtrot", "zeta")


@pytest.mark.parametrize("script", SCRIPTS)
def test_no_literal_agent_identity_anywhere(script):
    """COUNT OF ZERO across the whole file -- not "the known-bad string is gone".

    The pre-existing guard (test_inject_goal_origin_is_derived_not_a_peer_identity)
    matched only ``^\\s*ORIGIN\\s*=\\s*"[^"$]*@`` -- an ORIGIN assignment containing
    '@'. The dict value ``'filed_by_agent': 'omni'`` is neither an ORIGIN
    assignment nor contains '@', so it sat ~400 lines below the line that WAS
    fixed and the guard still reported green. The predicate was NARROWER than the
    class it was written to prevent, so the guard PASSING is what concealed the
    survivor (guard-1802 shape).

    Two forms are checked because the two real survivors took different shapes:
      * a quoted literal            -- 'filed_by_agent': 'omni'
      * a default-value expansion   -- ${MIND_AGENT:-omni}
    The second is the subtler one: it sat two lines under a comment reading
    "DIE rather than default", defaulting the agent half of the provenance stamp
    while the env half correctly refused.

    'echo' is both a fleet agent name and a shell builtin. Matching only QUOTED
    occurrences dissolves that collision by construction -- the builtin is never
    written as 'echo' or "echo", so no exclusion list is needed.
    """
    names = "|".join(FLEET_AGENT_NAMES)
    quoted = re.compile(r"""["'](?:""" + names + r""")["']""")
    defaulted = re.compile(r":-\s*(?:" + names + r")\s*\}")

    offenders = []
    text = (PROJECT_ROOT / script).read_text(encoding="utf-8", errors="replace")
    for lineno, ln in enumerate(text.splitlines(), 1):
        if ln.lstrip().startswith("#"):
            continue
        if quoted.search(ln) or defaulted.search(ln):
            offenders.append(f"  {lineno}: {ln.strip()}")

    assert offenders == [], (
        f"{script} contains {len(offenders)} literal agent identit(ies). A literal "
        f"agent name is correct in at most one promotion tier and silently forges "
        f"a peer identity in every other:\n" + "\n".join(offenders)
    )


def _peer_with_target_aspiration(tmp_path, asp_id="asp-115"):
    """A peer world whose aspirations.jsonl holds a real target aspiration.

    Includes a SECOND, unrelated record so the live-write test can prove the
    in-place rewrite preserved every line it did not mean to touch.
    """
    peer = tmp_path / "peerworld"
    (peer / "board").mkdir(parents=True)
    asp_file = peer / "aspirations.jsonl"
    records = [
        {"id": "asp-999", "title": "unrelated bystander", "status": "active",
         "goals": [{"id": "g-999-01", "title": "untouched", "status": "pending"}]},
        {"id": asp_id, "title": "target", "status": "active",
         "goals": [{"id": "g-115-1", "title": "pre-existing", "status": "completed"}]},
    ]
    with open(asp_file, "w", encoding="utf-8", newline="\n") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    return peer, asp_file


def _live_env(peer):
    return {PEER_WORLD_VAR: str(peer),
            "MIND_AGENT": "bravo", "ENVIRONMENT_ID": "ayoai-mind"}


def test_live_write_embeds_goal_and_derives_filed_by_agent(tmp_path):
    """The LIVE-write path -- which every pre-existing test in this file skipped.

    All of them pass --dry-run, and ``filed_by_agent`` is built ONLY on the live
    path (the --dry-run block contains zero occurrences of it). So the injected
    record's shape was unreachable by the suite, which is how a hardcoded
    ``filed_by_agent: 'omni'`` survived g-115-4191 -- the goal whose entire
    purpose was removing hardcoded peer identities from this very file.
    """
    peer, asp_file = _peer_with_target_aspiration(tmp_path)
    r = _run(INJECT, "--target", "ayoai", "--title", "Investigate: live probe",
             "--description", "d", "--reason", "r", "--shared",
             env_extra=_live_env(peer))
    assert r.returncode == 0, (
        f"live write should succeed.\nrc={r.returncode}\n"
        f"stdout:\n{r.stdout[:600]}\nstderr:\n{r.stderr[:800]}"
    )

    records = [json.loads(ln) for ln in
               asp_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    by_id = {rec["id"]: rec for rec in records}

    # The bystander record must survive the in-place rewrite untouched.
    assert "asp-999" in by_id, "the rewrite dropped an unrelated record"
    assert by_id["asp-999"]["goals"] == [
        {"id": "g-999-01", "title": "untouched", "status": "pending"}]

    target = by_id["asp-115"]
    injected = [g for g in target["goals"] if g["id"] != "g-115-1"]
    assert len(injected) == 1, f"expected exactly one injected goal, got {injected}"
    goal = injected[0]

    # F-001: the field that the dry-run-only suite could never see.
    assert goal["filed_by_agent"] == "bravo", (
        f"filed_by_agent must derive from MIND_AGENT, got {goal['filed_by_agent']!r}"
    )
    # The guardrail stamps must hold on the live path too, not just in dry-run text.
    assert goal["injected_by"] == "bravo@ayoai-mind"          # G2
    assert goal["cross_world_origin"] == "bravo@ayoai-mind"   # G5
    assert goal["sandbox"] is True                            # G2
    assert goal["participants"] == ["agent", "user"]          # G3


def test_write2_failure_prints_a_diagnostic_instead_of_dying_silently(tmp_path):
    """The Write [2] failure branch was unreachable, and this proves it is not.

    Under ``set -euo pipefail`` a ``VAR=$(cmd)`` assignment inherits cmd's exit
    status, so a non-zero python exit killed the shell AT THE ASSIGNMENT -- some
    60 lines before the ``if [ "$WRITE2_RC" != "ok" ]`` check written to report
    it. Compounding it, the old ``2>&1`` captured python's explanation into the
    variable that was never echoed, so a failed peer write produced a bare exit 1
    with no message anywhere.
    """
    peer, _ = _peer_with_target_aspiration(tmp_path)
    r = _run(INJECT, "--target", "ayoai", "--title", "Investigate: missing target",
             "--description", "d", "--reason", "r", "--shared",
             "--target-aspiration", "asp-nonexistent",
             env_extra=_live_env(peer))

    assert r.returncode == 1, (
        f"expected exit 1 from a failed Write [2], got {r.returncode}\n"
        f"stderr:\n{r.stderr[:600]}"
    )
    combined = r.stdout + r.stderr
    assert "Write [2] FAILED" in combined, (
        "the failure branch is STILL unreachable -- no diagnostic was printed.\n"
        f"stdout:\n{r.stdout[:400]}\nstderr:\n{r.stderr[:400]}"
    )
    assert "asp-nonexistent" in combined, (
        "the diagnostic does not name the target aspiration that was not found"
    )


def test_live_write_is_atomic_not_a_truncate_rewrite(tmp_path):
    """Write [2] must not open the peer's store with mode 'w'.

    ``open(asp_file, 'w')`` truncates before writing, so a crash in that window
    leaves the peer's ENTIRE goal store empty. The hazard is realized, not
    theoretical: on 2026-07-09 a world aspirations.jsonl went from 1366 goals to
    a single fixture and needed .history recovery (guard-955 / rb-2983).

    Asserted structurally rather than by racing a crash: the write must go
    through a temp file plus os.replace, and no truncating open of asp_file may
    remain anywhere in the script.
    """
    text = (PROJECT_ROOT / INJECT).read_text(encoding="utf-8", errors="replace")
    code = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    body = "\n".join(code)

    truncating = [ln for ln in code
                  if re.search(r"open\(\s*asp_file\s*,\s*['\"]w", ln)]
    assert truncating == [], (
        f"Write [2] still truncates the peer's store in place: {truncating}"
    )
    assert "os.replace(" in body, "atomic replace is missing from the write path"
    assert "tempfile.mkstemp(" in body, "no temp file is created for the rewrite"
    assert "os.fsync(" in body, "the temp file is replaced without an fsync"


def test_concurrent_change_abort_does_not_false_fire_on_a_missing_trailing_newline(tmp_path):
    """The abort added alongside the atomic write must not fire on a LEGITIMATE write.

    Write [2] now re-counts the peer file immediately before the swap and aborts
    if the count differs from the number of lines it read, so a line appended by
    the peer's own daemon mid-rewrite is never silently clobbered (guard-1706;
    the peer does not honour this script's spinlock).

    The risk that introduces is the INVERSE of the one it fixes: if the two
    counts can ever differ for a benign reason, the abort fires on every
    injection and the transport is dead -- the same silent fleet-wide breakage
    g-115-4191 fixed, reintroduced by its own hardening. A JSONL file whose last
    line lacks a trailing newline is the obvious candidate, and it is ordinary:
    any tool that writes records without a final newline produces one.

    Asserted behaviourally rather than by reading the counting code, because the
    property that matters is "a normal injection still succeeds", not "the
    arithmetic looks right".
    """
    peer = tmp_path / "peerworld"
    (peer / "board").mkdir(parents=True)
    asp_file = peer / "aspirations.jsonl"
    records = [
        {"id": "asp-999", "title": "bystander", "status": "active", "goals": []},
        {"id": "asp-115", "title": "target", "status": "active",
         "goals": [{"id": "g-115-1", "title": "pre-existing", "status": "completed"}]},
    ]
    # Deliberately NO trailing newline on the final record.
    asp_file.write_text(
        "\n".join(json.dumps(r) for r in records), encoding="utf-8", newline="\n")
    assert not asp_file.read_text(encoding="utf-8").endswith("\n")

    r = _run(INJECT, "--target", "ayoai", "--title", "Investigate: no trailing newline",
             "--description", "d", "--reason", "r", "--shared",
             env_extra=_live_env(peer))

    assert r.returncode == 0, (
        "the concurrency abort false-fired on a legitimate write -- a peer store "
        "whose last line lacks a trailing newline must still be injectable.\n"
        f"rc={r.returncode}\nstdout:\n{r.stdout[:500]}\nstderr:\n{r.stderr[:700]}"
    )
    assert "refusing to clobber" not in (r.stdout + r.stderr), (
        "the concurrent-change abort fired although nothing changed the file"
    )

    # And the write still landed correctly.
    recs = [json.loads(ln) for ln in
            asp_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    target = [x for x in recs if x["id"] == "asp-115"][0]
    assert len([g for g in target["goals"] if g["id"] != "g-115-1"]) == 1
    assert [x for x in recs if x["id"] == "asp-999"], "bystander record was dropped"


def test_resolver_blocks_are_byte_identical_across_both_scripts():
    """These two files drift apart -- parity is the mechanical guard.

    Measured precedent: cross-world-post.sh got its ORIGIN fix on 2026-07-30 and
    cross-world-inject-goal.sh did not, leaving the second forging provenance for
    a day. The resolver is duplicated by intent (both are standalone bash entry
    points with no shared lib), so the only cheap defence against the next
    one-sided fix is asserting the duplicate is exact.
    """
    def extract(path):
        text = (PROJECT_ROOT / path).read_text(encoding="utf-8", errors="replace")
        blocks = []
        for fn in ("_peer_unreachable", "resolve_target"):
            m = re.search(rf"^{fn}\(\) \{{\n(.*?)^\}}", text, re.S | re.M)
            assert m, f"{path}: cannot locate {fn}()"
            blocks.append(m.group(0))
        return "\n".join(blocks)

    a, b = extract(INJECT), extract(POST)
    assert a == b, (
        "resolver blocks have diverged between cross-world-inject-goal.sh and "
        "cross-world-post.sh. They are duplicated deliberately; if you changed "
        "one, change the other identically (guard-2078)."
    )
