"""test_pending_questions_read.py — regression for .

pending-questions-read.sh is the ONE shape-tolerant read entry point that the
user-facing consumers (agent-completion-report, open-questions, respond) route
through instead of hand-rolling a naive top-level `status == "pending"` scan.
A naive scan silently SKIPS entries nested inside a `{questions: [...]}` wrapper
element (shapes A and B) — the invisibility bug this reader eliminates.

The reader MUST flatten the same on-disk container shapes its sibling
pending-questions-sweep.py::_load_questions reads (rb-1786):

    shape A:   {"questions": [ {...}, ... ]}            dict wrapper
    shape B:   [ {"questions": [ {...}, ... ]}, ... ]   list with wrapper element
    shape C:   [ {"id": ...}, {"id": ...}, ... ]        bare list of entry dicts
    mixed B+C: a list carrying BOTH a wrapper element and bare entry dicts

The reader is a thin bash wrapper over an inline `python3 - <<'PYEOF'` heredoc;
this test invokes the real wrapper as a subprocess against an isolated tmp file
via --pq-path (guard-692/guard-759: never touch the real agent file).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from _bash_helpers import BASH  # : bare "bash" hits the System32 WSL launcher outside pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
READ_SCRIPT = CORE_SCRIPTS / "pending-questions-read.sh"


def _env(**overrides) -> dict:
    """Ambient env with MIND_* stripped, plus explicit overrides.

    NOT a hand-built {"PATH": "/usr/bin:/bin:/usr/local/bin"} (g-115-3197).
    A POSIX-only PATH HANGS Git Bash on win32: the wrapper never returns, and
    subprocess.run(timeout=) does NOT bound it — Python kills the child, then
    re-enters communicate(), which waits on a pipe a surviving grandchild still
    holds. The hang therefore rides pytest's 600s faulthandler bound and ABORTS
    THE WHOLE CHUNK, silently deleting coverage for every test scheduled after
    it. Measured: chunk 02 of a 4-chunk full-suite run died at 51%, ~700 tests
    never ran, and the run still reported a plausible-looking pass count.

    Proven by controlled comparison — same script, args and cwd, ONLY env
    differing:
        POSIX-only PATH + SystemRoot/COMSPEC/WINDIR -> HUNG (>15s)
        minimal env but REAL Windows PATH           -> rc=0 in 2.61s
        os.environ.copy() + overrides               -> rc=0 in 2.54s
    So PATH is the discriminating variable, NOT the Windows essentials — the
    obvious "it must need SystemRoot" hypothesis is wrong and was falsified
    before this fix was written.

    MIND_* is stripped rather than passed through so the no-agent-binding test
    still gets a genuinely unbound env: under pytest the ambient environment
    always carries MIND_AGENT (conftest sets it), which would otherwise make
    that test silently stop testing what it names.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("MIND_")}
    env.update(overrides)
    return env


def _write(pq_path: Path, data) -> None:
    with open(pq_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)


def _run(pq_path: Path, *args: str):
    """Run the real wrapper against pq_path, returning (rc, parsed_json)."""
    r = subprocess.run(
        [BASH, str(READ_SCRIPT), "--pq-path", str(pq_path), *args],
        env=_env(MIND_AGENT="foxtrot"),
        capture_output=True, text=True,
    )
    parsed = None
    if r.stdout.strip():
        parsed = json.loads(r.stdout.strip().splitlines()[-1])
    return r.returncode, parsed


def _ids(entries):
    return [e.get("id") for e in (entries or [])]


# --- Shape A: dict wrapper — the entry is nested, naive scan would miss it ---

def test_shape_a_dict_wrapper_pending_surfaces():
    with tempfile.TemporaryDirectory() as tmpd:
        pq = Path(tmpd) / "pq.yaml"
        _write(pq, {"questions": [{"id": "pq-a", "status": "pending"}]})
        rc, out = _run(pq, "--status", "pending")
        assert rc == 0
        assert _ids(out) == ["pq-a"], "wrapper-nested pending entry must surface"


# --- Shape B: list with a wrapper element ---

def test_shape_b_wrapper_element_pending_surfaces():
    with tempfile.TemporaryDirectory() as tmpd:
        pq = Path(tmpd) / "pq.yaml"
        _write(pq, [{"questions": [{"id": "pq-b", "status": "pending"}]}])
        rc, out = _run(pq, "--status", "pending")
        assert rc == 0
        assert _ids(out) == ["pq-b"]


