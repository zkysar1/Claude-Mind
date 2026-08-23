"""Tests for tree_shape_fork.py — the crit3 shape-fork measurement (gap-111).

The tool exists to stop a destructive reduction being applied to the wrong node
SHAPE, so the properties worth pinning are the ones whose failure is silent:

  * the unit is BYTES, not lines (a line-based profile is wrong by up to 7x and
    errs in the direction that HIDES the dominant section — the exact defect
    that made Step 1.6's own prescription wrong until 2026-08-17);
  * thresholds are READ from their owning files, never hardcoded here or there
    (core/config/tree.yaml says outright: "tree.py owns the ratio; this comment
    must not restate it as a second source of truth");
  * the (a)/(b)/(c)/(d) verdict is WITHHELD — a tool that started emitting one
    would convert a judgment call into an automated one.

Every detector below is pinned with a POSITIVE and a NEGATIVE case in the same
test (guard-3845: a positive control proves the pattern fires, never that it
discriminates, and the negative half is the one usually skipped).
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent

_spec = importlib.util.spec_from_file_location(
    "tree_shape_fork", CORE_SCRIPTS / "tree_shape_fork.py")
tsf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tsf)


def _node(tmp_path, body, name="node.md"):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def _profile(tmp_path, body, name="node.md"):
    return tsf.profile(_node(tmp_path, body, name), tsf.load_thresholds())


# ── the unit is BYTES ────────────────────────────────────────────────────────


def test_table_section_dominates_by_bytes_while_looking_small_by_lines(tmp_path):
    """The canonical failure: few lines, most of the bytes.

    A line-based profile routes this to "roll up"; the byte profile shows it is
    the dominant section. Both units are asserted in one test so the inversion
    between them is visible rather than implied.
    """
    prose = "short prose line\n" * 400              # many lines, few bytes each
    table = ("| " + "x" * 500 + " |\n") * 40        # few lines, many bytes each
    p = _profile(tmp_path, f"# N\n\n## Prose\n\n{prose}\n## Table\n\n{table}")
    by_head = {s["heading"]: s for s in p["sections"]}
    prose_s, table_s = by_head["## Prose"], by_head["## Table"]

    assert prose_s["lines"] > table_s["lines"]          # by LINES prose wins
    assert table_s["bytes"] > prose_s["bytes"]          # by BYTES the table wins
    assert p["dominant_section"] == "## Table"
    assert any("DOMINANT SECTION" in f for f in p["findings"])


def test_byte_count_is_utf8_not_character_count(tmp_path):
    """A node full of em-dashes is larger on disk than its character count.

    The cap this fork protects is about bytes/tokens; counting characters
    understates a punctuation-dense node.
    """
    body = "# N\n\n## S\n\n" + ("— " * 100) + "\n"    # em-dash is 3 bytes in UTF-8
    p = _profile(tmp_path, body)
    assert p["total_bytes"] == len(body.encode("utf-8"))
    assert p["total_bytes"] > len(body)               # strictly more than chars


# ── TABLE-shaped detection: positive AND negative ────────────────────────────


def test_table_shaped_flag_discriminates_table_from_prose(tmp_path):
    table = ("| " + "y" * 400 + " |\n") * 30
    prose = "a short prose line here\n" * 30
    p = _profile(tmp_path, f"# N\n\n## Prose\n\n{prose}\n## Table\n\n{table}")
    by_head = {s["heading"]: s for s in p["sections"]}

    assert by_head["## Table"]["table_shaped"] is True       # positive control
    assert by_head["## Prose"]["table_shaped"] is False      # negative control
    assert by_head["## Table"]["bytes_per_line"] > tsf.TABLE_SHAPED_BYTES_PER_LINE
    assert by_head["## Prose"]["bytes_per_line"] < tsf.TABLE_SHAPED_BYTES_PER_LINE
    assert any("TABLE-SHAPED" in f for f in p["findings"])


def test_uniform_node_raises_no_dominant_or_table_finding(tmp_path):
    """The negative control for the whole detector set.

    Without this, every assertion above would also pass a tool that flagged
    everything unconditionally.
    """
    sec = "an ordinary prose line of text\n" * 20
    body = "# N\n\n" + "".join(f"## Section {i}\n\n{sec}\n" for i in range(6))
    p = _profile(tmp_path, body)

    assert not any("DOMINANT SECTION" in f for f in p["findings"])
    assert not any("TABLE-SHAPED" in f for f in p["findings"])
    assert all(s["table_shaped"] is False for s in p["sections"])


# ── inverted bloat (shape (c) tell) ──────────────────────────────────────────


def test_inversion_ratio_flags_newer_half_larger_and_stays_quiet_when_uniform(tmp_path):
    small, big = "s\n" * 5, "b" * 400 + "\n"
    grown = "# N\n\n" + "".join(f"## Old {i}\n\n{small}\n" for i in range(3)) \
                      + "".join(f"## New {i}\n\n{big}\n" for i in range(3))
    p_grown = _profile(tmp_path, grown, "grown.md")
    assert p_grown["newest_vs_oldest_byte_ratio"] >= 2.0
    assert any("INVERTED BLOAT" in f for f in p_grown["findings"])

    sec = "an ordinary prose line\n" * 10
    uniform = "# N\n\n" + "".join(f"## S{i}\n\n{sec}\n" for i in range(6))
    p_uniform = _profile(tmp_path, uniform, "uniform.md")
    assert p_uniform["newest_vs_oldest_byte_ratio"] < 2.0
    assert not any("INVERTED BLOAT" in f for f in p_uniform["findings"])


# ── thresholds come from their owning files, not from this repo twice ────────


def test_chars_per_token_is_read_from_tree_py_not_hardcoded():
    """core/config/tree.yaml: 'tree.py owns the ratio'. Assert we read THAT one."""
    sys.path.insert(0, str(CORE_SCRIPTS))
    spec = importlib.util.spec_from_file_location("_t", CORE_SCRIPTS / "tree.py")
    tree_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tree_mod)

    th = tsf.load_thresholds()
    assert th["chars_per_token"] == float(tree_mod.CHARS_PER_TOKEN)
    assert "tree.py" in th["chars_per_token_source"]
    # The module must not carry its own copy of the ratio.
    src = (CORE_SCRIPTS / "tree_shape_fork.py").read_text(encoding="utf-8")
    assert "CHARS_PER_TOKEN = " not in src


def test_crit3_budget_is_derived_from_config_not_a_literal():
    th = tsf.load_thresholds()
    assert th["distill_token_cap"], "token cap not read from core/config/tree.yaml"
    assert th["distill_token_ratio"], "crit3 ratio not read from core/config/tree.yaml"
    expected = int(th["distill_token_cap"] * th["distill_token_ratio"] * th["chars_per_token"])
    assert th["crit3_trigger_bytes"] == expected
    # The literal must not appear in the source as a constant.
    src = (CORE_SCRIPTS / "tree_shape_fork.py").read_text(encoding="utf-8")
    assert "46000" not in src


def test_suggested_partitions_uses_the_budget_and_exceeds_one_when_needed(tmp_path):
    """One child is often not enough — the count comes from the budget."""
    th = tsf.load_thresholds()
    budget = th["crit3_trigger_bytes"]
    body = "# N\n\n" + "".join(f"## S{i}\n\n" + ("x" * 1000 + "\n") * 40
                               for i in range(int(budget * 3 / 40000) + 4))
    p = _profile(tmp_path, body)
    assert p["total_bytes"] > budget * 2
    assert p["suggested_partitions"] >= 3
    assert p["suggested_partitions"] == -(-p["total_bytes"] // budget)


# ── the verdict is withheld BY DESIGN ────────────────────────────────────────


def test_shape_verdict_is_always_withheld(tmp_path):
    """The design invariant. If this ever starts returning a shape, the tool has
    taken over a judgment call that Step 1.6 assigns to a reader."""
    for body in ("# N\n\n## A\n\nx\n",
                 "# N\n\n## A\n\n" + "x" * 90000 + "\n",
                 "# N\n\n## A\n\ns\n\n## B\n\n" + "b" * 50000 + "\n"):
        p = _profile(tmp_path, body)
        assert p["shape_verdict"] is None
        assert "WITHHELD BY DESIGN" in p["shape_verdict_note"]


# ── CLI contract ─────────────────────────────────────────────────────────────


def _run(*args):
    return subprocess.run(
        [sys.executable, str(CORE_SCRIPTS / "tree_shape_fork.py"), *args],
        capture_output=True, text=True)


def test_cli_json_is_parseable_and_carries_provenance(tmp_path):
    node = _node(tmp_path, "# N\n\n## A\n\nhello\n")
    r = _run(str(node), "--json")
    assert r.returncode == 0
    payload = json.loads(r.stdout)
    assert payload["shape_verdict"] is None
    assert payload["thresholds"]["chars_per_token_source"]


def test_cli_refuses_a_directory_with_rc2_and_names_the_shards(tmp_path):
    """A tree node can already BE a split directory; exists() is True for one.

    Measured live during validation: without this the tool died on an unhandled
    IsADirectoryError traceback.
    """
    d = tmp_path / "already-split"
    d.mkdir()
    (d / "shard-a.md").write_text("# a\n", encoding="utf-8")
    (d / "shard-b.md").write_text("# b\n", encoding="utf-8")
    r = _run(str(d))
    assert r.returncode == 2
    assert "DIRECTORY" in r.stderr
    assert "shard-a.md" in r.stderr and "shard-b.md" in r.stderr
    assert "Traceback" not in r.stderr


def test_cli_missing_file_is_rc2_not_a_traceback(tmp_path):
    r = _run(str(tmp_path / "nope.md"))
    assert r.returncode == 2
    assert "no such file" in r.stderr
    assert "Traceback" not in r.stderr
