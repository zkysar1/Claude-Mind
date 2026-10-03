""" — the curator demotion must land in the store consolidation reads.

THE DEFECT (zeta, cc-02, g-001-07 spark 2026-09-29): the curator quality gate
demoted a rejected insight to the single scalar WM slot `curator_overflow`
(via `wm-set.sh`), but /aspirations-consolidate reads a DIFFERENT store —
`agents/<agent>/session/overflow-queue.yaml` (Step 0.1 triage gate + Overflow
Queue Management) — so the demoted insight had no reader, and `wm-set`
REPLACES the slot, so a second rejection in one session overwrote the first.
bravo measured the third half live 2026-09-30: `wm-prune.sh` age-evicted the
slot after 120 minutes (evicted_slots [{"slot": "curator_overflow",
"minutes_stale": 158}]), so even a late reader would find it gone.

THE FIX UNDER TEST: `core/scripts/overflow-queue-append.py` APPENDS the
demoted insight to overflow-queue.yaml — the file consolidation actually reads
(outcome 1), an append never replaces so two rejections both survive
(outcome 2, the goal's positive control), and the file is a session-manifest
file (sync_tier: continuity), not a WM slot, so wm-prune cannot age it out.

Every predicate assertion drives the PRODUCTION script
`overflow-queue-append.py` through the `MIND_AGENT_DIR` test seam rather than
re-deriving its body in the probe (guard-4323): a matcher that re-implements
the application validates a fix the real caller applies differently —
silently green. The reader half is pinned by asserting that
`consolidation-precheck.py` — the store consolidation's precheck counts from —
counts the appended items in its `overflow_queue` field on the SAME tmp agent
dir (outcome 1: "a test that demotes one and finds it in consolidation's
candidate list").

Run:
  MIND_AGENT_DIR=(unset) python3 -m pytest \
    core/scripts/tests/test_overflow_queue_append.py -q
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_SCRIPTS = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _SCRIPTS.parent.parent
APPENDER = _SCRIPTS / "overflow-queue-append.py"
PRECHECK = _SCRIPTS / "consolidation-precheck.py"

# Python interpreter that runs the production scripts. On this deployment the
# box uses `py -3` (Microsoft Store `python3` stub, exit 49); inside
# core/scripts the shim makes `python3` canonical (CLAUDE.md Python Invocation).
PY = sys.executable or "python3"


@pytest.fixture
def agent_dir(tmp_path):
    """A tmp agent dir with the session/ subdir the queue lives in."""
    d = tmp_path / "agent"
    (d / "session").mkdir(parents=True)
    return d


def _append(agent_dir, item: dict):
    """Drive the REAL appender with AGENT_DIR pointed at the tmp dir.

    MIND_AGENT_DIR is _paths.py's documented test-only override seam (Tier 4:
    `MIND_AGENT_DIR env (test override) > MIND_AGENT under PROJECT_ROOT`); it
    keeps the write off the live agents/ tree. cwd is the project root so the
    script's own `sys.path.insert(dirname)` + `from _paths import AGENT_DIR`
    resolve exactly as in production.
    """
    env = dict(os.environ)
    env["MIND_AGENT_DIR"] = str(agent_dir)
    env.pop("BODY_ROLE", None)
    r = subprocess.run(
        [PY, str(APPENDER), "--"],
        input=json.dumps(item),
        capture_output=True, text=True, timeout=60, env=env,
        cwd=str(_PROJECT_ROOT),
    )
    return r


def _queue(agent_dir) -> list:
    p = Path(agent_dir) / "session" / "overflow-queue.yaml"
    if not p.exists():
        return []
    with open(p, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    assert isinstance(doc, list), f"queue file must be a top-level list, got {type(doc)}"
    return doc


def _precheck_overflow_count(agent_dir) -> int:
    """consolidation-precheck.py's own count of the queue — the store
    consolidation reads. This is the reader half of outcome 1."""
    env = dict(os.environ)
    env["MIND_AGENT_DIR"] = str(agent_dir)
    env.pop("BODY_ROLE", None)
    r = subprocess.run(
        [PY, str(PRECHECK), "--"],
        capture_output=True, text=True, timeout=30, env=env,
        cwd=str(_PROJECT_ROOT),
    )
    out = r.stdout.strip().splitlines()
    assert out, f"precheck printed nothing: rc={r.returncode} stderr={r.stderr[:300]}"
    verdict = json.loads(out[0])
    return verdict["overflow_queue"]


# ── outcome 1: a demoted insight reaches the store consolidation reads ──────

def test_demoted_insight_reaches_consolidation_store(agent_dir):
    """Demote one insight; the precheck that consolidation's triage gate runs
    must count it in overflow_queue (it competes with new items next session)."""
    item = {
        "observation": "concrete fact: the floor keyword 割腕 misses 割过腕",
        "target_node": "crisis-screener-keyword-floor",
        "curator_score": 0.31,
        "reason": "below_threshold",
        "source_goal": "g-335-1718",
        "category": "vinheim-safety",
    }
    r = _append(agent_dir, item)
    assert r.returncode == 0, f"appender rc={r.returncode}: {r.stderr[:300]}"
    payload = json.loads(r.stdout)
    assert payload["ok"] is True
    assert payload["items_total"] == 1

    # the file is a top-level list (the branch consolidation-precheck reads first)
    q = _queue(agent_dir)
    assert len(q) == 1
    assert q[0]["observation"] == item["observation"]
    assert q[0]["target_node"] == item["target_node"]
    assert q[0]["curator_score"] == 0.31
    # per-item fields consolidation's Overflow Queue Management expects
    assert q[0]["original_score"] == 0.31
    assert q[0]["current_score"] == 0.31
    assert q[0]["deferred_count"] == 1
    assert q[0]["first_seen"]
    assert q[0]["session_first_seen"]
    # provenance: this came from the curator gate, not a consolidation deferral
    assert q[0]["origin"] == "curator_gate"
    assert q[0]["source_goal"] == "g-335-1718"
    assert q[0]["category"] == "vinheim-safety"

    # THE READER: consolidation's own precheck counts it
    assert _precheck_overflow_count(agent_dir) == 1


# ── outcome 2: two rejections in one session are BOTH retained ──────────────

def test_two_rejections_both_retained(agent_dir):
    """Positive control against the single-slot wm-set overwrite: the second
    append must not clobber the first."""
    a = {"observation": "first rejection", "target_node": "node-a",
         "curator_score": 0.30}
    b = {"observation": "second rejection", "target_node": "node-b",
         "curator_score": 0.29}
    assert _append(agent_dir, a).returncode == 0
    assert _append(agent_dir, b).returncode == 0
    q = _queue(agent_dir)
    assert len(q) == 2, f"expected BOTH rejections, got {len(q)}"
    assert [x["observation"] for x in q] == ["first rejection", "second rejection"]
    # order preserved (append order), not replaced
    assert q[0]["target_node"] == "node-a"
    assert q[1]["target_node"] == "node-b"
    assert _precheck_overflow_count(agent_dir) == 2


# ── no age-eviction: the demotion never creates a WM slot ──────────────────

def test_demotion_never_touches_working_memory(agent_dir):
    """The 120-minute loss (bravo live, minutes_stale=158) exists because the
    OLD demotion wrote a scalar WM slot, and wm.py's production eviction
    predicate age-nulls any scalar slot (`slot_name not in ARRAY_SLOTS`)
    untouched past evict_threshold_minutes. The new writer's only write target
    is overflow-queue.yaml — a registered session-manifest file (sync_tier:
    continuity, recovery_action: preserve) that wm-prune does not age — so
    working-memory.yaml must be byte-identical before and after the demotion,
    even when it already holds the old slot's content."""
    wm_path = Path(agent_dir) / "session" / "working-memory.yaml"
    wm_path.write_text(
        "slots:\n  curator_overflow: 'stale parked insight'\n"
        "  active_context: 'x'\n",
        encoding="utf-8",
    )
    before = wm_path.read_text(encoding="utf-8")
    r = _append(agent_dir, {"observation": "o", "target_node": "n",
                            "curator_score": 0.3})
    assert r.returncode == 0, f"appender rc={r.returncode}: {r.stderr[:300]}"
    assert wm_path.read_text(encoding="utf-8") == before, (
        "the demotion must not write any WM slot — a scalar slot is age-"
        "evicted after 120 min (the defect this fix removes)"
    )
    q = _queue(agent_dir)
    assert len(q) == 1  # the item landed in the file, not a slot


