"""Tests for the guard-2846 xtrace/credential defense ().

Covers the shared predicate, the Layer-A gate through main() with real hook
payloads, and the Layer-C detective's lane classification.

THE GOAL'S OWN VERIFY CLAUSE IS THE SPINE, quoted: "a command combining an
xtrace flag with a credential-sourcing wrapper is refused, AND the same command
with the override token is allowed and logged. Do NOT verify by checking the
gate file exists -- a presence check passes forever on a gate that never fires."
So every test here drives BEHAVIOUR: nothing asserts that a file is present.

The fixture builds its own script tree rather than pointing at the live world,
so the suite cannot go green or red because of what this deployment happens to
have installed.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
GATE = CORE_SCRIPTS / "xtrace-credential-gate.py"
AUDIT = CORE_SCRIPTS / "xtrace-credential-audit.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pred = _load("xcred_pred", CORE_SCRIPTS / "_xtrace_credential_predicate.py")


def _tree(tmp: Path):
    """A miniature two-tier script tree mirroring the real shapes.

    loader        -- exports secrets (the `_env.sh` role)
    wrapper       -- sources the loader via the $(dirname ...) idiom, the
                     DOMINANT real form and the one a path-parsing extractor
                     silently drops
    deep          -- sources the wrapper (transitive, one hop further)
    plain         -- sources nothing dangerous
    pathconf      -- named _paths.sh and sources a credential file: the
                     path-config exception
    pathconf_user -- sources pathconf, so it must stay clear too
    """
    d = tmp / "scripts"
    d.mkdir(parents=True)
    (d / "_env.sh").write_text("export SECRET_TOKEN=abc\n", encoding="utf-8")
    (d / "wrapper.sh").write_text(
        '#!/usr/bin/env bash\nsource "$(dirname "${BASH_SOURCE[0]}")/_env.sh"\naws "$@"\n',
        encoding="utf-8")
    (d / "deep.sh").write_text(
        '#!/usr/bin/env bash\nsource "$(dirname "${BASH_SOURCE[0]}")/wrapper.sh"\n',
        encoding="utf-8")
    (d / "plain.sh").write_text("#!/usr/bin/env bash\necho hello\n", encoding="utf-8")
    (d / "_paths.sh").write_text(
        '#!/usr/bin/env bash\n. "$_MD_DIR/.env.local"\n', encoding="utf-8")
    (d / "pathconf_user.sh").write_text(
        '#!/usr/bin/env bash\nsource "$(dirname "$0")/_paths.sh"\n', encoding="utf-8")
    return d


def _off(cmd, d):
    return pred.offending(cmd, str(d.parent), [str(d)])


# ---- predicate: the true-positive side -----------------------------------


def test_xtrace_on_credential_wrapper_is_offending():
    with tempfile.TemporaryDirectory() as t:
        d = _tree(Path(t))
        assert _off(f"bash -x {d}/wrapper.sh s3 ls", d)


def test_dominant_dirname_idiom_is_seen():
    """The wrapper sources via `$(dirname ...)`. A path-parsing extractor stops
    at the first whitespace, sees `"$(dirname`, and drops it -- measured as a
    48% undercount that omitted every canonical credential wrapper."""
    with tempfile.TemporaryDirectory() as t:
        d = _tree(Path(t))
        assert "$(dirname" in (d / "wrapper.sh").read_text(encoding="utf-8")
        assert pred.script_reaches_loader(str(d / "wrapper.sh"))


def test_transitive_one_hop_further_is_reached():
    with tempfile.TemporaryDirectory() as t:
        d = _tree(Path(t))
        assert pred.script_reaches_loader(str(d / "deep.sh"))
        assert _off(f"bash -x {d}/deep.sh", d)


def test_every_xtrace_door_is_covered():
    with tempfile.TemporaryDirectory() as t:
        d = _tree(Path(t))
        for cmd in (
            f"bash -x {d}/wrapper.sh",
            f"bash -eux {d}/wrapper.sh",
            f"sh -x {d}/wrapper.sh",
            f"bash -o xtrace {d}/wrapper.sh",
            f"set -x; {d}/wrapper.sh",
            f"SHELLOPTS=xtrace bash {d}/wrapper.sh",
            f"export SHELLOPTS=xtrace; bash {d}/wrapper.sh",
            f"BASH_XTRACEFD=3 bash {d}/wrapper.sh",
        ):
            assert _off(cmd, d), f"missed xtrace door: {cmd}"


# ---- predicate: the false-positive side ----------------------------------


def test_common_dash_x_commands_are_not_flagged():
    """`tar -x`, `grep -x`, `chmod -x`, `test -x`, `unzip -x` are common and
    harmless. Flagging them makes the gate a nuisance, and a nuisance gate gets
    switched off -- which is the failure this whole goal is about."""
    with tempfile.TemporaryDirectory() as t:
        d = _tree(Path(t))
        for cmd in ("tar -xzf a.tar.gz", "grep -x pat f", "chmod -x s.sh",
                    "test -x s.sh", "unzip -x a.zip", "echo bashful -x"):
            assert not _off(cmd, d), f"false positive: {cmd}"


def test_set_plus_x_disables_and_must_not_match():
    with tempfile.TemporaryDirectory() as t:
        d = _tree(Path(t))
        assert not _off(f"set +x; {d}/wrapper.sh", d)


def test_credential_script_without_xtrace_is_allowed():
    with tempfile.TemporaryDirectory() as t:
        d = _tree(Path(t))
        assert not _off(f"bash {d}/wrapper.sh s3 ls", d)


def test_xtrace_on_non_credential_script_is_allowed():
    with tempfile.TemporaryDirectory() as t:
        d = _tree(Path(t))
        assert not _off(f"bash -x {d}/plain.sh", d)


def test_path_config_exception_and_its_dependents_stay_clear():
    """_paths.sh sources a credential FILE but holds path config, and 496 of 930
    scripts source it. Counting it takes the blocked population from 3.9% to
    63% -- a gate that refuses xtrace on essentially everything."""
    with tempfile.TemporaryDirectory() as t:
        d = _tree(Path(t))
        assert not pred.script_reaches_loader(str(d / "_paths.sh"))
        assert not _off(f"bash -x {d}/_paths.sh", d)
        assert not _off(f"bash -x {d}/pathconf_user.sh", d)


def test_override_construct_allows_the_offending_command():
    """An INVOCATION suppresses: token + `=` + a non-empty quoted reason."""
    with tempfile.TemporaryDirectory() as t:
        d = _tree(Path(t))
        assert _off(f"bash -x {d}/wrapper.sh s3 ls", d), "control: offending without the token"
        assert not _off(
            f'{pred.OVERRIDE_TOKEN}="tracing the gate itself" bash -x {d}/wrapper.sh s3 ls', d)


def test_merely_naming_the_override_does_NOT_suppress():         # noqa: N802 - 
    """REGRESSION (). `OVERRIDE_TOKEN in command` read a MENTION as an
    invocation, so a trailing comment -- or a runbook explaining the bypass --
    silently disarmed a credential-facing gate.

    Arm B's suffix is the goal's measured fixture verbatim:
        `  # do not reach for XTRACE_CREDENTIAL_GATE_OVERRIDE here`
    The command it decorates is this suite's hermetic tree rather than the goal's
    live `bash -x core/scripts/liveness-check.sh --agent alpha --json`, because
    this file's contract is that no test may go green or red on what a deployment
    happens to have installed. The non-vacuousness arm A was chosen for is
    preserved by asserting arm A in the same test, which is stronger: it cannot
    go vacuous as the live tree changes.
    """
    with tempfile.TemporaryDirectory() as t:
        d = _tree(Path(t))
        bare = f"bash -x {d}/wrapper.sh s3 ls"
        assert _off(bare, d), "arm A control: must fire, or arm B proves nothing"
        mention = bare + f"  # do not reach for {pred.OVERRIDE_TOKEN} here"
        assert _off(mention, d), "arm B: a MENTION must not suppress a credential gate"


def test_override_requires_a_non_empty_quoted_reason():
    """The justification is what makes the bypass auditable, so an empty one is
    not an override. Also pins that a glued prefix cannot borrow the token."""
    with tempfile.TemporaryDirectory() as t:
        d = _tree(Path(t))
        bare = f"bash -x {d}/wrapper.sh s3 ls"
        assert _off(f'{pred.OVERRIDE_TOKEN}="" ' + bare, d), "empty reason is not an override"
        assert _off(f'FOO_{pred.OVERRIDE_TOKEN}="why" ' + bare, d), "glued prefix must not match"
        assert not _off(f"{pred.OVERRIDE_TOKEN}='why' " + bare, d), "single quotes are valid"


def test_a_documented_construct_inside_a_heredoc_does_not_suppress():
    """FRESH-EYES REGRESSION (). The construct requirement stops a
    bare MENTION, but a runbook that writes the CORRECT invocation form into a
    heredoc is still data, not an invocation -- and it disarmed the gate for the
    command that writes it until the override test moved behind
    strip_heredoc_bodies. Same self-inflicted-by-documentation shape the goal
    was filed on, one narrowing further in."""
    with tempfile.TemporaryDirectory() as t:
        d = _tree(Path(t))
        bare = f"bash -x {d}/wrapper.sh s3 ls"
        assert _off(bare, d), "arm A control: must fire, or this proves nothing"
        doc = (
            "cat > runbook.md <<'EOF'\n"
            f'To bypass, run {pred.OVERRIDE_TOKEN}="your reason" before the command.\n'
            "EOF\n" + bare
        )
        assert _off(doc, d), "a heredoc-documented construct must NOT suppress"


def test_override_is_not_start_anchored():
    """rb-9764: the fleet's dominant shape is `cd ... && VAR=v bash ...`. A
    start-anchored test would refuse every real invocation -- trading a silent
    bypass for a silently-unusable escape hatch."""
    with tempfile.TemporaryDirectory() as t:
        d = _tree(Path(t))
        cmd = f'cd /tmp && {pred.OVERRIDE_TOKEN}="mid-command invocation" bash -x {d}/wrapper.sh s3 ls'
        assert not _off(cmd, d)


# ---- the env-assignment anchor (regression, found in production) ---------


def test_env_assignment_inside_a_quoted_string_is_not_a_command():
    """REGRESSION. _ENV_X matched on a bare word boundary while its four
    siblings all required command position, so the assignment matched inside
    ANY quoted string. Measured the moment the gate went live: it refused the
    command writing THIS FILE, whose only xtrace token was `SHELLOPTS=xtrace`
    inside a Python string literal."""
    with tempfile.TemporaryDirectory() as t:
        d = _tree(Path(t))
        for quoted in ('    f"SHELLOPTS=xtrace bash {d}/wrapper.sh",',
                       '    f"BASH_XTRACEFD=3 bash {d}/w.sh",',
                       "items = ['SHELLOPTS=xtrace']"):
            assert not _off(quoted, d), f"quoted data read as a command: {quoted}"
        # ... and the anchoring lost no real invocation form:
        assert _off(f"SHELLOPTS=xtrace bash {d}/wrapper.sh", d)
        assert _off(f"export SHELLOPTS=xtrace; bash {d}/wrapper.sh", d)


# ---- heredoc bodies: data vs command -------------------------------------


def test_heredoc_written_to_a_file_is_data_not_a_command():
    """Writing a test, guardrail or convention that QUOTES the offending shape
    is not running it. A gate that refuses its own documentation cannot coexist
    with its own maintenance: it gets overridden as a reflex, and a reflex
    override is indistinguishable from no gate."""
    with tempfile.TemporaryDirectory() as t:
        d = _tree(Path(t))
        doc = f"cat > doc.md <<'EOF'\nnever run: bash -x {d}/wrapper.sh\nEOF"
        assert not _off(doc, d)


def test_heredoc_fed_to_a_shell_is_still_a_command():
    """The evasion is closed STRUCTURALLY, so the exemption never rests on an
    argument about who would bother to use it."""
    with tempfile.TemporaryDirectory() as t:
        d = _tree(Path(t))
        piped = f"cat <<'EOF' | bash\nset -x\n{d}/wrapper.sh\nEOF"
        direct = f"bash <<'EOF'\nbash -x {d}/wrapper.sh\nEOF"
        assert _off(piped, d), "heredoc piped into a shell IS the command"
        assert _off(direct, d), "heredoc fed to bash IS the command"


def test_a_real_xtrace_beside_a_written_heredoc_still_fires():
    """The stripping must not become a prefix that launders the rest of the
    command -- the residual outside the body is still scanned."""
    with tempfile.TemporaryDirectory() as t:
        d = _tree(Path(t))
        mixed = (f"cat > d.md <<'EOF'\nnever do: bash -x {d}/wrapper.sh\nEOF\n"
                 f"bash -x {d}/wrapper.sh")
        assert _off(mixed, d)


def test_strip_leaves_non_heredoc_commands_untouched():
    cmd = "echo one\nbash -x s.sh\necho two"
    assert pred.strip_heredoc_bodies(cmd) == cmd


# ---- the gate, through main(), with real hook payloads -------------------


def _run_gate(command, tool_name="Bash", raw=None, env_world=None):
    payload = raw if raw is not None else json.dumps(
        {"tool_name": tool_name, "tool_input": {"command": command}})
    env = os.environ.copy()
    if env_world:
        env["WORLD_PATH"] = env_world
    return subprocess.run([sys.executable, str(GATE)], input=payload,
                          capture_output=True, text=True, timeout=30, env=env)


def test_gate_denies_the_real_leak_shape():
    with tempfile.TemporaryDirectory() as t:
        d = _tree(Path(t))
        proc = _run_gate(f"bash -x {d}/wrapper.sh s3 ls")
        assert proc.returncode == 0, "a hook must never exit non-zero"
        hs = json.loads(proc.stdout)["hookSpecificOutput"]
        assert hs["permissionDecision"] == "deny"
        reason = hs["permissionDecisionReason"]
        assert "wrapper.sh" in reason, "the deny must name the offending script"
        assert pred.OVERRIDE_TOKEN in reason, "the deny must name the escape hatch"
        # the three safe alternatives guard-2846 already lists
        assert "NAMES" in reason and "dry-run" in reason and "Read the script" in reason


def test_gate_allows_with_override_token():
    with tempfile.TemporaryDirectory() as t:
        d = _tree(Path(t))
        proc = _run_gate(
            f'{pred.OVERRIDE_TOKEN}="tracing the gate itself" bash -x {d}/wrapper.sh s3 ls')
        assert proc.returncode == 0
        assert proc.stdout.strip() == "", "override must approve with no mutation"


def test_gate_denies_when_the_override_is_only_mentioned():
    """REGRESSION (), through the REAL hook entry point: a mention
    must still be refused. The predicate test above is the unit; this is the
    wiring, because a green predicate over a short-circuiting wrapper is exactly
    how a gate looks enabled while being off."""
    with tempfile.TemporaryDirectory() as t:
        d = _tree(Path(t))
        proc = _run_gate(
            f"bash -x {d}/wrapper.sh s3 ls  # do not reach for {pred.OVERRIDE_TOKEN} here")
        assert proc.returncode == 0
        assert proc.stdout.strip(), "a mention must NOT approve"
        assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_gate_fails_open_on_garbage_stdin():
    proc = _run_gate(None, raw="not json at all")
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_gate_ignores_non_bash_tools():
    proc = _run_gate(None, raw=json.dumps(
        {"tool_name": "Read", "tool_input": {"file_path": "x"}}))
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_gate_resolves_scripts_through_world_path_env():
    """Every credential wrapper measured on this deployment lives in the
    EXTERNAL world tree, so a gate searching only core/scripts would be
    silently inert for the entire real population."""
    with tempfile.TemporaryDirectory() as t:
        d = _tree(Path(t))
        proc = _run_gate("bash -x wrapper.sh s3 ls", env_world=str(d.parent))
        assert proc.returncode == 0
        assert proc.stdout.strip(), "a bare script name must resolve via WORLD_PATH"
        assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


# ---- the detective -------------------------------------------------------


def test_detective_shares_the_predicate_with_the_gate():
    """A divergence between the layers is worse than either being absent: the
    sweep would report clean over exactly the population the gate stopped
    covering."""
    src = AUDIT.read_text(encoding="utf-8")
    assert "from _xtrace_credential_predicate import offending" in src
    assert "def has_xtrace" not in src, "the detective must not restate the predicate"


def test_detective_exempts_its_own_family():
    audit = _load("xcred_audit", AUDIT)
    for name in ("_xtrace_credential_predicate.py", "xtrace-credential-gate.py",
                 "xtrace-credential-audit.py"):
        assert name in audit.SELF_EXEMPT, (
            "the defense quotes offending shapes on purpose; scanning itself "
            "would report the defense as the defect on every run")


# ---- override logging (regression: the deny message promised it) ---------
#
# The deny text says "The bypass is recorded", and for the gate's first hour it
# was not: no logging existed at all. An unlogged bypass makes a security
# control's own promise false, and the goal's VERIFY BY clause requires the
# override be "allowed AND logged". These tests assert at the DECISION boundary
# with the writer monkeypatched -- whether a record then survives the spool is
# _gate_log's own tested responsibility, not this gate's.

import io  # noqa: E402


def _drive_gate(monkeypatch, command, search_root):
    """Run the gate's main() over one payload, capturing its log calls."""
    spec = importlib.util.spec_from_file_location("xcred_gate", GATE)
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)

    calls = []
    monkeypatch.setattr(gate, "_gate_log_write",
                        lambda *a, **k: calls.append((a, k)))
    monkeypatch.setenv("WORLD_PATH", str(search_root))
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    try:
        gate.main()
    except SystemExit:
        pass
    return calls


