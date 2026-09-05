"""Tests for junit-xml-testcount.py — one per silent trap it exists to close.

Each fixture ENCODES the trap: the display name deliberately differs from the
class name (trap 1), nested containers are deliberately split across files
(trap 2), and the absent-results case is deliberately distinguished from a
zero-count case (trap 3). A fixture that did not encode the trap would pass
against a naive implementation and prove nothing.
"""
import importlib.util
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(_HERE, os.pardir, "junit-xml-testcount.py")

_spec = importlib.util.spec_from_file_location("junit_xml_testcount", _SCRIPT)
jtc = importlib.util.module_from_spec(_spec)
sys.modules["junit_xml_testcount"] = jtc
_spec.loader.exec_module(jtc)


def _suite(path, name, tests, failures=0, errors=0, skipped=0):
    path.write_text(
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<testsuite name="{name}" tests="{tests}" failures="{failures}" '
        f'errors="{errors}" skipped="{skipped}"></testsuite>\n',
        encoding="utf-8",
    )


# ---------------------------------------------------------------- trap 1
def test_finds_class_whose_name_attribute_is_a_display_name(tmp_path):
    """The `name` attribute holds the @DisplayName, so a name-keyed match returns
    nothing and reads as 'never ran'. Keying on the FILENAME finds it."""
    rd = tmp_path / "test-results"
    rd.mkdir()
    _suite(rd / "TEST-com.example.TestRouterGuard.xml",
           name="a readable display name that does not contain the class", tests=2)

    rec = jtc.assess(str(rd), "TestRouterGuard", None, None)
    assert rec["verdict"] == "EXECUTED"
    assert rec["tests"] == 2
    # The trap made concrete: the attribute the naive implementation would read
    # does NOT contain the class name.
    import xml.etree.ElementTree as ET
    attr = ET.parse(rd / "TEST-com.example.TestRouterGuard.xml").getroot().get("name")
    assert "TestRouterGuard" not in attr


# ---------------------------------------------------------------- trap 2
def test_sums_nested_container_files_rather_than_reading_one(tmp_path):
    """@Nested classes write their OWN files named by display name; the outer
    file holds only part of the total."""
    rd = tmp_path / "test-results"
    rd.mkdir()
    _suite(rd / "TEST-com.example.TestStartupBranch.xml", name="outer", tests=2)
    _suite(rd / "TEST-com.example.TestStartupBranch$Nested1.xml", name="1. the branch reaches critical", tests=2)
    _suite(rd / "TEST-com.example.TestStartupBranch$Nested2.xml", name="2. negative control", tests=1)

    rec = jtc.assess(str(rd), "TestStartupBranch", None, None)
    assert rec["files"] == 3, "must match nested-container files, not just the outer one"
    assert rec["tests"] == 5, "must SUM across nested files (2+2+1), not report the outer 2"
    assert rec["verdict"] == "EXECUTED"


# ---------------------------------------------------------------- trap 3
def test_absent_results_is_not_reported_as_zero_executed(tmp_path):
    """UP-TO-DATE writes no XML, so absence must NOT collapse into 'zero tests'."""
    rd = tmp_path / "test-results"
    rd.mkdir()
    rec = jtc.assess(str(rd), "TestNeverRan", None, None)
    assert rec["verdict"] == "NO_RESULT_FILE"
    assert rec["verdict"] != "ZERO_EXECUTED"
    assert not rec["ok"]
    assert "UP-TO-DATE" in rec["detail"]


def test_zero_tests_in_a_present_file_is_a_failed_measurement(tmp_path):
    rd = tmp_path / "test-results"
    rd.mkdir()
    _suite(rd / "TEST-com.example.TestSelectorMissed.xml", name="whatever", tests=0)
    rec = jtc.assess(str(rd), "TestSelectorMissed", None, None)
    assert rec["verdict"] == "ZERO_EXECUTED"
    assert not rec["ok"]


