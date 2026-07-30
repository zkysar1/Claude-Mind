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


# ── L5 response-field lane () ────────────────────────────────────
#
# The lane checks that every name a `Parse <var>: <names>` line reads out of a
# script's JSON actually appears as a quoted key in that script — following a
# daemon-only wrapper through its `rt_call <M> <route>` to the module that
# registers the route, because such a wrapper emits nothing itself.

PARSE_RE = _mod._PARSE_LINE_RE


def _fields_flagged(var, body, var_script_map):
    return {f["stale_ref"].split()[0]
            for f in _mod.check_response_fields(1, var, body, var_script_map)}


def test_parse_line_re_matches_var_colon_fields():
    """The sibling matcher catches the shape _CHECK_LINE_RE structurally cannot:
    a field-parse instruction is neither a `Check:` nor a `Bash:` line."""
    m = PARSE_RE.match("    Parse eval_json: configured, all_passed, gates")
    assert m, "Parse <var>: <fields> not matched"
    assert m.group(1) == "eval_json"
    assert "all_passed" in m.group(2)


def test_parse_line_re_ignores_fieldless_prose():
    """`Parse the JSON result.` names no var and no fields — matching it would
    make every field-token in the sentence a candidate. 42 of the corpus's 46
    Parse lines are this shape, so a loose matcher is nearly all false positive."""
    for line in ("Parse the JSON result.", "Parse verdict JSON.", "Parse:"):
        assert not PARSE_RE.match(line), f"fieldless prose wrongly matched: {line!r}"


def test_e2e_bogus_field_is_flagged():
    """A name no emitter writes is stale — the lane's reason for existing."""
    vmap = {"eval_json": "curriculum-evaluate.sh"}
    flagged = _fields_flagged("eval_json", "configured, all_passed, zzz_never_emitted", vmap)
    assert "zzz_never_emitted" in flagged, f"bogus field not flagged: {flagged}"


def test_e2e_real_field_behind_daemon_only_wrapper_not_flagged():
    """Real fields emitted by the ENDPOINT, not the wrapper, must NOT be flagged.

    curriculum-evaluate.sh is daemon-only: its whole body is an rt_call, so it
    contains none of these names. Checking only the wrapper reports every field
    stale — the lane must follow the route to mind_api/src/endpoints/curriculum.py.
    """
    vmap = {"eval_json": "curriculum-evaluate.sh"}
    flagged = _fields_flagged("eval_json", "configured, all_passed, current_stage, gates", vmap)
    assert not flagged, f"real endpoint-emitted fields wrongly flagged: {flagged}"


def test_e2e_route_registered_outside_endpoints_dir_resolves():
    """Regression guard for the subpackage blind spot.

    `/v1/team-state/read` registers in mind_api/src/world/team_state.py, NOT in
    mind_api/src/endpoints/. A non-recursive endpoints-only glob resolved to []
    — indistinguishable from "no emitter writes this field" — so live team-state
    fields were reported stale. Resolution walks mind_api/src recursively.
    """
    assert _mod._resolve_endpoints("/v1/team-state/read"), \
        "route registered outside endpoints/ no longer resolves — subpackage walk lost"
    vmap = {"st": "team-state-read.sh"}
    flagged = _fields_flagged("st", "agent_status", vmap)
    assert "agent_status" not in flagged, \
        f"live team-state field wrongly flagged stale: {flagged}"


def test_scan_wires_l5_end_to_end(tmp_path):
    """scan() must actually CALL the lane — unit-testing check_response_fields in
    isolation proves the lane works, not that it runs. The wiring (extract the
    Parse lines, build the var->script map, feed both in) is a separate failure
    surface: drop any one of the three and every unit test above still passes
    while the scanner reports clean forever.
    """
    skill = tmp_path / "SKILL.md"
    skill.write_text(
        "# fixture\n"
        "   Bash: eval_json=$(bash core/scripts/curriculum-evaluate.sh)\n"
        "   Parse eval_json: configured, all_passed, zzz_never_emitted\n",
        encoding="utf-8")

    result = _mod.scan(skill)

    assert result["parse_lines_scanned"] == 1, \
        f"Parse line not extracted by scan(): {result}"
    l5 = [f for f in result["findings"] if f["lane"] == "L5_response_field"]
    assert [f["stale_ref"].split()[0] for f in l5] == ["zzz_never_emitted"], \
        f"L5 not wired into scan(), or fired wrongly: {l5}"


def test_wrapped_parse_list_is_joined(tmp_path):
    """A field list that WRAPS must be scanned whole (fresh-eyes, ).

    Both real corpus instances wrap. Matching per-line captured only through the
    trailing comma, so the continuation's fields were never checked — silent
    under-coverage, which reads identically to clean because fewer fields checked
    just means fewer findings.
    """
    skill = tmp_path / "SKILL.md"
    skill.write_text(
        "   Bash: eval_json=$(bash core/scripts/curriculum-evaluate.sh)\n"
        "   Parse eval_json: configured, all_passed,\n"
        "                    current_stage, zzz_wrapped_bogus\n",
        encoding="utf-8")

    result = _mod.scan(skill)
    assert result["parse_lines_scanned"] == 1, "wrap produced two entries, not one"
    flagged = {f["stale_ref"].split()[0] for f in result["findings"]}
    assert "zzz_wrapped_bogus" in flagged, \
        f"continuation-line field never scanned: {flagged}"
    assert "current_stage" not in flagged, \
        f"real continuation field wrongly flagged: {flagged}"


def test_parse_continuation_stops_at_next_construct(tmp_path):
    """The comma rule must not swallow the following Bash:/Parse:/# line.

    A list ending exactly at a line boundary is the dangerous case: absorb the
    next line and a comment's prose words become candidate field names.
    """
    skill = tmp_path / "SKILL.md"
    skill.write_text(
        "   Bash: eval_json=$(bash core/scripts/curriculum-evaluate.sh)\n"
        "   Parse eval_json: configured, all_passed,\n"
        "   # zzz_comment_token must not be read as a field\n"
        "   Bash: other=$(bash core/scripts/team-state-read.sh)\n",
        encoding="utf-8")

    flagged = {f["stale_ref"].split()[0] for f in _mod.scan(skill)["findings"]}
    assert "zzz_comment_token" not in flagged, \
        f"continuation swallowed a comment line: {flagged}"


def test_all_skills_aggregate_reports_parse_line_coverage():
    """--all-skills is the mode the recurring audit runs; it must carry L5's
    coverage counter. Without it the lane can scan ZERO lines corpus-wide and
    report the same clean result as one that scanned every line."""
    result = _mod.scan_all_skills()
    assert "parse_lines_scanned_total" in result, \
        "aggregate dropped L5 coverage — a 0-scan lane is indistinguishable from a clean one"
    assert isinstance(result["parse_lines_scanned_total"], int)
    sample = next(iter(result["per_skill"].values()))
    assert "parse_lines_scanned" in sample, "per_skill row dropped L5 coverage"


def test_unknown_var_is_not_flagged():
    """No `Bash: var=$(script)` binding means no output to check against.
    Unverifiable is NOT stale — the precision choice that keeps the lane quiet
    enough to stay wired in."""
    assert not _fields_flagged("mystery", "alpha, beta", {}), \
        "unbound var produced findings — lane will drown in false positives"


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
