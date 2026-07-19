"""Regression tests for verify-learning-staleness.py L4 (argparse-flag lane).

Locks the g-115-2196 fix: `_BASH_SCRIPT_RE` was blind to the
`Bash: var=$(bash <script> ...)` command-substitution form, so stale flags on
the highest-stakes call sites (dedup guards that fail open into filing a
DUPLICATE goal) were never checked. The fix adds an OPTIONAL command-sub
assignment prefix BEFORE the runner without weakening the load-bearing
arg-tail guard `[^|&;<>(\\n]*` (rb-3437 / guard-1081 — a guard is an interface;
widening the PREFIX must not weaken the TAIL).

The three acceptance criteria (from g-115-2196):
  (1) command-sub form  -> `--status` is reported stale for aspirations-query.sh
  (2) plain form        -> behavior UNCHANGED (no regression)
  (3) piped downstream  -> `-q` is STILL NOT attributed to the upstream script
      (the arg-tail guard, which also excludes parenthesized prose, is intact)
"""
import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "verify-learning-staleness.py"
_spec = importlib.util.spec_from_file_location("verify_learning_staleness", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

RE = _mod._BASH_SCRIPT_RE


def _match(line):
    """Return (script_basename, args_tail) or (None, None) for a full Bash: line."""
    m = RE.match(line)
    if not m:
        return None, None
    return m.group(1).rsplit("/", 1)[-1], (m.group(2) or "").strip()


# ── Regex-level structural tests (the load-bearing fix, script-source-agnostic) ──

def test_command_sub_form_is_matched_and_flags_captured():
    """C1: the var=$(bash <script> ...) form now matches, script + flags captured."""
    line = ('Bash: existing=$(bash core/scripts/aspirations-query.sh '
            '--status pending,in-progress --contains "defer-drift")')
    script, args = _match(line)
    assert script == "aspirations-query.sh", f"script not captured: {script!r}"
    assert "--status" in args, f"--status not in args tail: {args!r}"


def test_plain_form_unchanged():
    """C2: the plain `Bash: bash <script> --flag` form matches exactly as before."""
    line = "Bash: bash core/scripts/foo.sh --flag arg"
    script, args = _match(line)
    assert script == "foo.sh"
    assert "--flag" in args


def test_piped_downstream_flag_not_attributed():
    """C3 (STOPS THE OVER-FIX): -q after a pipe must NOT reach the args tail."""
    line = "Bash: bash core/scripts/foo.sh | grep -q x"
    script, args = _match(line)
    assert script == "foo.sh"
    assert "-q" not in args, f"arg-tail guard leaked the piped -q: {args!r}"


def test_command_sub_then_pipe_tail_guard_holds():
    """C3 composite: var=$(bash foo.sh --a) | grep -q x -> --a captured, -q excluded."""
    line = "Bash: v=$(bash core/scripts/foo.sh --a) | grep -q x"
    script, args = _match(line)
    assert script == "foo.sh"
    assert "--a" in args
    assert "-q" not in args


def test_parenthesized_prose_excluded():
    """The `(` guard still stops the args tail at a prose comment paren."""
    line = "Bash: wm-read.sh encoding_queue --json  (if --selective mode)"
    script, args = _match(line)
    assert script == "wm-read.sh"
    assert "--json" in args
    assert "--selective" not in args, f"paren-prose leaked into args: {args!r}"


def test_env_var_prefix_not_matched_as_command_sub():
    """A plain env-var prefix (FOO=bar, no `$(`) must NOT satisfy the command-sub
    prefix — it remains an out-of-scope known limitation, not a false match."""
    line = "Bash: FOO=bar bash core/scripts/foo.sh --flag"
    script, _ = _match(line)
    assert script is None, "env-var prefix should not match (no $( )"


# ── End-to-end tests via check_argparse_flags against the real aspirations-query.sh ──

def _flags_flagged(body):
    return {f["stale_ref"].split()[0] for f in _mod.check_argparse_flags(1, body)}


def test_e2e_command_sub_stale_status_flagged():
    """C1 canonical: command-sub `--status` on aspirations-query.sh is now stale.
    aspirations-query.sh accepts --goal-status/--goal-field/--title-contains, NOT
    --status/--contains — the exact drift this lane must catch."""
    body = ('existing=$(bash core/scripts/aspirations-query.sh '
            '--status pending,in-progress --contains "defer-drift")')
    flagged = _flags_flagged(body)
    assert "--status" in flagged, f"--status not flagged stale: {flagged}"


def test_e2e_command_sub_correct_flags_not_flagged():
    """The FIXED call-site form (--goal-status/--goal-field) produces NO stale
    finding — the detector passes the corrected sites (non-vacuous confirmation)."""
    body = ('existing=$(bash core/scripts/aspirations-query.sh '
            '--goal-status pending,in-progress --goal-field origin_signal "defer-drift-audit")')
    flagged = _flags_flagged(body)
    assert "--goal-status" not in flagged and "--goal-field" not in flagged, \
        f"valid flags wrongly flagged: {flagged}"


def test_e2e_piped_downstream_not_attributed():
    """C3 canonical end-to-end: a piped `grep -q` flag is not attributed to the
    upstream script, so no spurious stale finding for -q."""
    body = "bash core/scripts/aspirations-query.sh --goal-status pending | grep -q x"
    flagged = _flags_flagged(body)
    assert "-q" not in flagged, f"-q wrongly attributed to aspirations-query.sh: {flagged}"


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
