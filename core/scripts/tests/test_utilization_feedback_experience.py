""" — experience records must reach the ENFORCED utilization writer.

Before this lane existed, `retrieval_stats.times_useful` had no enforced writer
at all. Its only two writers were honor-system SKILL.md pseudocode lines
(replay:517, reflect-on-outcome:1160), and empirically neither fired: measured
2026-08-11 (echo, hostname cc-03, uname -r 6.8.0-137-generic), the 40
MOST-retrieved experience records — `retrieval_count` 26 through 38 — all read
`times_useful` 0 and `utility_ratio` 0.0. Two consumers were silently disabled by
that: the encoding-weight adaptation (no high/low bucket exists when every record
is 0.0) and the archive sweep's never-archive-high-value guard
(`retrieval_count >= 5 AND utility_ratio >= 0.5`), which cannot qualify any
record and so archives valuable experiences on the same timer as noise.

The cause was a store-coverage asymmetry, not a forgetful LLM: tree nodes,
reasoning-bank entries and guardrails are classified by `utilization-feedback.py`
(wired into iteration-close, gated by phase-4-26-gate), and the experience store
was never a type it knew about.

These tests pin the pieces that a future refactor would silently drop.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _load_ufb():
    """utilization-feedback.py is hyphenated, so import it by path."""
    spec = importlib.util.spec_from_file_location(
        "ufb_under_test", SCRIPTS / "utilization-feedback.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def ufb():
    return _load_ufb()


# --------------------------------------------------------------------------
# The counter-name mapping
# --------------------------------------------------------------------------

def test_helpful_maps_to_times_useful(ufb):
    """The rb/guardrail counter is `times_helpful`; the experience counter is
    `times_useful`. A refactor that assumes one name for both silently writes a
    key nothing reads."""
    assert ufb.EXPERIENCE_STAT_FOR_FIELD["times_helpful"] == "times_useful"


def test_noise_keeps_its_name(ufb):
    assert ufb.EXPERIENCE_STAT_FOR_FIELD["times_noise"] == "times_noise"


def test_inferred_does_not_fold_into_times_useful(ufb):
    """THE load-bearing assertion of this file.

    `utility_ratio` is derived from `times_useful` alone, and both live
    consumers read that ratio as evidence a judgement marked the record useful.
    Folding heuristic `--infer` hits into it would change what the ratio means
    with no consumer being told — the vacuous-signal trap the goal's own caution
    names (rb-7404). Inferred hits get their own key instead.
    """
    mapped = ufb.EXPERIENCE_STAT_FOR_FIELD["times_inferred_helpful"]
    assert mapped == "times_inferred_useful"
    assert mapped != "times_useful"


def test_unmapped_counters_are_absent(ufb):
    """`times_active` / `times_skipped` / `times_cited` are rb/guardrail-only
    signals. They must not acquire an experience-side mapping by accident —
    absence here is what makes the writer no-op rather than invent a counter."""
    for field in ("times_active", "times_skipped", "times_cited",
                  "retrieval_count"):
        assert field not in ufb.EXPERIENCE_STAT_FOR_FIELD


# --------------------------------------------------------------------------
# Dispatch: experiences must NOT go through /v1/store/increment
# --------------------------------------------------------------------------

def test_experience_type_routes_to_its_own_writer(ufb, monkeypatch):
    """/v1/store/increment takes a dotted `utilization.*` path against a store
    registered with the rb/guardrail schema, so it cannot reach a counter that
    lives under `retrieval_stats`. Routing an experience there would fail
    silently-ish rather than write."""
    called = {}
    monkeypatch.setattr(ufb, "_increment_experience_stat",
                        lambda i, f: called.setdefault("exp", (i, f)))
    monkeypatch.setattr(ufb._rt, "store_increment",
                        lambda *a, **k: called.setdefault("store", a))

    ufb.increment_supplementary("exp-abc", "experience", "times_helpful")

    assert called.get("exp") == ("exp-abc", "times_helpful")
    assert "store" not in called, "experience must not hit /v1/store/increment"


@pytest.mark.parametrize("item_type,store", [
    ("reasoning_bank", "reasoning-bank"),
    ("guardrail", "guardrails"),
])
def test_existing_types_unchanged(ufb, monkeypatch, item_type, store):
    """Regression pin: adding the experience branch must not perturb the two
    types that already worked."""
    seen = []
    monkeypatch.setattr(ufb._rt, "store_increment",
                        lambda s, i, f: seen.append((s, i, f)))
    ufb.increment_supplementary("x-1", item_type, "times_helpful")
    assert seen == [(store, "x-1", "utilization.times_helpful")]


def test_pattern_signature_still_noops(ufb, monkeypatch):
    """pattern_signatures have no utilization increment path and must stay a
    no-op — the `else: return` branch is deliberate, not an oversight."""
    seen = []
    monkeypatch.setattr(ufb._rt, "store_increment",
                        lambda *a: seen.append(a))
    monkeypatch.setattr(ufb, "_increment_experience_stat",
                        lambda *a: seen.append(a))
    ufb.increment_supplementary("sig-1", "pattern_signature", "times_helpful")
    assert seen == []


# --------------------------------------------------------------------------
# The writer itself
# --------------------------------------------------------------------------

def _fake_rt(ufb, monkeypatch, record, writes):
    """Stub _rt.rt_call for the read-modify-write pair."""
    def rt_call(method, path, query=None, body=None, headers=None):
        if path == "/v1/experience/read":
            return json.dumps(record)
        if path == "/v1/experience/update-field":
            writes.append(dict(query))
            return json.dumps({"ok": True})
        raise AssertionError(f"unexpected path {path}")
    monkeypatch.setattr(ufb._rt, "rt_call", rt_call)


def test_writer_uses_the_whole_object_shape(ufb, monkeypatch):
    """The DOTTED form (`field=retrieval_stats.times_useful`) is rejected by the
    daemon with {"error": "dotted_field_rejected"}; the whole-object form is
    accepted AND triggers the server-side utility_ratio recompute. Both were
    probed on a live record 2026-08-11. guard-2645 documents the inverse — that
    the whole-object form skips the recompute — which was true before g-115-4969
    landed and is now stale. This pins the shape so it is not "restored".
    """
    writes = []
    _fake_rt(ufb, monkeypatch,
             {"id": "exp-x", "retrieval_stats": {
                 "retrieval_count": 4, "times_useful": 0, "times_noise": 0,
                 "utility_ratio": 0.0, "last_retrieved": None}},
             writes)

    ufb._increment_experience_stat("exp-x", "times_helpful")

    assert len(writes) == 1
    assert writes[0]["field"] == "retrieval_stats", \
        "must write the whole object; the dotted path is rejected by the daemon"
    assert "." not in writes[0]["field"]
    assert json.loads(writes[0]["value"])["times_useful"] == 1


def test_writer_reads_before_writing(ufb, monkeypatch):
    """Read-modify-write, not blind set: an existing count must survive."""
    writes = []
    _fake_rt(ufb, monkeypatch,
             {"id": "exp-x", "retrieval_stats": {
                 "retrieval_count": 9, "times_useful": 3, "times_noise": 1,
                 "utility_ratio": 0.33, "last_retrieved": "2026-08-01"}},
             writes)

    ufb._increment_experience_stat("exp-x", "times_helpful")

    blob = json.loads(writes[0]["value"])
    assert blob["times_useful"] == 4
    assert blob["retrieval_count"] == 9, "denominator must not be disturbed"
    assert blob["times_noise"] == 1
    assert blob["last_retrieved"] == "2026-08-01"


def test_writer_skips_record_without_stats_block(ufb, monkeypatch):
    """A record with no `retrieval_stats` was never bumped by retrieve.py
    either. Synthesising a block here would manufacture a denominator that no
    retrieval produced — so the writer declines rather than invents."""
    writes = []
    _fake_rt(ufb, monkeypatch, {"id": "exp-x"}, writes)
    ufb._increment_experience_stat("exp-x", "times_helpful")
    assert writes == []


def test_writer_noops_on_unmapped_field(ufb, monkeypatch):
    writes = []
    _fake_rt(ufb, monkeypatch,
             {"id": "exp-x", "retrieval_stats": {"retrieval_count": 1,
                                                 "times_useful": 0}},
             writes)
    ufb._increment_experience_stat("exp-x", "times_active")
    assert writes == []


def test_writer_is_fail_soft_on_daemon_error(ufb, monkeypatch, capsys):
    """Fail-soft in the same direction as its siblings: a daemon error prints to
    stderr and returns. Silence is the one behaviour to avoid — a swallowed
    write is how `times_inferred_helpful` once dropped for two sessions."""
    def boom(*a, **k):
        raise ufb._rt.RtError("daemon down")
    monkeypatch.setattr(ufb._rt, "rt_call", boom)

    ufb._increment_experience_stat("exp-x", "times_helpful")  # must not raise

    assert "exp-x" in capsys.readouterr().err


# --------------------------------------------------------------------------
# Producer side: the session must carry WHICH experiences, not just how many
# --------------------------------------------------------------------------

def test_producer_emits_experience_supplementary_items():
    """`counts.experiences` has been tallied since v1 while no loop appended the
    experiences themselves, so the session recorded HOW MANY were retrieved and
    never WHICH — and utilization-feedback classifies `supplementary_items`, not
    `counts`. Pinned structurally: all four sibling loops must be present."""
    # SCRIPTS is core/scripts, so parents[1] is PROJECT_ROOT (parents[0] is core/).
    src = (SCRIPTS.parents[1] / "mind_api" / "src" / "endpoints"
           / "retrieve.py").read_text(encoding="utf-8")
    for bucket in ("meta_lessons", "guardrails", "pattern_signatures",
                   "experiences"):
        assert f"for item in {bucket}:" in src, \
            f"supplementary loop for {bucket} is missing"
    assert '{"id": iid, "type": "experience"}' in src


def test_experience_text_falls_through_to_summary():
    """`_item_text_for_tokens` has no experience branch and falls through to its
    `summary` default — which is exactly right, because that IS the experience
    record's text field. Pinned so the default is not "tightened" into a raise.
    """
    import importlib
    r = importlib.import_module("retrieve")
    rec = {"id": "exp-x", "summary": "a distinctive summary phrase"}
    assert r._item_text_for_tokens(rec, "experience") == \
        "a distinctive summary phrase"