# ── verified_values ride through: evidence tokens survive to the reader ─────

def test_verified_values_ride_through_to_reader(agent_dir):
    """A demoted insight may carry exact evidence tokens (verified_values).
    They must land on the item in the queue file UNCHANGED — the reader
    (consolidation) encodes from those tokens, not from re-verified prose,
    so a truncated or dropped field would silently downgrade the evidence."""
    item = {
        "observation": "credential lane fixed on prod build 704f3b9",
        "target_node": "sibling-node",
        "curator_score": 0.31,
        "source_goal": "g-335-1681",
        "verified_values": {
            "prod_build": "704f3b9bd79b3801d1d5dc39db8076033e404731",
            "maxPropagationSeconds": 360,
        },
    }
    assert _append(agent_dir, item).returncode == 0
    q = _queue(agent_dir)
    assert len(q) == 1
    assert q[0]["verified_values"] == item["verified_values"]
    assert q[0]["verified_values"]["maxPropagationSeconds"] == 360
    assert _precheck_overflow_count(agent_dir) == 1


# ── refusal: an unreadable existing file is NOT clobbered ───────────────────

def test_refuses_to_clobber_unreadable_existing_queue(agent_dir):
    """If the queue file exists in a shape the reader cannot consume, the
    appender must refuse (exit 1) and leave the file untouched — clobbering
    unreadable prior work would be the same silent loss this fix removes."""
    q = Path(agent_dir) / "session" / "overflow-queue.yaml"
    q.write_text("just: a scalar\nnot: [a list]\n", encoding="utf-8")
    before = q.read_text(encoding="utf-8")
    r = _append(agent_dir, {"observation": "x", "target_node": "n",
                            "curator_score": 0.2})
    assert r.returncode == 1, f"expected refusal, rc={r.returncode}"
    payload = json.loads(r.stdout)
    assert payload["ok"] is False
    assert "refusing to overwrite" in payload["error"]
    assert q.read_text(encoding="utf-8") == before  # untouched