# --- Shape C: bare list ---

def test_shape_c_bare_list_pending_surfaces():
    with tempfile.TemporaryDirectory() as tmpd:
        pq = Path(tmpd) / "pq.yaml"
        _write(pq, [{"id": "pq-c", "status": "pending"}])
        rc, out = _run(pq, "--status", "pending")
        assert rc == 0
        assert _ids(out) == ["pq-c"]


# --- mixed B+C: status filter must see BOTH the wrapped and the bare entry ---

def test_mixed_shape_status_filter_sees_both():
    with tempfile.TemporaryDirectory() as tmpd:
        pq = Path(tmpd) / "pq.yaml"
        _write(pq, [{"questions": [{"id": "pq-wrapped", "status": "pending"}]},
                    {"id": "pq-bare", "status": "pending"}])
        rc, out = _run(pq, "--status", "pending")
        assert rc == 0
        assert set(_ids(out)) == {"pq-wrapped", "pq-bare"}


# --- status filter excludes non-matching entries ---

def test_status_filter_excludes_non_pending():
    with tempfile.TemporaryDirectory() as tmpd:
        pq = Path(tmpd) / "pq.yaml"
        _write(pq, {"questions": [{"id": "pq-p", "status": "pending"},
                                  {"id": "pq-r", "status": "resolved"}]})
        rc, out = _run(pq, "--status", "pending")
        assert rc == 0
        assert _ids(out) == ["pq-p"]


# --- type filter (respond's priority-review / fresh-eyes-review surfacing) ---

def test_type_and_status_filter():
    with tempfile.TemporaryDirectory() as tmpd:
        pq = Path(tmpd) / "pq.yaml"
        _write(pq, {"questions": [
            {"id": "pq-pr", "status": "pending", "type": "priority-review"},
            {"id": "pq-fe", "status": "pending", "type": "fresh-eyes-review"},
        ]})
        rc, out = _run(pq, "--type", "priority-review", "--status", "pending")
        assert rc == 0
        assert _ids(out) == ["pq-pr"]


# --- no filter returns all flattened entries ---

def test_no_filter_returns_all():
    with tempfile.TemporaryDirectory() as tmpd:
        pq = Path(tmpd) / "pq.yaml"
        _write(pq, [{"questions": [{"id": "pq-1", "status": "pending"}]},
                    {"id": "pq-2", "status": "resolved"}])
        rc, out = _run(pq)
        assert rc == 0
        assert set(_ids(out)) == {"pq-1", "pq-2"}


# --- missing file: NORMAL empty state → [] + exit 0 (matches canonical
#     _load_questions; a missing pending-questions.yaml is not an error).
#      fresh-eyes-code self-review corrected this from exit 2. ------

def test_missing_file_returns_empty_exit_0():
    with tempfile.TemporaryDirectory() as tmpd:
        pq = Path(tmpd) / "does-not-exist.yaml"
        rc, out = _run(pq, "--status", "pending")
        assert rc == 0, "missing file is a normal empty state, not an input error"
        assert out == []


# --- malformed YAML: fail-open (exit 0, empty array) ---

def test_malformed_yaml_fails_open():
    with tempfile.TemporaryDirectory() as tmpd:
        pq = Path(tmpd) / "pq.yaml"
        pq.write_text("{ this is: : not valid yaml :\n  - broken", encoding="utf-8")
        rc, out = _run(pq, "--status", "pending")
        assert rc == 0, "malformed YAML must fail-open, never block a consumer"
        assert out == []


# --- default path (no --pq-path): must resolve MIND_AGENT, never crash on
#     `set -u` with an unbound $AGENT.  fresh-eyes follow-up — every
#     other test passes --pq-path, so the agent-resolution branch was untested
#     and shipped an "AGENT: unbound variable" crash that broke every REAL
#     consumer call (none pass --pq-path). ---------------------------------