def test_a_denied_command_logs_a_block(monkeypatch):
    with tempfile.TemporaryDirectory() as t:
        d = _tree(Path(t))
        calls = _drive_gate(monkeypatch, f"bash -x {d}/wrapper.sh", d.parent)
        decisions = [a[1] for a, _ in calls]
        assert "block" in decisions, f"expected a block firing, got {decisions}"


def test_the_override_bypass_is_logged(monkeypatch):
    """THE regression. Allowed is only half the contract; recorded is the other."""
    with tempfile.TemporaryDirectory() as t:
        d = _tree(Path(t))
        cmd = f'{pred.OVERRIDE_TOKEN}="auditing the bypass path" bash -x {d}/wrapper.sh'
        calls = _drive_gate(monkeypatch, cmd, d.parent)
        overrides = [(a, k) for a, k in calls if a[1] == "override"]
        assert overrides, f"bypass went unrecorded; decisions={[a[1] for a, _ in calls]}"
        assert overrides[0][1].get("override_reason"), "override needs a reason"
        assert "wrapper.sh" in (overrides[0][1].get("trigger_matched") or ""), (
            "the record must name what WOULD have been blocked")


def test_a_token_on_a_harmless_command_is_not_a_bypass(monkeypatch):
    """It logs the COUNTERFACTUAL, not the token's presence -- otherwise every
    command merely mentioning the token would register as a bypass."""
    with tempfile.TemporaryDirectory() as t:
        d = _tree(Path(t))
        calls = _drive_gate(monkeypatch, f"echo hi # {pred.OVERRIDE_TOKEN}", d.parent)
        assert not [1 for a, _ in calls if a[1] == "override"]


def test_an_ordinary_command_logs_nothing(monkeypatch):
    """noop is deliberately unlogged: this hook fires on EVERY Bash call."""
    with tempfile.TemporaryDirectory() as t:
        d = _tree(Path(t))
        assert _drive_gate(monkeypatch, "echo hello", d.parent) == []


def test_gate_id_is_registered_in_the_gate_registry():
    """_gate_log.log: 'gate_id MUST match an id in core/config/gates.yaml or the
    retirement evaluator will not see this gate's firings.'"""
    import yaml
    spec = importlib.util.spec_from_file_location("xcred_gate2", GATE)
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    registry = yaml.safe_load((CORE_SCRIPTS.parent / "config" / "gates.yaml").read_text(
        encoding="utf-8"))
    assert gate.GATE_ID in [g["id"] for g in registry["gates"]]
