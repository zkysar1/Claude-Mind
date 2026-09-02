"""Pin : the Layer-B gate for hook-bypassing git commits.

guard-901 is a Layer-A rule and rb-5390 records it failing by enumeration
leak (--no-verify named; `-c core.hooksPath=/dev/null` used all session).
These tests exercise the gate in the LITERAL production shape (guard-920):
the hook receives Claude Code's PreToolUse stdin JSON and answers with empty
stdout (approve) or a deny payload on stdout — exit code 0 either way.

Every bypass form the goal names is pinned, plus the false-positive surface
that matters most (guard-958): prose inside a quoted -m message that MENTIONS
the forbidden flags must not trip a token-anchored predicate.

The override ledger is redirected to a tmp path via GIT_HOOK_BYPASS_LEDGER —
same-session fresh-eyes lesson: a test that appends to the real store leaves
residue every suite run.
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

GATE = Path(__file__).resolve().parents[1] / "git-hook-bypass-gate.py"


def _run(command: str, tool_name: str = "Bash", ledger: Path | None = None):
    payload = {"tool_name": tool_name, "tool_input": {"command": command}}
    env = os.environ.copy()
    if ledger is not None:
        env["GIT_HOOK_BYPASS_LEDGER"] = str(ledger)
    proc = subprocess.run(
        [sys.executable, str(GATE)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr  # hook contract: never non-zero
    return proc.stdout.strip()


def _is_deny(stdout: str) -> bool:
    if not stdout:
        return False
    d = json.loads(stdout)
    return d["hookSpecificOutput"]["permissionDecision"] == "deny"


def _reason(stdout: str) -> str:
    return json.loads(stdout)["hookSpecificOutput"]["permissionDecisionReason"]


# ── Form A: --no-verify / short clusters ──────────────────────────────────

def test_no_verify_denied():
    out = _run('git commit --no-verify -m "msg"')
    assert _is_deny(out)
    assert "guard-901" in _reason(out)


def test_short_n_after_commit_denied():
    out = _run('git commit -n -m "msg"')
    assert _is_deny(out)


def test_short_cluster_with_n_denied():
    out = _run('git commit -anm "msg"')
    assert _is_deny(out)
    assert "no-verify-short" in _reason(out)


def test_git_log_dash_n_approved():
    # -n belongs to `git log`, no commit token — must not fire.
    assert _run("git log -n 5") == ""


def test_short_cluster_without_n_approved():
    assert _run('git commit -am "msg"') == ""


# ── Form B: core.hooksPath via -c / --config-env ──────────────────────────

def test_hookspath_dev_null_denied():
    # The literal rb-5390 incident form.
    out = _run('git -c core.hooksPath=/dev/null commit -m "msg"')
    assert _is_deny(out)
    assert "hookspath-c" in _reason(out)


def test_hookspath_glued_denied():
    out = _run('git -ccore.hooksPath=/tmp/x commit -m "msg"')
    assert _is_deny(out)


def test_hookspath_case_insensitive_denied():
    out = _run('git -c core.hookspath=/dev/null commit -m "msg"')
    assert _is_deny(out)


def test_hookspath_canonical_value_approved():
    # Explicitly re-stating the repo's correct chain is repair, not bypass.
    assert _run('git -c core.hooksPath=core/githooks commit -m "msg"') == ""


def test_config_env_denied():
    out = _run('git --config-env=core.hooksPath=HP commit -m "msg"')
    assert _is_deny(out)


# ── Form C: env-var equivalents ───────────────────────────────────────────

def test_git_config_parameters_env_denied():
    out = _run("GIT_CONFIG_PARAMETERS=\"'core.hookspath'='/dev/null'\" git commit -m x")
    assert _is_deny(out)


def test_git_config_key_env_denied():
    out = _run(
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath "
        "GIT_CONFIG_VALUE_0=/dev/null git commit -m x"
    )
    assert _is_deny(out)


# ── Form D: persistent config writes ──────────────────────────────────────

def test_config_write_denied():
    out = _run("git config core.hooksPath /dev/null")
    assert _is_deny(out)
    assert "config-write" in _reason(out)


def test_config_unset_denied():
    out = _run("git config --unset core.hooksPath")
    assert _is_deny(out)
    assert "config-unset" in _reason(out)


def test_config_read_approved():
    assert _run("git config core.hooksPath") == ""


def test_config_get_approved():
    assert _run("git config --get core.hooksPath") == ""


def test_config_restore_canonical_approved():
    assert _run("git config core.hooksPath core/githooks") == ""


#  (2026-09-02): Form D used to scan the FLAT token stream, so the
# token after `core.hooksPath` across a `;` / `&&` boundary was read as the
# VALUE and a read-only query was denied as a config-write with value ';'.
# It is now scoped to one simple command whose argv[0] is git, like Forms A-C.
def test_config_read_followed_by_separator_approved():
    assert _run("git config core.hooksPath; echo x") == ""
    assert _run('echo "=== hooksPath ==="; git config core.hooksPath; echo "=== hooks dir ==="') == ""
    assert _run("echo a && git config --get core.hooksPath && echo b") == ""


def test_config_write_after_separator_still_denied():
    out = _run("echo a; git config core.hooksPath /dev/null")
    assert _is_deny(out)
    assert "config-write" in _reason(out)


def test_config_unset_after_separator_still_denied():
    out = _run("echo a && git config --unset core.hooksPath")
    assert _is_deny(out)
    assert "config-unset" in _reason(out)


def test_config_token_in_unrelated_command_not_scanned():
    # `config` and `core.hooksPath` are grep ARGUMENTS here, with a real git call
    # on the same line: that simple command's argv[0] is grep, so Form D must
    # not arm (the flat scan read `notes.txt` as the config value and denied).
    assert _run("grep config core.hooksPath notes.txt; git status") == ""


# ── False-positive surface (guard-958: token-anchored, prose-safe) ─────────

def test_legitimate_commit_approved():
    assert _run('git add -A && git commit -m "fix(tests): normal commit"') == ""


def test_prose_mention_in_message_approved():
    # This very goal's commits mention the flags in the -m payload.
    out = _run(
        'git commit -m "feat(gate): deny --no-verify and core.hooksPath bypass forms"'
    )
    assert out == ""


def test_non_git_command_approved():
    assert _run("echo core.hooksPath --no-verify") == ""


def test_unbalanced_quotes_approved():
    assert _run('git commit -m "unterminated --no-verify') == ""


def test_non_bash_tool_approved():
    assert _run('git commit --no-verify -m x', tool_name="Edit") == ""


# ── Override hatch + ledger ───────────────────────────────────────────────

def test_override_with_justification_approved_and_logged(tmp_path):
    ledger = tmp_path / "overrides.jsonl"
    out = _run(
        'GIT_HOOK_BYPASS_OVERRIDE="repairing wedged pre-commit gate, chain cannot run" '
        'git commit --no-verify -m "hook repair"',
        ledger=ledger,
    )
    assert out == ""
    rows = [json.loads(l) for l in ledger.read_text().splitlines()]
    assert len(rows) == 1
    assert "repairing wedged" in rows[0]["justification"]
    assert "command_head" in rows[0] and "ts" in rows[0]


def test_override_empty_justification_denied(tmp_path):
    ledger = tmp_path / "overrides.jsonl"
    out = _run(
        'GIT_HOOK_BYPASS_OVERRIDE="" git commit --no-verify -m x',
        ledger=ledger,
    )
    assert _is_deny(out)
    assert "override-rejected" in _reason(out)
    assert not ledger.exists()  # nothing logged on a rejected override


def test_override_on_clean_command_writes_nothing(tmp_path):
    ledger = tmp_path / "overrides.jsonl"
    out = _run(
        'GIT_HOOK_BYPASS_OVERRIDE="not needed" git commit -m "clean"',
        ledger=ledger,
    )
    assert out == ""
    assert not ledger.exists()  # no findings → override path never reached


# ── Predicate unit surface (import via file location — hyphenated name) ────

def _load_gate_module():
    spec = importlib.util.spec_from_file_location("_ghbg", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_scan_command_no_git_short_circuits():
    mod = _load_gate_module()
    assert mod.scan_command("echo --no-verify") == []
    assert mod.scan_command("") == []


def test_scan_command_multiple_findings():
    mod = _load_gate_module()
    forms = [f for f, _ in mod.scan_command(
        'git -c core.hooksPath=/dev/null commit --no-verify -m x'
    )]
    assert "no-verify" in forms and "hookspath-c" in forms


# ── : `commit` must be git's OWN subcommand, not a bare token ────
#
# TWO false-positive instances, one defect. The gate matched `commit` anywhere in
# the FLAT token stream and then scanned every later token for a short cluster
# containing `n`:
#   1. a heredoc commit MESSAGE containing ordinary prose ("bash -n clean on
#      both") — rewording the prose, changing nothing else, was accepted;
#   2. a pipeline with NO git invocation at all, where `git` and `commit` both
#      arrived as quoted grep PATTERNS and `-rn` came from a third grep.
# Fix: split on shell separators, and arm Form A only for a simple command whose
# argv[0] basename is git and whose first non-option word is `commit`.

FALSE_POSITIVES = [
    # (label, command) — every one must APPROVE
    ("grep pipeline, no git invocation at all",
     'grep -n "commit" core/scripts/seed-transplant.sh | grep -i "git" | head -8'),
    ("heredoc message mentioning a bare -n",
     'git commit -F - <<EOF\nfix: bash -n clean on both\nEOF'),
    ("heredoc message mentioning --no-verify as prose",
     'git commit -F - <<EOF\ndocs: explain why --no-verify is banned\nEOF'),
    ("heredoc message mentioning core.hooksPath as prose",
     'git commit -F - <<EOF\ndocs: core.hooksPath=/dev/null is the rb-5390 form\nEOF'),
    ("commit as a --grep VALUE on git log",
     'git log --grep=commit -n 5'),
    ("commit as prose in an echo, -n on an unrelated command",
     'echo "remember to commit" ; ls -n'),
    ("sed -n before a word 'commit' in an echo",
     'sed -n "1,5p" f.txt && echo "commit done"'),
    ("multi-line quoted message containing sed -n",
     'git commit -m "line one\nsed -n stuff"'),
]

# Must still DENY. The narrowing is the risk, so the true-positive set is pinned
# at least as hard as the false-positive one — including bypasses positioned
# AFTER a separator, after a heredoc, and on their own LINE, each of which the
# simple-command split could plausibly have dropped.
TRUE_POSITIVES = [
    ("plain --no-verify", 'git commit --no-verify -m "msg"'),
    ("short -n", 'git commit -n -m "msg"'),
    ("short cluster -anm", 'git commit -anm "msg"'),
    ("global -C dir before subcommand", 'git -C /repo commit -n -m x'),
    ("after a && separator", 'ls -la && git commit -n -m "sneaky"'),
    ("on its own LINE", 'cd /repo\ngit commit -n -m x'),
    ("after a TERMINATED heredoc", 'git commit -F - <<EOF\nmsg\nEOF\ngit commit -n -m x'),
    ("after an UNTERMINATED heredoc (nothing may be stripped)",
     'cat <<EOF\nnever terminated\ngit commit -n -m x'),
    ("after a <<- indented-terminator heredoc",
     'git commit -F - <<-EOF\n\tmsg\n\tEOF\ngit commit --no-verify -m x'),
    ("after a quoted-delimiter heredoc",
     "git commit -F - <<'EOF'\nmsg\nEOF\ngit commit -n -m x"),
    ("flag BEFORE the heredoc on the same line", 'git commit -n -F - <<EOF\nmsg\nEOF'),
    ("multi-line quoted message then -n", 'git commit -m "line one\nline two" -n'),
]


def test_g4695_false_positives_approve():
    mod = _load_gate_module()
    offenders = [(lbl, mod.scan_command(cmd))
                 for lbl, cmd in FALSE_POSITIVES if mod.scan_command(cmd)]
    assert not offenders, f"these must not deny: {offenders}"


def test_g4695_true_positives_still_deny():
    mod = _load_gate_module()
    missed = [lbl for lbl, cmd in TRUE_POSITIVES if not mod.scan_command(cmd)]
    assert not missed, (
        f"the narrowing went too far — these bypasses now PASS: {missed}"
    )


def test_g4695_incident_command_in_production_shape():
    """The literal second-instance command, through the real hook (guard-920)."""
    assert _run(
        'grep -n "commit" core/scripts/seed-transplant.sh | grep -i "git" '
        '| head -8; echo ---; grep -rn "def .*commit" core/scripts/'
    ) == ""


def test_g4695_heredoc_prose_in_production_shape():
    """The originating incident, through the real hook."""
    assert _run('git commit -F - <<EOF\nfix: bash -n clean on both\nEOF') == ""


def test_g4695_newline_is_a_command_boundary():
    """Regression pin for a hole the narrowing OPENED and adversarial probing caught.

    shlex eats newlines as whitespace, so without re-inserting a separator a
    `git commit -n` on its own line joins the PREVIOUS line's argv — argv[0] is
    then that line's command, not git, and a real bypass approves. The old flat
    scan caught this case incidentally; the fix had to keep it.
    """
    mod = _load_gate_module()
    assert mod.scan_command('cd /repo\ngit commit -n -m x'), \
        "a bypass on its own line must still deny"
    assert mod.scan_command('echo hi\ngit commit --no-verify -m x'), \
        "a --no-verify on its own line must still deny"


def test_g4695_multiline_message_does_not_swallow_the_next_command():
    """The separator jump must be APPORTIONED between quoted and real newlines.

    Found in fresh-eyes review of the 4695 fix itself. `lex.lineno` lags, and a
    multi-line QUOTED token swallows its own line breaks AND the trailing
    separator newline in ONE step — so the jump counts both. The first fix
    tested only "does this token contain a newline?", which threw the real
    separator away with the embedded ones and MERGED the next command into the
    commit's argv.

    Measured on `git commit -m "a\\nb"\\nsort -n f`: jump=2 (one embedded, one
    real), no separator emitted, tokens
    ['git','commit','-m','a\\nb','sort','-n','f'] — so a benign `sort -n` was
    read as a flag on `git commit` and DENIED. That is precisely the
    false-positive class this gate was narrowed to remove, firing on the most
    ordinary shape there is: every multi-line commit message in this repo,
    including the ones iteration-commit.sh writes. The single-line case stayed
    correct throughout, which is why it hid.

    Both directions are pinned: the benign command must survive the multi-line
    message, and a real bypass after one must still be caught.
    """
    mod = _load_gate_module()
    for n, cmd in enumerate((
            'git commit -m "line1\nline2"\nsort -n /tmp/f',
            'git commit -m "a\nb"\nhead -n 5 /tmp/f',
            'git commit -m "a\nb\nc"\nsort -n f',
            'git commit -m "a\nb" && sort -n f',
    )):
        assert mod.scan_command(cmd) == [], \
            f"case {n}: a benign -n after a multi-line commit message must approve"

    for n, cmd in enumerate((
            'git commit -m "a\nb"\ngit commit -n -m x',
            'git commit -m "a\nb"\ngit commit --no-verify -m y',
            'git commit -m "a\nb" && git commit -n -m z',
    )):
        assert mod.scan_command(cmd), \
            f"case {n}: a real bypass after a multi-line message must still deny"

    # The mechanism itself, so a future edit cannot satisfy the cases above by
    # accident: the separator IS emitted, and exactly once.
    toks = mod._tokenize('git commit -m "a\nb"\nsort -n f')
    assert toks.count(";") == 1, f"expected exactly one separator, got {toks}"
    assert toks[toks.index(";") + 1] == "sort", \
        f"the separator must land BEFORE the next command, got {toks}"