def test_default_path_no_agent_clean_exit_not_unbound_crash():
    r = subprocess.run(
        [BASH, str(READ_SCRIPT), "--status", "pending"],
        env=_env(),  # deliberately NO MIND_AGENT — _env strips MIND_*
        capture_output=True, text=True,
    )
    assert "unbound variable" not in r.stderr, (
        f"reader crashed on set -u instead of a clean diagnostic: {r.stderr!r}"
    )
    assert r.returncode == 2, "no agent binding + no --pq-path must exit 2"
    assert r.stdout.strip().splitlines()[-1] == "[]", "must still print [] for consumers"


# --- --prefix id filter () ------------------------------------
#
# aspirations-evolve Step 0.5b's anti-stacking guard passed `--prefix
# l1-taxonomy-` to a parser that did NOT accept it: the `*)` arm fired,
# exit 2, EMPTY stdout. The call site read that empty output as "no
# l1-taxonomy question is open" and never SKIPped — so the guard was
# VACUOUS and the "do not stack proposals" invariant was unenforced
# (guard-487: a suppression gate whose probe cannot match fails OPEN).
# Measured at fix time: 2 l1-taxonomy- questions were open fleet-wide,
# so the guard should have been suppressing and was not.


def test_prefix_flag_is_accepted_not_unknown_arg():
    """THE  regression: --prefix must not fall through to `*)`."""
    with tempfile.TemporaryDirectory() as tmpd:
        pq = Path(tmpd) / "pq.yaml"
        _write(pq, [{"id": "l1-taxonomy-2026-01-01-rename-x", "status": "pending"}])
        rc, out = _run(pq, "--prefix", "l1-taxonomy-", "--status", "pending")
        assert rc == 0, "--prefix must be a known flag (was exit 2 'unknown arg')"
        assert out is not None, "must print JSON, not empty stdout"


def test_prefix_match_signals_suppression():
    """An open l1-taxonomy- question must SURFACE, so the guard can SKIP."""
    with tempfile.TemporaryDirectory() as tmpd:
        pq = Path(tmpd) / "pq.yaml"
        _write(pq, [
            {"id": "l1-taxonomy-2026-01-01-rename-x", "status": "pending"},
            {"id": "sq-012-unrelated", "status": "pending"},
        ])
        rc, out = _run(pq, "--prefix", "l1-taxonomy-", "--status", "pending")
        assert rc == 0
        assert _ids(out) == ["l1-taxonomy-2026-01-01-rename-x"], (
            "non-empty result is the SUPPRESS signal; the unrelated sq-012 "
            "entry must not leak into the family filter"
        )


def test_prefix_no_match_allows_proposal():
    """No open question in the family -> empty -> the guard correctly proceeds."""
    with tempfile.TemporaryDirectory() as tmpd:
        pq = Path(tmpd) / "pq.yaml"
        _write(pq, [{"id": "sq-012-unrelated", "status": "pending"}])
        rc, out = _run(pq, "--prefix", "l1-taxonomy-", "--status", "pending")
        assert rc == 0
        assert out == [], "empty result is the PROCEED signal"


def test_prefix_composes_with_status_filter():
    """An ANSWERED l1-taxonomy- question must not suppress a new proposal."""
    with tempfile.TemporaryDirectory() as tmpd:
        pq = Path(tmpd) / "pq.yaml"
        _write(pq, [{"id": "l1-taxonomy-2026-01-01-rename-x", "status": "answered"}])
        rc, out = _run(pq, "--prefix", "l1-taxonomy-", "--status", "pending")
        assert rc == 0
        assert out == [], "status filter must still apply alongside --prefix"


def test_prefix_tolerates_non_string_id():
    """A YAML-coerced non-string id must not raise on .startswith (str() guard)."""
    with tempfile.TemporaryDirectory() as tmpd:
        pq = Path(tmpd) / "pq.yaml"
        _write(pq, [{"id": 20260101, "status": "pending"},
                    {"id": "l1-taxonomy-ok", "status": "pending"}])
        rc, out = _run(pq, "--prefix", "l1-taxonomy-", "--status", "pending")
        assert rc == 0, "a non-string id must not crash the prefix filter"
        assert _ids(out) == ["l1-taxonomy-ok"]


