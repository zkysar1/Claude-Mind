"""Tests for jsonl-field-probe.py's additive value-frequency mode ().

Two guardrails prescribe a value-frequency histogram before trusting a
measurement built on a field — guard-3265 (histogram a timestamp before
pairing/joining on it; a backfill writes one literal instant across many
records) and guard-2144 (print the batch's date and category distributions
beside the corpus slice's). Neither had a command, so both asked the reader to
hand-roll a Counter, which is the step a reader under time pressure skips.

The half of this file that matters most is TestExistenceModeUnchanged. The mode
is ADDITIVE: every pre-existing caller must see the byte-identical output it saw
before. The first draft of the implementation failed exactly one shape —
`--sample-count 0`, where the baseline's `max(1, n)` reads one record and the
draft read all of them — and it was caught only by diffing the live script
against its own git HEAD baseline, not by any test written from intent. So the
class is pinned here per-value rather than "spot-checked".
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "jsonl-field-probe.py"


def _load():
    """Import the hyphenated script as a module (not importable by name)."""
    spec = importlib.util.spec_from_file_location("_jfp_under_test", str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


JFP = _load()


def _write(tmp_path, rows, name="s.jsonl"):
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return p


def _run(*args):
    """Run the script as a subprocess — the production invocation shape.

    In-process main() is exercised too, but the callers named in
    negative-conclusions.md and verification-checklist.md invoke it as a
    subprocess, and the verification checklist asserts the exit code of THAT
    shape (item 11: always exits 0)."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), *[str(a) for a in args]],
        capture_output=True, text=True,
    )


@pytest.fixture
def sample(tmp_path):
    return _write(tmp_path, [
        {"id": "a", "u": {"t": 3}, "d": "2026-08-01T00:00:00", "kind": "x"},
        {"id": "b", "u": {"t": 0}, "d": "2026-08-01T00:00:00", "kind": "x"},
        {"id": "d", "d": "2026-08-01T00:00:00", "kind": "x"},
        {"id": "e", "u": {"t": 3}, "d": "2026-08-03", "kind": "z"},
        {"id": "c", "u": {"t": None}, "d": "2026-08-02T11:04:09", "kind": "y"},
    ])


class TestExistenceModeUnchanged:
    """The default path must be byte-identical to the pre-frequency script."""

    def test_no_frequency_key_without_the_flag(self, sample):
        r = _run("--file", sample, "--field", "u.t")
        assert r.returncode == 0
        assert "frequency" not in json.loads(r.stdout)

    @pytest.mark.parametrize("sc", [None, 0, -3, 1, 2, 5, 99])
    def test_sample_count_semantics_are_preserved_per_value(self, sample, sc):
        """`max(1, n)`: values below 1 read exactly ONE record.

        Parameterised per value rather than spot-checked because the one
        regression this file exists to prevent lived at a single value (0)
        while every neighbouring value was correct.
        """
        args = ["--file", sample, "--field", "u.t"]
        if sc is not None:
            args += ["--sample-count", sc]
        out = json.loads(_run(*args).stdout)
        expected = 1 if (sc is None or sc < 1) else min(sc, 5)
        assert out["records_sampled"] == expected

    def test_explicit_null_terminal_still_reads_absent(self, sample):
        """verification-checklist item 45 — the probe and audit-schema-gate
        must agree that an explicit null is NOT present. The last record here
        carries u.t = null."""
        out = json.loads(_run("--file", sample, "--field", "u.t",
                              "--sample-count", 1).stdout)
        assert out["field_present"] is False

    def test_exit_code_is_zero_even_for_a_missing_file(self, tmp_path):
        """Diagnostic contract: always exits 0 (checklist items 2 and 11)."""
        r = _run("--file", tmp_path / "nope.jsonl", "--field", "a.b")
        assert r.returncode == 0
        assert json.loads(r.stdout)["probe_error"]