# ── refusal: a pre-existing list shape APPENDS (does not replace) ───────────

def test_appends_to_existing_list_without_dropping_items(agent_dir):
    """Consolidation may already have written deferred items; a curator
    demotion must append to them, not replace them."""
    q = Path(agent_dir) / "session" / "overflow-queue.yaml"
    existing = [{"observation": "prior deferred", "target_node": "prior",
                 "curator_score": 0.35, "original_score": 0.35,
                 "current_score": 0.35, "deferred_count": 2,
                 "first_seen": "2026-09-29T20:00:00",
                 "session_first_seen": "2026-09-29T20:00:00",
                 "origin": "consolidation"}]
    yaml.safe_dump(existing, open(q, "w", encoding="utf-8"))
    r = _append(agent_dir, {"observation": "new demotion", "target_node": "n",
                            "curator_score": 0.25})
    assert r.returncode == 0
    out = _queue(agent_dir)
    assert len(out) == 2
    assert out[0]["observation"] == "prior deferred"  # not dropped
    assert out[1]["observation"] == "new demotion"
    assert _precheck_overflow_count(agent_dir) == 2


# ── input validation ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    {"target_node": "n", "curator_score": 0.3},          # missing observation
    {"observation": "x", "curator_score": 0.3},          # missing target_node
    {"observation": "x", "target_node": "n"},            # missing score
    {"observation": "x", "target_node": "n",
     "curator_score": "high"},                           # non-numeric score
    {"observation": "", "target_node": "n",
     "curator_score": 0.3},                              # empty observation
    {"observation": "x", "target_node": "n",
     "curator_score": True},                             # bool is not a number
])
def test_refuses_invalid_items(agent_dir, bad):
    r = _append(agent_dir, bad)
    assert r.returncode == 1
    payload = json.loads(r.stdout)
    assert payload["ok"] is False
    assert _queue(agent_dir) == []  # nothing written


