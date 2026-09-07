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
    return tmp_path


@pytest.fixture
def run_in(monkeypatch):
    def _run(tmp_path, rows, capsys):
        build_world(tmp_path, rows)
        monkeypatch.chdir(tmp_path)
        rc = load_checker().main()
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