def test_all_agents_with_prefix_is_accepted():
    """Plumbing for the real evolve call site: --all-agents --prefix --status.

    Asserts the flag COMBINATION is accepted and yields a JSON list. It does
    NOT assert on counts: bash `agents_root()` derives from the script's own
    location with no env override (unlike the Python `MIND_AGENTS_ROOT`), so
    --all-agents necessarily reads the live agents root. Seeding dirs there to
    make it hermetic is exactly the cruft-leak the fleet adopts (g-115-1633),
    so this stays a read-only plumbing assertion.
    """
    r = subprocess.run(
        [BASH, str(READ_SCRIPT), "--all-agents",
         "--prefix", "l1-taxonomy-", "--status", "pending"],
        env=_env(MIND_AGENT="foxtrot"),
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"combination must be accepted: {r.stderr!r}"
    parsed = json.loads(r.stdout.strip().splitlines()[-1])
    assert isinstance(parsed, list)
    assert all(str(e.get("id", "")).startswith("l1-taxonomy-") for e in parsed), (
        "every returned entry must match the requested family"
    )


def test_all_agents_and_pq_path_remain_mutually_exclusive():
    """--prefix must not have loosened the existing mutual-exclusion guard."""
    with tempfile.TemporaryDirectory() as tmpd:
        pq = Path(tmpd) / "pq.yaml"
        _write(pq, [{"id": "l1-taxonomy-x", "status": "pending"}])
        r = subprocess.run(
            [BASH, str(READ_SCRIPT), "--all-agents", "--pq-path", str(pq),
             "--prefix", "l1-taxonomy-"],
            env=_env(MIND_AGENT="foxtrot"),
            capture_output=True, text=True,
        )
        assert r.returncode == 2
        assert r.stdout.strip().splitlines()[-1] == "[]"


# --- F2 (): ONE unreadable peer must not blank the whole fleet view ---

def _pyeof_body() -> str:
    """Extract the SHIPPED python heredoc from the wrapper.

    Not a copy of the logic — the real body, read off disk at test time, so
    this test cannot drift away from what actually ships. Needed because the
    bash `agents_root()` has no env override (see
    test_all_agents_with_prefix_is_accepted), while the heredoc DOES read
    AGENTS_ROOT from the environment — so driving the extracted body is the
    only way to exercise fleet mode hermetically instead of seeding dirs into
    the live agents root (the g-115-1633 cruft-leak).
    """
    src = READ_SCRIPT.read_text(encoding="utf-8")
    m = re.search(r"<<'PYEOF'\n(.*?)\nPYEOF", src, re.S)
    assert m, "PYEOF heredoc not found in pending-questions-read.sh"
    return m.group(1)


def _run_fleet(agents_root: Path):
    """Drive the shipped heredoc in fleet mode over an isolated agents root."""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                     encoding="utf-8") as f:
        f.write(_pyeof_body())
        body = f.name
    try:
        r = subprocess.run(
            [sys.executable, body],
            env=_env(ALL_AGENTS="1", AGENTS_ROOT=str(agents_root),
                     PQ_PATH="", STATUS="", TYPE="", PREFIX=""),
            capture_output=True, text=True,
        )
    finally:
        os.unlink(body)
    parsed = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else None
    return r.returncode, parsed, r.stderr