def test_refuses_malformed_stdin(agent_dir):
    env = dict(os.environ)
    env["MIND_AGENT_DIR"] = str(agent_dir)
    env.pop("BODY_ROLE", None)
    r = subprocess.run([PY, str(APPENDER), "--"], input="not json",
                       capture_output=True, text=True, timeout=60, env=env,
                       cwd=str(_PROJECT_ROOT))
    assert r.returncode == 1
    payload = json.loads(r.stdout)
    assert payload["ok"] is False


# ── no agent bound ───────────────────────────────────────────────────────────

def test_refuses_when_no_agent_bound(tmp_path, monkeypatch):
    """AGENT_DIR is None when no agent is resolvable; the appender must refuse
    rather than write to an arbitrary location."""
    env = dict(os.environ)
    env.pop("MIND_AGENT_DIR", None)
    env.pop("MIND_AGENT", None)
    r = subprocess.run([PY, str(APPENDER), "--"],
                       input=json.dumps({"observation": "x", "target_node": "n",
                                         "curator_score": 0.3}),
                       capture_output=True, text=True, timeout=60, env=env,
                       cwd=str(_PROJECT_ROOT))
    # rc 1 (refused) is the pass; rc 0 would mean it wrote somewhere it should
    # not have. (A box where MIND_AGENT resolves to a live agent by other
    # means would make this test skip-false, so assert the refused shape when
    # it is refused.)
    assert r.returncode == 1, (
        f"expected refusal with no agent bound, rc={r.returncode} "
        f"stdout={r.stdout[:200]} stderr={r.stderr[:200]}"
    )


# ── outcome 3: the demotion docs name the real store, not the dead slot ────

_DOC_FILES = {
    "state-update": _PROJECT_ROOT / ".claude" / "skills" / "aspirations-state-update" / "SKILL.md",
    "encoding-digest": _PROJECT_ROOT / "core" / "config" / "encoding-protocol-digest.md",
    "close-digest": _PROJECT_ROOT / "core" / "config" / "iteration-close-digest.md",
}


def test_docs_prescribe_the_real_store_not_the_dead_slot():
    """Outcome 3: the false consumer claims are corrected in place. The old
    docs prescribed piping the demotion into `wm-set.sh curator_overflow`
    (no reader, clobbered, age-evicted). They must now name the append
    helper / the file consolidation reads. (Historical mention of the old
    slot name in a g-115-11580 annotation is fine; re-prescribing the old
    CALL is the regression.)"""
    for label, p in _DOC_FILES.items():
        text = p.read_text(encoding="utf-8")
        assert "wm-set.sh curator_overflow" not in text, (
            f"{label}: still prescribes the dead single-slot demotion call")
    # each doc names the real mechanism
    assert "overflow-queue-append.py" in _DOC_FILES["state-update"].read_text(encoding="utf-8")
    assert "overflow-queue.yaml" in _DOC_FILES["encoding-digest"].read_text(encoding="utf-8")
    assert "overflow-queue-append.py" in _DOC_FILES["close-digest"].read_text(encoding="utf-8")