# ------------------------------------------------- positive control (step 4)
def test_declared_count_control_flags_an_under_report(tmp_path):
    rd = tmp_path / "test-results"
    rd.mkdir()
    _suite(rd / "TEST-com.example.TestPartial.xml", name="display", tests=3)
    src = tmp_path / "src"
    (src / "com" / "example").mkdir(parents=True)
    (src / "com" / "example" / "TestPartial.java").write_text(
        "class TestPartial {\n"
        "  @Test void a() {}\n  @Test void b() {}\n  @Test void c() {}\n"
        "  @Test void d() {}\n  @ParameterizedTest void e(int i) {}\n}\n",
        encoding="utf-8")
    rec = jtc.assess(str(rd), "TestPartial", str(src), None)
    assert rec["declared"] == 5
    assert rec["verdict"] == "UNDER_DECLARED"
    assert not rec["ok"]


def test_declared_count_ignores_commented_out_annotations(tmp_path):
    rd = tmp_path / "test-results"
    rd.mkdir()
    _suite(rd / "TEST-com.example.TestCommented.xml", name="display", tests=1)
    src = tmp_path / "src"
    (src / "p").mkdir(parents=True)
    (src / "p" / "TestCommented.java").write_text(
        "class TestCommented {\n  @Test void a() {}\n  // @Test void disabled() {}\n"
        "  * @Test in javadoc\n}\n", encoding="utf-8")
    rec = jtc.assess(str(rd), "TestCommented", str(src), None)
    assert rec["declared"] == 1
    assert rec["verdict"] == "EXECUTED"


def test_missing_source_reports_control_unavailable_not_a_pass(tmp_path):
    rd = tmp_path / "test-results"
    rd.mkdir()
    _suite(rd / "TEST-com.example.TestNoSource.xml", name="display", tests=4)
    src = tmp_path / "src"
    src.mkdir()
    rec = jtc.assess(str(rd), "TestNoSource", str(src), None)
    assert rec["declared"] is None
    assert "no source file" in rec["declared_note"]
    assert rec["verdict"] == "EXECUTED"


# --------------------------------------------------------------- staleness
def test_results_older_than_the_edits_are_stale(tmp_path):
    rd = tmp_path / "test-results"
    rd.mkdir()
    xml = rd / "TEST-com.example.TestStale.xml"
    _suite(xml, name="display", tests=7)
    edit = tmp_path / "Thing.java"
    edit.write_text("// edited after the run\n", encoding="utf-8")
    os.utime(xml, (1_000_000, 1_000_000))
    rec = jtc.assess(str(rd), "TestStale", None, str(edit))
    assert rec["verdict"] == "STALE_RESULTS"
    assert not rec["ok"]


# ------------------------------------------------------------- suite total
def test_suite_level_total_sums_every_result_file(tmp_path):
    rd = tmp_path / "test-results"
    rd.mkdir()
    _suite(rd / "TEST-a.A.xml", name="A", tests=10, failures=1)
    _suite(rd / "TEST-b.B.xml", name="B", tests=5, errors=2, skipped=1)
    rec = jtc.assess(str(rd), None, None, None)
    assert (rec["tests"], rec["failures"], rec["errors"], rec["skipped"]) == (15, 1, 2, 1)
    assert rec["files"] == 2


def test_unparseable_file_is_reported_not_silently_zeroed(tmp_path):
    rd = tmp_path / "test-results"
    rd.mkdir()
    _suite(rd / "TEST-com.example.TestMixed.xml", name="display", tests=3)
    (rd / "TEST-com.example.TestMixedBroken.xml").write_text("<testsuite oops", encoding="utf-8")
    rec = jtc.assess(str(rd), "TestMixed", None, None)
    assert rec["tests"] == 3
    assert rec["unparseable"], "a swallowed parse error is indistinguishable from a zero"


# ------------------------------------------------------------------- CLI
def test_cli_exit_codes(tmp_path, capsys):
    rd = tmp_path / "test-results"
    rd.mkdir()
    _suite(rd / "TEST-com.example.TestOk.xml", name="display", tests=3)
    assert jtc.main(["--results-dir", str(rd), "--class", "TestOk"]) == 0
    assert jtc.main(["--results-dir", str(rd), "--class", "TestAbsent"]) == 1
    assert jtc.main(["--results-dir", str(tmp_path / "nope")]) == 2


def test_cli_json_shape(tmp_path, capsys):
    import json
    rd = tmp_path / "test-results"
    rd.mkdir()
    _suite(rd / "TEST-com.example.TestJson.xml", name="display", tests=2)
    jtc.main(["--results-dir", str(rd), "--class", "TestJson", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["records"][0]["verdict"] == "EXECUTED"