def test_all_agents_survives_one_unreadable_peer():
    """A directory at a peer's pq path must not blank the fleet view.

    os.path.exists() proves a path EXISTS, not that it OPENS as a file. Before
    g-115-3107 the catch was `except yaml.YAMLError` only, so a directory at
    that path raised IsADirectoryError [Errno 21] — an OSError, uncaught —
    which killed the process: exit 1 and ZERO entries for EVERY agent, the
    exact opposite of the docstring's fail-open promise. Fleet mode amplifies
    it (N peer files other boxes are actively writing, vs one self-owned file).
    PermissionError on a root-owned peer file and a lost exists()->open() TOCTOU
    race are the same class.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        root = Path(tmpd)
        for name in ("aaa", "ccc"):
            (root / name / "session").mkdir(parents=True)
            _write(root / name / "session" / "pending-questions.yaml",
                   [{"id": f"pq-{name}-1", "status": "pending"}])
        # The bad peer: a DIRECTORY where the file is expected.
        (root / "bbb" / "session" / "pending-questions.yaml").mkdir(parents=True)

        rc, parsed, stderr = _run_fleet(root)

    assert rc == 0, f"one bad peer must not fail the sweep (stderr={stderr!r})"
    assert _ids(parsed) == ["pq-aaa-1", "pq-ccc-1"], (
        f"both good peers must still surface, got {_ids(parsed)}"
    )
    # The exception CLASS is platform-specific — opening a directory as a file
    # raises IsADirectoryError on POSIX but PermissionError ("Access is denied")
    # on Windows. Pinning the POSIX name alone made this fail on every win32
    # box (). The invariant under test is unchanged: the bad peer is
    # NAMED on stderr and reported as a real read error, never silently dropped.
    assert "bbb" in stderr, (
        "the unreadable peer must be named on stderr, not silently dropped"
    )
    assert ("IsADirectoryError" in stderr or "PermissionError" in stderr), (
        f"the failure must surface as a read error, got stderr={stderr!r}"
    )


def test_all_agents_tags_every_entry_with_its_owning_agent():
    """The `agent` tag is the accepted-residual-risk mitigation for F3.

    The read leg enumerates from an on-disk glob while the pull leg enumerates
    from the team-state roster (deliberate divergence — the legs have opposite
    failure requirements). A dir present on disk but absent from the roster is
    therefore READ but never REFRESHED, so a retired agent's leftover file
    could linger. The per-entry tag is what makes such an entry identifiable at
    display time rather than anonymous — if it regresses, the mitigation is
    gone and the divergence becomes unsafe.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        root = Path(tmpd)
        (root / "zzz" / "session").mkdir(parents=True)
        _write(root / "zzz" / "session" / "pending-questions.yaml",
               [{"id": "pq-zzz-1", "status": "pending"}])
        rc, parsed, _ = _run_fleet(root)

    assert rc == 0
    assert parsed[0]["agent"] == "zzz"


# --- : the residue the  pass left uncovered ---------------
#
# READ THE PREMISE CORRECTION FIRST.  was filed saying --all-agents
# "shipped with zero new tests". That was true when filed and is FALSE now:
#  (commit 333ef7a4b) added the fleet harness and three tests above.
# Measured 2026-07-31 before writing a line. Two genuine gaps survive it, and
# they are narrower and more specific than the goal's framing:
#
#   (a) EVERY fleet test drives the EXTRACTED python heredoc with AGENTS_ROOT
#       injected into the environment (see _run_fleet). That is deliberate and
#       well-reasoned — bash agents_root() has no env override, so seeding the
#       live agents root would be the  cruft-leak. But the consequence
#       is absolute: NOTHING exercises the bash-side derivation at line ~102,
#       so a regression THERE is invisible to the entire suite. That derivation
#       is exactly what CLAUDE.md's "cross-agent glob consumers" table calls
#       this call site's only audit surface, and exactly what the goal ranks
#       HIGHEST risk. A source-level assertion is the only instrument that can
#       reach it under the hermeticity constraint.
#   (b) The tagging test uses ONE agent, so it cannot distinguish "tag each
#       entry with ITS owner" from "tag every entry with the FIRST owner seen".
#       The isolation test happens to run three agents but asserts only on ids.


def test_bash_derives_agents_root_via_the_paths_helper():
    """SOURCE guard for the one line no runtime test can reach (gap (a)).

    ANCHORED ON THE ASSIGNMENT, not on a mention of the name — guard-1099. The
    word `agents_root` appears on six lines of the wrapper and TWO of them are
    comments (the header's "routed through `agents_root()`" note and the
    path-decomposition comment beside the glob). A bare
    `grep -q agents_root` therefore passes with the assignment DELETED, matching
    the prose that describes the rule instead of the code that implements it.
    Measured on this file, not assumed.
    """
    src = READ_SCRIPT.read_text(encoding="utf-8")
    code = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]

    assert any(re.match(r'\s*AGENTS_ROOT="\$\(agents_root\)"\s*$', ln) for ln in code), (
        "the fleet root must be derived by calling agents_root() from _paths.sh; "
        "a literal path or a PROJECT_ROOT-relative glob would match NOTHING after "
        "an AGENTS_PARENT_DIR rename and is invisible to all three CLAUDE.md audit greps"
    )
    # The unavailability guard is half the contract: without it a missing
    # _paths.sh yields an EMPTY root, and the glob then silently walks "/*/session/…"
    # rather than failing. Exit 2, not a quiet empty sweep.
    assert any("declare -F agents_root" in ln for ln in code), (
        "an unsourced _paths.sh must fail loudly (exit 2), not degrade to an empty root"
    )
    for ln in code:
        assert "AGENTS_ROOT=" not in ln or "$(agents_root)" in ln or 'AGENTS_ROOT=""' in ln, (
            f"AGENTS_ROOT assigned from something other than agents_root(): {ln!r}"
        )


