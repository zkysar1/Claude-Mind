"""Unit tests for the uncrossed producer/consumer seam census (gap-117).

Each of the five filter steps gets its own fixture that is IDENTICAL to the
positive case except for the one property that step tests. That shape matters
here more than usual: every step is a NARROWING filter, so a broken step fails by
returning FEWER findings -- and "no uncrossed seams" is the reading a healthy
repo also produces. A test that only asserts "the positive case is found" would
stay green against a filter that had silently stopped narrowing.

The mandatory positive control (guard-1941) gets its own test for the same
reason: a dead class-name matcher makes EVERY pair read as uncrossed, which is
the loudest possible finding produced by the quietest possible bug.

Daemon-safe: pure filesystem parsing, no daemon, no world/meta writes.

Run:
  STORAGE_BACKEND=local python -m pytest core/scripts/tests/test_uncrossed_seam_census.py -q
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
CENSUS_PY = CORE_SCRIPTS / "uncrossed_seam_census.py"

sys.path.insert(0, str(CORE_SCRIPTS))
_spec = importlib.util.spec_from_file_location("uncrossed_seam_census", CENSUS_PY)
census_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(census_mod)


def build_repo(tmp_path: Path, main: dict, test: dict) -> Path:
    """Write {ClassName: java-source} maps into a src/main + src/test skeleton."""
    for rel, files in (("src/main", main), ("src/test", test)):
        root = tmp_path / rel
        root.mkdir(parents=True, exist_ok=True)
        for name, body in files.items():
            (root / f"{name}.java").write_text(body, encoding="utf-8")
    return tmp_path


# The canonical UNCROSSED shape, mirroring the real  finding:
# Producer writes a key; Consumer soft-reads it with a default; a test hand-builds
# the key; no test file names both classes.
PRODUCER = 'class Producer { void f() { payload.put("seamKey", 1); } }'
CONSUMER = 'class Consumer { void g() { var v = payload.getInteger("seamKey", 0); } }'
TEST_HAND_BUILDS = 'class ProducerTest { void t() { new JsonObject().put("seamKey", 1); Producer p; } }'
# A second test file so the positive control has something to find. It names two
# production classes but NOT the seam pair, so it cannot cross it.
CONTROL_TEST = 'class OtherTest { void t() { Producer a; Unrelated b; } }'
UNRELATED = 'class Unrelated { void h() { other.put("x", 1); } }'


def base_repo(tmp_path):
    return build_repo(
        tmp_path,
        main={"Producer": PRODUCER, "Consumer": CONSUMER, "Unrelated": UNRELATED},
        test={"ProducerTest": TEST_HAND_BUILDS, "OtherTest": CONTROL_TEST},
    )


def run(repo: Path) -> dict:
    return census_mod.census(repo, "src/main", "src/test")


def test_finds_the_uncrossed_pair(tmp_path):
    r = run(base_repo(tmp_path))
    assert r["uncrossed_class_pairs"] == [{"producer": "Producer", "consumer": "Consumer"}], r
    assert [p["key"] for p in r["uncrossed_pairs"]] == ["seamKey"]
    assert r["funnel"]["5_single_writer"] == 1


def test_a_test_naming_both_classes_makes_it_crossed(tmp_path):
    # The ONLY difference from the positive case: one test file names both halves.
    repo = build_repo(
        tmp_path,
        main={"Producer": PRODUCER, "Consumer": CONSUMER, "Unrelated": UNRELATED},
        test={"ProducerTest": TEST_HAND_BUILDS, "OtherTest": CONTROL_TEST,
              "SeamTest": 'class SeamTest { void t() { Producer p; Consumer c; } }'},
    )
    r = run(repo)
    assert r["uncrossed_pairs"] == [], r
    assert [p["key"] for p in r["crossed_pairs"]] == ["seamKey"]
    assert r["crossed_pairs"][0]["crossing_test_files"] == ["SeamTest"]


def test_a_hard_read_is_not_a_seam_this_census_reports(tmp_path):
    # Step 3. getInteger("seamKey") with NO default throws on a break, so a test
    # CAN notice by crashing -- out of scope by construction, not an oversight.
    repo = build_repo(
        tmp_path,
        main={"Producer": PRODUCER,
              "Consumer": 'class Consumer { void g() { var v = payload.getInteger("seamKey"); } }',
              "Unrelated": UNRELATED},
        test={"ProducerTest": TEST_HAND_BUILDS, "OtherTest": CONTROL_TEST},
    )
    r = run(repo)
    assert r["funnel"]["3_soft_read_by_non_writer"] == 0
    assert r["uncrossed_pairs"] == []


def test_a_key_no_test_hand_builds_is_dropped(tmp_path):
    # Step 4, the load-bearing filter. Without a hand-built fixture there is no
    # synthetic agreement to preserve, so the seam is not this census's subject.
    repo = build_repo(
        tmp_path,
        main={"Producer": PRODUCER, "Consumer": CONSUMER, "Unrelated": UNRELATED},
        test={"ProducerTest": 'class ProducerTest { void t() { Producer p; } }',
              "OtherTest": CONTROL_TEST},
    )
    r = run(repo)
    assert r["funnel"]["4_hand_built_in_a_test"] == 0
    assert r["uncrossed_pairs"] == []


def test_a_multi_writer_key_is_dropped(tmp_path):
    # Step 5. Two producers writing one key is usually a generic name collision
    # ("status", "id"), not one seam.
    repo = build_repo(
        tmp_path,
        main={"Producer": PRODUCER,
              "SecondProducer": 'class SecondProducer { void f() { other.put("seamKey", 2); } }',
              "Consumer": CONSUMER, "Unrelated": UNRELATED},
        test={"ProducerTest": TEST_HAND_BUILDS, "OtherTest": CONTROL_TEST},
    )
    r = run(repo)
    assert r["funnel"]["4_hand_built_in_a_test"] == 1, "precondition: survives step 4"
    assert r["funnel"]["5_single_writer"] == 0, "two writers must drop it at step 5"
    assert r["uncrossed_pairs"] == []


def test_same_class_read_is_not_cross_class(tmp_path):
    # Step 2. A class reading its own key crosses no seam.
    repo = build_repo(
        tmp_path,
        main={"Producer": 'class Producer { void f() { p.put("seamKey", 1); '
                          'var v = p.getInteger("seamKey", 0); } }',
              "Unrelated": UNRELATED},
        test={"ProducerTest": TEST_HAND_BUILDS, "OtherTest": CONTROL_TEST},
    )
    r = run(repo)
    assert r["funnel"]["2_cross_class"] == 0
    assert r["uncrossed_pairs"] == []


def test_positive_control_counts_tests_naming_two_production_classes(tmp_path):
    r = run(base_repo(tmp_path))
    pc = r["positive_control"]
    # OtherTest names Producer + Unrelated. ProducerTest names only Producer.
    assert pc["test_files_naming_2plus_production_classes"] == 1
    assert pc["test_files_total"] == 2
    assert pc["valid"] is True


def test_dead_matcher_invalidates_the_verdict_rather_than_reporting_findings(tmp_path):
    # guard-1941 in its exact shape: when NO test file names 2+ production
    # classes, every pair reads uncrossed. That is the loudest finding produced
    # by the quietest bug, so the run must mark itself invalid -- and say so via
    # the EXIT CODE, not only in prose a caller may not parse.
    repo = build_repo(
        tmp_path,
        main={"Producer": PRODUCER, "Consumer": CONSUMER},
        test={"ProducerTest": TEST_HAND_BUILDS},   # names one class only
    )
    r = run(repo)
    assert r["positive_control"]["valid"] is False
    assert r["uncrossed_pairs"], "the pair is still reported -- suppressing it would hide the bug"

    rc = census_mod.main(["--repo", str(repo), "--json"])
    assert rc == 3, "a dead positive control must exit 3, distinct from 0 (clean) and 1 (usage)"


def test_zero_reader_keys_are_reported_separately(tmp_path):
    # Slot (b). Unaffected by the crossing predicate: a key with no reader has no
    # reader to cross to, which is why it survived the deferral that held slot (a).
    repo = build_repo(
        tmp_path,
        main={"Producer": 'class Producer { void f() { p.put("orphanKey", 1); '
                          'p.put("seamKey", 1); } }',
              "Consumer": CONSUMER, "Unrelated": UNRELATED},
        test={"ProducerTest": TEST_HAND_BUILDS, "OtherTest": CONTROL_TEST},
    )
    r = run(repo)
    keys = [z["key"] for z in r["zero_reader_keys"]]
    assert "orphanKey" in keys, r["zero_reader_keys"]
    assert "seamKey" not in keys, "seamKey HAS a reader -- it belongs to slot (a), not (b)"


def test_non_default_source_roots_are_honoured(tmp_path):
    # Named as an EXCLUDED layer by guard-1462 and then closed rather than merely
    # documented: every other test uses the defaults, so a --main-dir/--test-dir
    # that was silently ignored would leave all of them green while the flags did
    # nothing on any repo that does not use the Maven layout.
    for rel, files in (("app/code", {"Producer": PRODUCER, "Consumer": CONSUMER,
                                     "Unrelated": UNRELATED}),
                       ("app/spec", {"ProducerTest": TEST_HAND_BUILDS,
                                     "OtherTest": CONTROL_TEST})):
        root = tmp_path / rel
        root.mkdir(parents=True, exist_ok=True)
        for name, body in files.items():
            (root / f"{name}.java").write_text(body, encoding="utf-8")

    r = census_mod.census(tmp_path, "app/code", "app/spec")
    assert r["uncrossed_class_pairs"] == [{"producer": "Producer", "consumer": "Consumer"}], r
    assert r["main_files"] == 3 and r["test_files"] == 2

    # ...and the defaults must NOT find this repo, or the flags could be inert.
    assert census_mod.census(tmp_path, "src/main", "src/test").get("error")


def test_missing_source_roots_error_rather_than_report_zero(tmp_path):
    # An absent src/test is indistinguishable from a repo with no tests if the
    # census answers 0. It must refuse instead.
    (tmp_path / "src/main").mkdir(parents=True)
    r = census_mod.census(tmp_path, "src/main", "src/test")
    assert r.get("error"), r
    assert census_mod.main(["--repo", str(tmp_path)]) == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