class TestFrequencyMode:
    def test_counts_shares_and_distinct(self, sample):
        out = json.loads(_run("--file", sample, "--field", "d",
                              "--frequency").stdout)
        f = out["frequency"]
        assert f["records_scanned"] == 5          # unset => ALL records
        assert f["distinct_values"] == 3
        assert f["top_values"][0]["count"] == 3
        assert f["top_value_share"] == 0.6

    def test_conservation_found_plus_missing_equals_scanned(self, sample):
        """A record missing the field is COUNTED, never silently skipped —
        otherwise a mostly-absent field is indistinguishable from a
        concentrated one (guard-2298: population beside the filtered count)."""
        f = json.loads(_run("--file", sample, "--field", "u.t",
                            "--frequency").stdout)["frequency"]
        assert f["values_found"] + f["records_missing_field"] == f["records_scanned"]
        # id=d has no u at all; id=c has an explicit null, which _get_dotted
        # reports absent by the same rb-245 semantic the existence check uses.
        assert f["records_missing_field"] == 2

    def test_repeated_cluster_respects_min_repeat(self, sample):
        f = json.loads(_run("--file", sample, "--field", "d", "--frequency",
                            "--min-repeat", 4).stdout)["frequency"]
        assert f["repeated_cluster_count"] == 0
        f2 = json.loads(_run("--file", sample, "--field", "d", "--frequency",
                             "--min-repeat", 3).stdout)["frequency"]
        assert f2["repeated_cluster_count"] == 1

    def test_bare_date_and_midnight_are_both_counted(self, sample):
        """guard-3265's precision-fallback tell: 3x midnight + 1x bare date."""
        f = json.loads(_run("--file", sample, "--field", "d",
                            "--frequency").stdout)["frequency"]
        assert f["date_only_values"] == 4
        assert f["date_only_share"] == 0.8

    def test_a_real_timestamp_is_not_counted_as_date_only(self, tmp_path):
        p = _write(tmp_path, [{"d": "2026-08-02T11:04:09"}], "t.jsonl")
        f = json.loads(_run("--file", p, "--field", "d",
                            "--frequency").stdout)["frequency"]
        assert f["date_only_values"] == 0

    def test_top_n_truncates_the_listing_but_not_the_counts(self, tmp_path):
        p = _write(tmp_path, [{"v": f"val{i}"} for i in range(25)], "n.jsonl")
        f = json.loads(_run("--file", p, "--field", "v", "--frequency",
                            "--top-n", 4).stdout)["frequency"]
        assert f["distinct_values"] == 25          # counted over everything
        assert len(f["top_values"]) == 4           # only the listing is bounded
        assert f["records_scanned"] == 25

    def test_truncated_repeated_clusters_report_the_surplus(self, tmp_path):
        """A bound must never render as a scan result (guard-3830)."""
        rows = [{"v": f"g{i}"} for i in range(6) for _ in range(2)]
        p = _write(tmp_path, rows, "r.jsonl")
        f = json.loads(_run("--file", p, "--field", "v", "--frequency",
                            "--top-n", 2).stdout)["frequency"]
        assert f["repeated_cluster_count"] == 6
        assert len(f["repeated_clusters"]) == 2
        assert f["repeated_clusters_truncated"] == 4

    def test_sample_count_narrows_the_frequency_window(self, sample):
        f = json.loads(_run("--file", sample, "--field", "d", "--frequency",
                            "--sample-count", 2).stdout)["frequency"]
        assert f["records_scanned"] == 2

    def test_empty_file_does_not_crash(self, tmp_path):
        p = tmp_path / "e.jsonl"
        p.write_text("", encoding="utf-8")
        r = _run("--file", p, "--field", "a", "--frequency")
        assert r.returncode == 0
        assert json.loads(r.stdout)["probe_error"]

    def test_human_output_renders_the_histogram(self, sample):
        r = _run("--file", sample, "--field", "d", "--frequency",
                 "--output", "human")
        assert r.returncode == 0
        assert "Distinct values: 3" in r.stdout
        assert "guard-3265" in r.stdout


class TestValueKey:
    """_value_key is where a frequency probe could manufacture the very
    concentration it exists to expose."""

    def test_true_and_one_do_not_merge(self):
        """Python hashes True == 1, so a raw-scalar Counter key would merge a
        boolean field's counts with an integer field's."""
        assert JFP._value_key(True) != JFP._value_key(1)

    def test_unhashable_values_are_keyed_not_raised(self):
        assert JFP._value_key({"b": 1, "a": 2}) == JFP._value_key({"a": 2, "b": 1})
        assert JFP._value_key([1, 2]) == "[1, 2]"

    def test_dict_valued_field_survives_end_to_end(self, tmp_path):
        p = _write(tmp_path, [{"v": {"a": 1}}, {"v": {"a": 1}}, {"v": {"a": 2}}],
                   "d.jsonl")
        r = _run("--file", p, "--field", "v", "--frequency")
        assert r.returncode == 0
        f = json.loads(r.stdout)["frequency"]
        assert f["distinct_values"] == 2
        assert f["top_values"][0]["count"] == 2

    def test_bool_and_int_stay_separate_end_to_end(self, tmp_path):
        p = _write(tmp_path, [{"v": True}, {"v": 1}, {"v": 1}], "b.jsonl")
        f = json.loads(_run("--file", p, "--field", "v",
                            "--frequency").stdout)["frequency"]
        assert f["distinct_values"] == 2
