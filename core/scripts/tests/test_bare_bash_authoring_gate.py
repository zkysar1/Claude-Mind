"""Tests for core/scripts/bare-bash-authoring-gate.py (goal ).

The PreToolUse[Bash] half of the guard-580 defense: refuses inline
``python -c`` / ``py -3 -c`` payloads that build a subprocess argv with a bare
``"bash"`` argv[0]. rb-5255 records that ad-hoc one-off code is where the
pattern actually returns, so this layer — not the pre-commit gate — is the one
that would have caught the reintroduction.

Two properties matter most and are pinned hardest:
  1. it DENIES a genuine inline violation (else the layer is decorative);
  2. it FAILS OPEN on everything else. This hook runs on EVERY Bash call in
     the loop, so a false positive or a crash is far more expensive than a
     miss. Every not-a-violation path must produce empty stdout + exit 0.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parents[1] / "bare-bash-authoring-gate.py"
REPO = Path(__file__).resolve().parents[3]


def _run(payload) -> subprocess.CompletedProcess:
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(
        [sys.executable, str(GATE)],
        input=body, capture_output=True, text=True, cwd=str(REPO), timeout=120,
    )


def _bash(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def _assert_approved(r: subprocess.CompletedProcess, why: str) -> None:
    assert r.returncode == 0, f"{why}: rc={r.returncode}"
    assert r.stdout.strip() == "", f"{why}: expected empty stdout, got {r.stdout!r}"


def _assert_denied(r: subprocess.CompletedProcess) -> dict:
    assert r.returncode == 0, "hook contract: deny is exit 0 + JSON, not nonzero"
    assert r.stdout.strip(), "expected a deny payload on stdout"
    out = json.loads(r.stdout)
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"
    return hso


# --- 1. genuine violations MUST be denied ----------------------------------

@pytest.mark.parametrize("cmd", [
    'py -3 -c "import subprocess; subprocess.run([\'bash\', \'x.sh\'])"',
    'python3 -c "import subprocess; subprocess.run([\'bash\', \'x.sh\'])"',
    'python -c "import subprocess; subprocess.run([\'bash\', \'x.sh\'])"',
    'py -3 -c "cmd = [\'bash\'] + rest"',                      # form (b)
    'py -3 -c "import subprocess; subprocess.run(\'bash x.sh\', shell=True)"',  # form (c)
])
def test_inline_violation_is_denied(cmd):
    hso = _assert_denied(_run(_bash(cmd)))
    reason = hso["permissionDecisionReason"]
    assert "guard-580" in reason
    assert "_runtime_bash" in reason, "deny must name the sanctioned resolver"


def test_deny_reason_names_the_escape_hatch():
    hso = _assert_denied(_run(_bash('py -3 -c "import subprocess; subprocess.run([\'bash\'])"')))
    assert "allow-bare-bash" in hso["permissionDecisionReason"]


def test_violation_is_caught_after_interpreter_flags():
    """`py -3 -c` puts a flag between interpreter and -c; walk-back must handle it."""
    _assert_denied(_run(_bash('py -3 -c "import subprocess; subprocess.run([\'bash\', \'a\'])"')))


# --- 2. everything else MUST fail open ------------------------------------

def test_marker_inside_payload_approves():
    cmd = ('py -3 -c "import subprocess; subprocess.run([\'bash\', \'x\'])  '
           '# allow-bare-bash: linux-only one-off"')
    _assert_approved(_run(_bash(cmd)), "override marker must suppress")


@pytest.mark.parametrize("cmd,why", [
    ('bash core/scripts/retrieve.sh --category x', "plain bash command, no inline python"),
    ('py -3 -c "import subprocess; subprocess.run([BASH, \'x.sh\'])"', "sanctioned BASH form"),
    ('py -3 -c "print(1)"', "inline python with no subprocess"),
    ('grep -c bash file.txt', "-c belongs to grep, not an interpreter"),
    ('sort -c bash.txt', "-c belongs to sort"),
    ('py -3 -c "unterminated', "unbalanced quotes — shlex cannot parse"),
    ('py -3 core/scripts/x.py', "script mode, no -c"),
    ('echo "subprocess.run([\'bash\'])"', "the pattern as echoed text, not executed python"),
    ('py -3 -c "def f(:"', "payload is not parseable python"),
    ('', "empty command"),
])
def test_non_violations_fail_open(cmd, why):
    _assert_approved(_run(_bash(cmd)), why)


@pytest.mark.parametrize("payload,why", [
    ({"tool_name": "Read", "tool_input": {"file_path": "/x"}}, "non-Bash tool"),
    ({"tool_name": "Bash"}, "missing tool_input"),
    ({"tool_name": "Bash", "tool_input": {}}, "missing command"),
    ({"tool_name": "Bash", "tool_input": {"command": 42}}, "command wrong type"),
    ({}, "empty payload"),
    ("not json at all", "unparseable stdin"),
    ("[1,2,3]", "json that is not an object"),
])
def test_malformed_input_fails_open(payload, why):
    _assert_approved(_run(payload), why)


def test_prose_mention_in_inline_python_is_not_denied():
    """A docstring mention inside a one-off must not trip the gate."""
    cmd = 'py -3 -c "\'\'\'we used to call subprocess.run([\\\'bash\\\', ...]) here\'\'\'"'
    _assert_approved(_run(_bash(cmd)), "docstring prose in inline python")


# --- 3. the two layers share ONE detection implementation ------------------

def test_engine_is_shared_not_reimplemented():
    """The hook must delegate detection, not carry a second copy.

    Two implementations would drift: a form added to one would silently stay
    uncovered in the other, which is how form (b) survived the first sweep.
    """
    src = GATE.read_text(encoding="utf-8")
    assert "check-no-bare-bash.py" in src, "hook must load the shared engine"
    assert "scan_source" in src, "hook must call the engine's detector"
    assert "ast.parse" not in src, "hook must NOT re-implement AST detection"


# --- 6. HEREDOC-fed payloads ( follow-on, 2026-07-27) -------------
#
# The `-c` extractor is blind to `py -3 - <<'PY'`, and that is the form
# multi-line ad-hoc Python actually takes (a `-c` payload cannot carry newlines
# comfortably). Measured against this gate BEFORE the fix: the `-c` spelling
# DENIED and every heredoc spelling was APPROVED.
#
# The gap mattered. This gate exists because of rb-5255 — the author
# reintroduced bare-bash within an hour of sweeping it from 12 sites. On
# 2026-07-26 the same author reintroduced it three MORE times in one session,
# every one a heredoc, so the gate built to catch exactly this never fired.
# The rule was right and the gate was right; only the aim was wrong. An
# authoring-time gate is worth precisely the forms it actually covers.

@pytest.mark.parametrize("cmd", [
    "py -3 - <<'PY'\nimport subprocess\nsubprocess.run([\"bash\", \"x.sh\"])\nPY",
    "python3 - <<'EOF'\nimport subprocess\np = subprocess.run([\"bash\", str(h)], capture_output=True)\nEOF",
    "py -3 - <<PY\nimport subprocess\nsubprocess.run([\"bash\", \"a\"])\nPY",          # unquoted delimiter
    'py -3 - <<"PY"\nimport subprocess\nsubprocess.run(["bash", "a"])\nPY',            # double-quoted delimiter
    "py -3 -u - <<'PY'\nimport subprocess\nsubprocess.run([\"bash\", \"a\"])\nPY",     # interpreter flags
])
def test_heredoc_violation_is_denied(cmd):
    hso = _assert_denied(_run(_bash(cmd)))
    assert "guard-580" in hso["permissionDecisionReason"]


@pytest.mark.parametrize("cmd,why", [
    ("py -3 - <<'PY'\nfrom _runtime_bash import BASH\nimport subprocess\n"
     "subprocess.run([BASH, \"x.sh\"])\nPY",
     "the sanctioned resolver must never be flagged"),
    ("py -3 - <<'PY'\nprint('hello bash')\nPY",
     "a prose mention of bash is not an argv"),
    ("cat > f.txt <<'EOF'\nsubprocess.run([\"bash\"])\nEOF",
     "a NON-python heredoc must not be scanned as python"),
    ("bash core/scripts/x.sh && echo done",
     "an ordinary bash invocation is not inline python"),
])
def test_heredoc_non_violations_fail_open(cmd, why):
    assert _run(_bash(cmd)).stdout.strip() == "", why


@pytest.mark.parametrize("payload_line,expected_denied", [
    ('subprocess.run(["bash", "x"])  # allow-bare-bash: posix', False),
    ('subprocess.run(["bash", "x"])', True),
])
def test_heredoc_honors_the_escape_hatch(payload_line, expected_denied):
    """The line/file markers must behave identically on the heredoc path —
    otherwise the documented escape hatch silently stops working for the form
    people actually write."""
    cmd = "py -3 - <<'PY'\nimport subprocess\n" + payload_line + "\nPY"
    denied = _run(_bash(cmd)).stdout.strip() != ""
    assert denied is expected_denied
