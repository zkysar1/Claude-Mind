"""Regression pins for the learning-ratio open-vocabulary fix ().

DEFECT (found on ZDS as g-001-291, reproduced on Ayoai-Mind before the fix).
`learning-ratio.py` had an OPEN producer feeding a CLOSED consumer:

    load_tree_classes()  reads whatever `domain_class` string a deployment puts
                         on any _tree.yaml node          -> open
    classify()           returns that string verbatim    -> open
    count_store()        counts[cls] += 1 into a dict pre-seeded with exactly
    count_goals()        four Ayoai names                -> CLOSED, KeyError

So the framework DOCUMENTED a per-node `domain_class` field whose use AS
DOCUMENTED crashed the dashboard in any deployment that tagged a node with its
own class. Measured on both boxes: 0 nodes carried `domain_class` (0/165 on
ZDS, 0/1556 here) -- because the obvious local workaround, tagging a node, is
exactly what triggered the crash.

WHAT THESE TESTS PIN, and why each would otherwise regress silently:

1. THE CRASH ITSELF. A future edit re-seeding a literal four-key dict restores
   the KeyError. `test_arbitrary_domain_class_does_not_raise` fails loudly.
2. BYTE-IDENTITY. The fix must be invisible to deployments that tag nothing --
   that is verification outcome 3 ("existing classification behavior unchanged
   when no deployment config is supplied"), and it is what makes the change
   safe to promote downstream. A reordering or reformatting of the summary line
   would break `domain-class-gate.py`, which parses this script's stdout.
3. guard-3948 -- when a key can appear in a contract output it must appear on
   EVERY exit path. A deployment class seen in one store but not another would
   otherwise leave `per_store` / `per_source` dicts with mismatched key sets,
   and a consumer diffing them could not distinguish an absent bucket from a
   real zero.
4. THE COMPLETION OF THE FIX (rb-6682: a fix creates states the original code
   could never reach -- audit what the new state newly reaches). Once counting
   stops crashing, a deployment class lands in `totals`. If the printed line
   still names four literals, that class is counted but never shown and the
   percentages silently stop summing to 100 -- under-reporting exactly the
   domain half the dashboard exists to measure. Pinned by
   `test_discovered_class_reaches_the_printed_line`.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "learning_ratio_under_test", SCRIPTS / "learning-ratio.py")
lr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lr)


# The exact pre-fix summary shape. Hard-coded rather than derived so a change to
# the formatting logic cannot quietly redefine what "unchanged" means.
BASELINE = ("Learning ratio (artifacts): framework-meta 50% · "
            "ayoai-product 25% · npc-domain 25% · other 0%  "
            "(target: framework ≤ 25%, npc ≥ 30%)")


def test_arbitrary_domain_class_does_not_raise():
    """The regression pin: an open producer must feed an open consumer."""
    tree_classes = {"consulting-delivery": "consulting-domain"}
    assert lr.classify("consulting-delivery", tree_classes) == "consulting-domain"

    counts = lr._new_counts()
    lr._bump(counts, lr.classify("consulting-delivery", tree_classes))
    assert counts["consulting-domain"] == 1
    # the four base buckets survive alongside the new one
    for base in lr.BASE_CLASSES:
        assert base in counts


def test_base_classes_are_always_seeded():
    """The bucket dict never shrinks, so the emitted shape never shrinks."""
    assert set(lr._new_counts()) == set(lr.BASE_CLASSES)
    assert lr.BASE_CLASSES == ("framework-meta", "ayoai-product", "npc-domain", "other")


def test_align_gives_every_exit_path_identical_keys():
    """guard-3948: a key that can appear in the output appears on EVERY path."""
    totals = lr._new_counts()
    lr._bump(totals, "consulting-domain")
    seen = lr._new_counts()
    lr._bump(seen, "consulting-domain")
    unseen = lr._new_counts()          # this store never encountered the class

    order = lr._align(totals, seen, unseen)

    assert sorted(totals) == sorted(seen) == sorted(unseen)
    assert "consulting-domain" in unseen and unseen["consulting-domain"] == 0
    # a real zero is now distinguishable from an absent bucket
    assert seen["consulting-domain"] == 1
    assert set(order) == set(totals)


def test_align_order_puts_base_first_and_other_last():
    """Deployment classes slot between the base classes and the catch-all."""
    d = lr._new_counts()
    lr._bump(d, "zeta-domain")
    lr._bump(d, "alpha-domain")
    order = lr._align(d)
    assert order == ["framework-meta", "ayoai-product", "npc-domain",
                     "alpha-domain", "zeta-domain", "other"]
    assert order[-1] == "other"


def test_align_with_no_deployment_classes_is_the_original_order():
    """The no-op case -- what every existing deployment gets."""
    assert lr._align(lr._new_counts()) == list(lr.BASE_CLASSES)


def test_summary_line_byte_identical_without_deployment_classes(monkeypatch, capsys):
    """Verification outcome 3, and what keeps domain-class-gate.py parsing."""
    monkeypatch.setattr(lr, "load_tree_classes", lambda: {})
    monkeypatch.setattr(lr, "load_targets", lambda: (25, 30))

    # main() sums two stores (guardrails, then reasoning_bank); make them differ
    # so the assertion exercises four distinct percentages rather than one.
    # 2/1/1/0 of 4 -> 50/25/25/0.
    calls = {"n": 0}

    def fake_count_store(path, tree_classes):
        calls["n"] += 1
        c = lr._new_counts()
        if calls["n"] == 1:
            c["framework-meta"] = 2
        else:
            c["ayoai-product"] = 1
            c["npc-domain"] = 1
        return c

    monkeypatch.setattr(lr, "count_store", fake_count_store)

    lr.main(["--scope", "artifacts"])
    line = capsys.readouterr().out.splitlines()[0]
    assert line == BASELINE, f"summary line drifted:\n  got {line!r}\n  want {BASELINE!r}"


def test_discovered_class_reaches_the_printed_line(monkeypatch, capsys):
    """rb-6682: the fix's new state must not silently under-report.

    Counted-but-unprinted is the failure this guards: the percentages would stop
    summing to 100 and the domain half would vanish from the dashboard.
    """
    monkeypatch.setattr(lr, "load_tree_classes", lambda: {})
    monkeypatch.setattr(lr, "load_targets", lambda: (25, 30))

    def fake_count_store(path, tree_classes):
        c = lr._new_counts()
        lr._bump(c, "consulting-domain")
        return c

    monkeypatch.setattr(lr, "count_store", fake_count_store)
    lr.main(["--scope", "artifacts", "--json"])
    out = capsys.readouterr().out
    line = out.splitlines()[0]

    assert "consulting-domain 100%" in line, f"deployment class missing from line: {line!r}"
    # every base class still shown, and the catch-all still last
    for base in lr.BASE_CLASSES:
        assert f"{base} " in line
    assert line.index("consulting-domain") < line.index("other ")

    # and the percentages still account for the whole population
    import json as _json
    payload = _json.loads(out[out.index("{"):])
    assert sum(payload["pct"].values()) == 100


def test_goals_scope_also_survives_an_unknown_class(monkeypatch, tmp_path):
    """count_goals has its own bucket dicts -- the fix must cover both scopes.

    Paths are redirected at the MODULE level because count_goals resolves its
    sources from lr.WORLD_DIR / lr.AGENT_DIR, not from arguments. Without the
    redirect this test opened the live queues: read-only and content-independent,
    so it passed everywhere while exercising whichever branch that box happened
    to have (fresh-eyes finding echo-fec-new-test-reads-live-queues).
    """
    monkeypatch.setattr(lr, "WORLD_DIR", tmp_path)
    monkeypatch.setattr(lr, "AGENT_DIR", tmp_path)
    fixture = tmp_path / "aspirations.jsonl"
    fixture.write_text(json.dumps({
        "id": "asp-999", "status": "active",
        "goals": [{"id": "g-999-01", "status": "pending", "category": "anything"},
                  {"id": "g-999-02", "status": "completed", "category": "anything"}],
    }) + "\n", encoding="utf-8")
    monkeypatch.setattr(lr, "classify", lambda cat, tc: "consulting-domain")

    counts, per_source = lr.count_goals({})

    # world and agent both resolve to tmp_path, so the single pending goal is
    # counted once per source -- a real assertion on the parse loop, not just
    # "it did not raise". The completed goal must NOT be counted.
    assert counts["consulting-domain"] == 2
    assert per_source["world"]["consulting-domain"] == 1
    assert per_source["agent"]["consulting-domain"] == 1
    for base in lr.BASE_CLASSES:
        assert base in counts and base in per_source["world"]


def test_non_string_domain_class_is_coerced(monkeypatch, tmp_path):
    """`domain_class: yes` is YAML for True -- it must not poison sorted().

    Measured before the coercion: one real class plus one bool raised
    `TypeError: '<' not supported between instances of 'bool' and 'str'` inside
    _align. A bool ALONE did not raise (a 1-element sorted() never compares) --
    it printed `True 100%` into the summary line domain-class-gate.py parses.
    A YAML list was worse: unhashable, dying at _bump. So the mixed case is the
    one worth pinning; the others ride along.

    Written as real YAML through a real safe_load rather than a stubbed return
    value, so the test also proves its own premise -- that `yes` parses as a
    bool -- instead of asserting it.
    """
    tree = tmp_path / "_tree.yaml"
    tree.write_text(
        "nodes:\n"
        "  n1:\n    domain_class: yes\n"                  # YAML bool True
        "  n2:\n    domain_class: 7\n"                    # int
        "  n3:\n    domain_class: [a]\n"                  # unhashable pre-fix
        "  n4:\n    domain_class: consulting-domain\n",   # the ordinary case
        encoding="utf-8")
    monkeypatch.setattr(lr, "TREE_PATH", tree)

    classes = lr.load_tree_classes()
    assert all(isinstance(v, str) for v in classes.values()), classes

    d = lr._new_counts()
    for key in classes:
        lr._bump(d, lr.classify(key, classes))
    order = lr._align(d)          # the line that used to raise TypeError
    assert "consulting-domain" in order and "True" in order
    assert order[-1] == "other"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