def test_two_agent_fleet_returns_both_entries_each_tagged_with_its_own_owner():
    """The goal's explicit ask, and what a single-agent tagging test cannot prove.

    With one agent, "tag with the owner" and "tag everything with the first
    owner seen" are indistinguishable. Two agents separate them. This also
    pins that BOTH agents' entries come back — the defect the fleet fix
    existed to cure was returning only the bound agent (21 of 31 fleet
    questions invisible).
    """
    with tempfile.TemporaryDirectory() as tmpd:
        root = Path(tmpd)
        for name in ("aaa", "bbb"):
            (root / name / "session").mkdir(parents=True)
            _write(root / name / "session" / "pending-questions.yaml",
                   [{"id": f"pq-{name}-1", "status": "pending"}])
        rc, parsed, stderr = _run_fleet(root)

    assert rc == 0, f"clean two-agent fleet must succeed (stderr={stderr!r})"
    assert _ids(parsed) == ["pq-aaa-1", "pq-bbb-1"], (
        f"both agents' entries must surface, got {_ids(parsed)}"
    )
    by_id = {e["id"]: e.get("agent") for e in parsed}
    assert by_id == {"pq-aaa-1": "aaa", "pq-bbb-1": "bbb"}, (
        "each entry must carry ITS OWN owner; tagging every entry with the first "
        f"agent seen passes a single-agent test and fails here: {by_id}"
    )


def test_unknown_flag_exits_2_so_all_agents_needs_no_extra_exclusion_guard():
    """PREMISE CORRECTION, pinned so the wrong premise cannot be re-derived.

    g-115-3101 asks for a `--all-agents + --agent` mutual-exclusion guard
    alongside the `--all-agents + --pq-path` one. There is no such guard and
    none is needed: `--agent` is not a flag at all, so it falls to the parser's
    catch-all and exits 2 already. Measured (rc=2 both with and without
    --all-agents) rather than inferred from reading the case statement.

    Pinned rather than merely noted because the NEXT reader of that goal text
    will otherwise re-derive the same wrong conclusion and add a redundant
    guard. What must not regress is the catch-all itself — loosening it to
    ignore unknown flags would silently accept a typo'd invocation.
    """
    for argv in (["--all-agents", "--agent", "foxtrot"], ["--agent", "foxtrot"]):
        r = subprocess.run(
            [BASH, str(READ_SCRIPT), *argv],
            env=_env(MIND_AGENT="foxtrot"), capture_output=True, text=True,
        )
        assert r.returncode == 2, f"{argv} must exit 2, got {r.returncode}"
        assert "unknown arg '--agent'" in r.stderr, (
            f"the catch-all must NAME the offending flag: {r.stderr!r}"
        )


if __name__ == "__main__":
    test_all_agents_survives_one_unreadable_peer()
    test_all_agents_tags_every_entry_with_its_owning_agent()
    test_bash_derives_agents_root_via_the_paths_helper()
    test_two_agent_fleet_returns_both_entries_each_tagged_with_its_own_owner()
    test_unknown_flag_exits_2_so_all_agents_needs_no_extra_exclusion_guard()
    test_prefix_flag_is_accepted_not_unknown_arg()
    test_prefix_match_signals_suppression()
    test_prefix_no_match_allows_proposal()
    test_prefix_composes_with_status_filter()
    test_prefix_tolerates_non_string_id()
    test_all_agents_with_prefix_is_accepted()
    test_all_agents_and_pq_path_remain_mutually_exclusive()
    test_shape_a_dict_wrapper_pending_surfaces()
    test_shape_b_wrapper_element_pending_surfaces()
    test_shape_c_bare_list_pending_surfaces()
    test_mixed_shape_status_filter_sees_both()
    test_status_filter_excludes_non_pending()
    test_type_and_status_filter()
    test_no_filter_returns_all()
    test_missing_file_returns_empty_exit_0()
    test_malformed_yaml_fails_open()
    test_default_path_no_agent_clean_exit_not_unbound_crash()
    print("ok")
