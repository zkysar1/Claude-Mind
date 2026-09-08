"""hook-slot-contract-check.py: the three-class verdict partition ().

Drives a SYNTHETIC Canonical Hook Slots table, never the live registry. The
live table's break count is the thing other goals are actively changing
(g-115-9281 owns the genuine breaks), so a test pinned to it would go red on
someone else's fix and say nothing about this parse.

What is pinned here is the classification the parse produces:

  * an EXECUTABLE slot (payload `world/scripts/<slot>.sh`, consumer in code)
    reports UNCHECKED, not FAIL — its markdown gate does not exist to be
    gated, so requirement 2 is inapplicable rather than unmet;
  * a MARKDOWN slot that references its slot without existence-gating it
    still FAILs — the parse fix retires no true positive (guard-3688: a
    correction that removes false positives must be shown not to remove real
    ones);
  * the slot NAME survives an annotated name cell;
  * the three classes partition the table.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "hook-slot-contract-check.py"


def load_checker():
    spec = importlib.util.spec_from_file_location("hook_slot_contract_check",
                                                  SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HEADER = """# Domain hooks

## Canonical Hook Slots

| Slot name | Consumer | Purpose |
|---|---|---|
"""

ROW_MD_GATED = ("| `md-gated` | `consumers/gated.md` step 4 | a markdown slot "
                "whose consumer gates it |\n")
ROW_MD_UNGATED = ("| `md-ungated` | `consumers/ungated.md` step 4 | a markdown "
                  "slot whose consumer does NOT gate it |\n")
# The name cell carries its own annotation, and the consumer column names a
# .py primary with a .sh companion — the live shape of every executable slot.
ROW_EXEC = ("| `exec-slot` (**executable slot** — `world/scripts/exec-slot.sh`,"
            " not a `.md`) | `consumers/driver.py` (`exec-slot.sh`) step 4 — "
            "the chokepoint | an executable slot |\n")
ROW_NO_PATH = ("| `no-path` | described in prose with nothing backticked | a "
               "registry row that names no consumer |\n")


def build_world(tmp_path, rows):
    """A world rooted at tmp_path holding the table and its consumer files."""
    conv = tmp_path / "core/config/conventions"
    conv.mkdir(parents=True)
    (conv / "domain-hooks.md").write_text(HEADER + "".join(rows) + "\n",
                                          encoding="utf-8")
    cons = tmp_path / "consumers"
    cons.mkdir()
    (cons / "gated.md").write_text(
        'load-conventions.sh md-gated\n'
        'if test -f "$WORLD_DIR/conventions/md-gated.md"; then ...; fi\n',
        encoding="utf-8")
    (cons / "ungated.md").write_text(
        "reads the md-ungated convention with no existence gate\n",
        encoding="utf-8")
    (cons / "driver.py").write_text(
        '# dispatches the exec-slot hook\nSLOT = "exec-slot"\n',
        encoding="utf-8")
    # F3 (): one consumer per accepted gate form. All three satisfy
    # requirement 2 identically; only `test -f` used to be recognised.
    (cons / "bracket.md").write_text(
        'load-conventions.sh md-bracket\n'
        'if [ -f "$WORLD_DIR/conventions/md-bracket.md" ]; then ...; fi\n',
        encoding="utf-8")
    (cons / "dbracket.md").write_text(
        'load-conventions.sh md-dbracket\n'
        'if [[ -f "$WORLD_DIR/conventions/md-dbracket.md" ]]; then ...; fi\n',
        encoding="utf-8")
    # F2: not valid UTF-8, so read_text raises. chmod is not usable here — the
    # suite can run as root, for whom a 000 mode is not a barrier.
    (cons / "undecodable.md").write_text("placeholder\n", encoding="utf-8")
    (cons / "undecodable.md").write_bytes(b"md-undecodable \xff\xfe not utf-8\n")
    return tmp_path


@pytest.fixture
def run_in(monkeypatch):
    """Point the checker at a synthetic world and run it.

    F1 (g-115-3299) made PROJECT_ROOT/CONV absolute, derived from __file__, so
    the checker no longer resolves anything against the cwd. That is the whole
    point of the fix -- and it means a chdir-based harness would silently grade
    the LIVE registry instead of the synthetic table (measured: 4 of 5 tests
    went red against the real 11-row table the moment F1 landed). The tmp world
    is therefore injected on the module, and the cwd is deliberately moved
    somewhere ELSE so any surviving cwd-relative path would fail loudly rather
    than accidentally resolve.
    """
    def _run(tmp_path, rows, capsys, foreign_cwd=True):
        build_world(tmp_path, rows)
        mod = load_checker()
        mod.PROJECT_ROOT = tmp_path
        mod.CONV = tmp_path / "core/config/conventions/domain-hooks.md"
        if foreign_cwd:
            elsewhere = tmp_path / "elsewhere"
            elsewhere.mkdir()
            monkeypatch.chdir(elsewhere)
        rc = mod.main()
        return rc, capsys.readouterr().out
    return _run


def _section(out, header_prefix):
    """Lines of the '  - ' block following the named header, else []."""
    lines, grabbing, found = [], False, []
    for line in out.splitlines():
        if line.startswith(header_prefix):
            grabbing = True
            continue
        if grabbing:
            if line.startswith("  - "):
                found.append(line[4:])
            elif not line.startswith("  "):
                grabbing = False
    return found


def test_executable_slot_reports_unchecked_not_broken(tmp_path, run_in, capsys):
    rc, out = run_in(tmp_path, [ROW_MD_GATED, ROW_EXEC], capsys)
    unchecked = _section(out, "UNCHECKED:")
    assert any(u.startswith("exec-slot:") for u in unchecked), out
    # and it is NOT a contract failure
    assert "FAIL" not in out, out
    assert rc == 0, out


def test_executable_slot_name_is_not_mangled(tmp_path, run_in, capsys):
    rc, out = run_in(tmp_path, [ROW_MD_GATED, ROW_EXEC], capsys)
    # the annotation must not bleed into the reported slot name
    assert "exec-slot`" not in out, out
    assert "(**executable slot**" not in out, out


def test_markdown_slot_without_gate_still_fails(tmp_path, run_in, capsys):
    """The parse retires false positives WITHOUT retiring this true one."""
    rc, out = run_in(tmp_path, [ROW_MD_UNGATED, ROW_EXEC], capsys)
    broken = _section(out, "FAIL:")
    assert any(b.startswith("md-ungated:") and "does NOT" in b
               for b in broken), out
    assert not any(b.startswith("exec-slot:") for b in broken), out
    assert rc == 1, out


def test_row_naming_no_consumer_path_is_a_failure(tmp_path, run_in, capsys):
    rc, out = run_in(tmp_path, [ROW_MD_GATED, ROW_NO_PATH], capsys)
    broken = _section(out, "FAIL:")
    assert any(b.startswith("no-path:") and "unparseable" in b
               for b in broken), out
    assert rc == 1, out


def test_three_classes_partition_the_table(tmp_path, run_in, capsys):
    rows = [ROW_MD_GATED, ROW_MD_UNGATED, ROW_EXEC, ROW_NO_PATH]
    rc, out = run_in(tmp_path, rows, capsys)
    n_unchecked = len(_section(out, "UNCHECKED:"))
    n_broken = len(_section(out, "FAIL:"))
    assert n_unchecked == 1, out
    assert n_broken == 2, out
    # ok = rows - broken - unchecked; the checker refuses to emit a verdict at
    # all when these do not sum, so reaching rc 1 is itself the partition proof
    assert rc == 1, out
    assert "internal accounting error" not in out, out


ROW_MD_BRACKET = ("| `md-bracket` | `consumers/bracket.md` step 4 | gated with "
                  "the single-bracket form |\n")
ROW_MD_DBRACKET = ("| `md-dbracket` | `consumers/dbracket.md` step 4 | gated "
                   "with the double-bracket form |\n")
ROW_MD_UNDECODABLE = ("| `md-undecodable` | `consumers/undecodable.md` step 4 | "
                      "a consumer that cannot be decoded |\n")


# ---------------------------------------------------------------- F3 --------
# The existence gate accepted ONLY `test -f`, so the two bracket forms — the
# more common bash idiom — were reported as "does NOT existence-gate it". A
# checker that punishes a correct implementation teaches people to ignore it.
# One test per form, per the goal's own instruction.

def test_single_bracket_gate_is_accepted(tmp_path, run_in, capsys):
    rc, out = run_in(tmp_path, [ROW_MD_BRACKET], capsys)
    assert "FAIL" not in out, out
    assert rc == 0, out


def test_double_bracket_gate_is_accepted(tmp_path, run_in, capsys):
    rc, out = run_in(tmp_path, [ROW_MD_DBRACKET], capsys)
    assert "FAIL" not in out, out
    assert rc == 0, out


def test_test_dash_f_gate_still_accepted(tmp_path, run_in, capsys):
    """The widening must not break the form that already worked (guard-2201:
    a widening is additive, so assert the REMOVED set is empty)."""
    rc, out = run_in(tmp_path, [ROW_MD_GATED], capsys)
    assert "FAIL" not in out, out
    assert rc == 0, out


def test_ungated_consumer_is_still_broken_after_the_widening(tmp_path, run_in,
                                                             capsys):
    """The widening accepts more GATE FORMS, not more consumers. A consumer
    with no gate at all must still FAIL, or F3 would have converted a real
    contract break into a silent pass."""
    rc, out = run_in(tmp_path, [ROW_MD_UNGATED], capsys)
    assert any(b.startswith("md-ungated:") for b in _section(out, "FAIL:")), out
    assert rc == 1, out


# ---------------------------------------------------------------- F2 --------

def test_undecodable_consumer_is_a_checker_fault_not_a_contract_verdict(
        tmp_path, run_in, capsys):
    """rc 2, never rc 1.

    An uncaught read error used to exit 1 — this script's "contract broken"
    code — so a permissions or encoding fault on ONE consumer was reported to
    verify-learning as a hook-slot VIOLATION. Crash and FAIL must not share an
    exit code.
    """
    rc, out = run_in(tmp_path, [ROW_MD_GATED, ROW_MD_UNDECODABLE], capsys)
    assert rc == 2, out
    assert "CHECKER fault" in out, out
    # and it must NOT be reported as a contract break against the slot
    assert not any(b.startswith("md-undecodable:")
                   for b in _section(out, "FAIL:")), out


# ---------------------------------------------------------------- F4 --------

def test_renamed_header_column_does_not_become_a_phantom_slot(tmp_path, run_in,
                                                              capsys):
    """The header was dropped by matching the literal "Slot name". Renaming
    that column turned the header row into a phantom slot named "Slot" that
    failed "consumer path unparseable" — the checker reporting the registry's
    own PROSE as a broken contract. The header is the first non-separator row;
    dropping it by position holds under any column name.
    """
    renamed = HEADER.replace("| Slot name | Consumer | Purpose |",
                             "| Hook | Consumer | Purpose |")
    assert "Slot name" not in renamed, "header rename did not apply"
    conv = tmp_path / "core/config/conventions"
    conv.mkdir(parents=True)
    (conv / "domain-hooks.md").write_text(renamed + ROW_MD_GATED + "\n",
                                          encoding="utf-8")
    cons = tmp_path / "consumers"
    cons.mkdir()
    (cons / "gated.md").write_text(
        'load-conventions.sh md-gated\n'
        'if test -f "$WORLD_DIR/conventions/md-gated.md"; then ...; fi\n',
        encoding="utf-8")
    mod = load_checker()
    mod.PROJECT_ROOT = tmp_path
    mod.CONV = conv / "domain-hooks.md"
    rc = mod.main()
    out = capsys.readouterr().out
    assert "Hook" not in _section(out, "FAIL:"), out
    assert "unparseable" not in out, out
    assert rc == 0, out


# ---------------------------------------------------------------- F1 --------

def test_runs_from_a_foreign_cwd(tmp_path, run_in, capsys):
    """Paths derive from __file__, not the cwd.

    Before F1 every path was cwd-relative, so from anywhere but PROJECT_ROOT
    the checker printed "FAIL: <registry> not found" and exited 2 — blaming the
    REGISTRY for a cwd fault. `run_in` chdirs somewhere else for EVERY test in
    this file, so this asserts the property explicitly rather than relying on
    the fixture's default staying that way.
    """
    rc, out = run_in(tmp_path, [ROW_MD_GATED], capsys, foreign_cwd=True)
    assert "not found" not in out, out
    assert rc == 0, out
