"""Regression pins for spark_capture write-time key normalization ().

WHY THIS EXISTS. Worker Bodies append their observations to the `spark_capture`
WM slot; the reducer's aspirations-spark Phase 6.5 replays them. Schema, writer
instruction and reader ALL name the key `observation` -- so a deviant entry is
not a reader/writer disagreement, it is WRITE-TIME IMPROVISATION by an LLM
worker that reached for `lesson` / `insight` / `summary` instead. Such an entry
reads as EMPTY and its learning is dropped with no error anywhere.

The fix normalizes at the WRITE CHOKEPOINT rather than adding a reader-side
fallback chain, because a fallback list is an ENUMERATION CLAIM (guard-3970):
it asserts those are all the names, and an LLM is free to invent an 11th one
tomorrow. Enumerating the CLOSED METADATA set the schema owns is immune to a
new content-key name by construction. `test_promotes_an_invented_key` is the
fixture that discriminates the two designs -- a fallback implementation fails
it, this one passes.

BOTH TWINS ARE PINNED (guard-742). `core/scripts/wm.py` is the CLI fallback;
`mind_api/src/endpoints/wm_write.py` is the LIVE daemon path. Patching one
alone is inert in production, so every fixture below runs against BOTH, and
the daemon half is executed from ITS OWN SOURCE (AST-extracted) rather than
asserted to merely exist -- a same-named function with drifted logic passes a
presence check and fails these.

Run:
  STORAGE_BACKEND=local python3 -m pytest core/scripts/tests/test_spark_capture_normalize.py -q
"""
from __future__ import annotations

import ast
import copy
import sys
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

import wm  # noqa: E402

DAEMON_TWIN = PROJECT_ROOT / "mind_api" / "src" / "endpoints" / "wm_write.py"
WANTED = {
    "_SPARK_CAPTURE_META_KEYS",
    "_SPARK_CAPTURE_MIN_CONTENT",
    "_normalize_spark_capture_entry",
}


def _load_daemon_normalizer():
    """Exec the daemon twin's normalizer from its own source.

    wm_write.py cannot be imported standalone (FastAPI + per-request ctx), so
    lift exactly the three nodes under test out of its AST and exec them in a
    bare namespace. This runs the daemon's REAL logic -- drift between the
    twins shows up as a behavioural failure, not a missing-name failure.
    """
    tree = ast.parse(DAEMON_TWIN.read_text(encoding="utf-8"))
    picked = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in WANTED:
            picked.append(node)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id in WANTED:
                    picked.append(node)
    ns: dict = {"frozenset": frozenset}
    exec(compile(ast.Module(body=picked, type_ignores=[]), str(DAEMON_TWIN), "exec"), ns)
    missing = WANTED - set(ns)
    assert not missing, (
        f"daemon twin {DAEMON_TWIN} is missing {sorted(missing)} -- the CLI twin "
        f"core/scripts/wm.py has them. Patching one twin alone is inert in "
        f"production (guard-742)."
    )
    return ns["_normalize_spark_capture_entry"]


DAEMON_NORMALIZE = _load_daemon_normalizer()
IMPLS = pytest.mark.parametrize(
    "normalize",
    [pytest.param(wm._normalize_spark_capture_entry, id="cli-wm.py"),
     pytest.param(DAEMON_NORMALIZE, id="daemon-wm_write.py")],
)

LONG = "a genuine worker observation that is comfortably past the minimum"
assert len(LONG) >= wm._SPARK_CAPTURE_MIN_CONTENT  # fixture sanity


def _entry(**extra) -> dict:
    """Production shape: the append endpoint stamps `_item_ts` on every dict
    item, so a fixture without it exercises a shape production never emits
    (guard-920)."""
    base = {
        "goal_id": "g-306-999",
        "category": "cross-box-bodies",
        "sq_trigger": None,
        "_item_ts": "2026-08-25T00:00:00",
    }
    base.update(extra)
    return base


# --- DIRECTION 1: a deviant content key IS read -----------------------------

@IMPLS
@pytest.mark.parametrize("key", ["lesson", "insight", "summary", "finding", "note"])
def test_promotes_a_deviant_content_key(normalize, key):
    item = _entry(**{key: LONG})
    assert normalize(item) == key
    assert item["observation"] == LONG
    assert item["observation_normalized_from"] == key


@IMPLS
def test_promotes_an_invented_key(normalize):
    """The property a reader-side fallback chain CANNOT have.

    `takeaway` is in no enumeration anywhere. A fallback implementation would
    return None here and drop the observation; normalizing against the closed
    METADATA set promotes it. This fixture is the design discriminator.
    """
    item = _entry(takeaway=LONG)
    assert normalize(item) == "takeaway"
    assert item["observation"] == LONG


@IMPLS
def test_promotes_the_longest_when_several_compete(normalize):
    short_but_valid = "x" * (wm._SPARK_CAPTURE_MIN_CONTENT + 1)
    item = _entry(note=short_but_valid, lesson=LONG + " and then some more text")
    assert normalize(item) == "lesson"
    assert item["observation"].startswith("a genuine worker observation")


@IMPLS
def test_promotes_when_observation_is_present_but_blank(normalize):
    """Blank-but-present must behave like absent -- it reads as empty too."""
    item = _entry(observation="   ", lesson=LONG)
    assert normalize(item) == "lesson"
    assert item["observation"] == LONG


# --- DIRECTION 2: a genuinely empty entry STAYS empty -----------------------

@IMPLS
def test_metadata_only_entry_is_left_alone(normalize):
    item = _entry()
    before = copy.deepcopy(item)
    assert normalize(item) is None
    assert item == before, "a genuinely empty entry must not be mutated"
    assert "observation" not in item


@IMPLS
def test_canonical_entry_is_a_noop(normalize):
    item = _entry(observation=LONG)
    before = copy.deepcopy(item)
    assert normalize(item) is None, "already canonical -- nothing to promote"
    assert item == before
    assert "observation_normalized_from" not in item


@IMPLS
def test_below_threshold_string_is_not_promoted(normalize):
    """Guards against promoting an incidental short scalar into content."""
    item = _entry(lesson="too short")
    assert normalize(item) is None
    assert "observation" not in item


@IMPLS
def test_metadata_keys_are_never_promoted(normalize):
    """A long metadata value must not become the observation."""
    item = _entry(category="x" * 200)
    assert normalize(item) is None
    assert "observation" not in item


@IMPLS
def test_non_dict_item_is_ignored(normalize):
    for junk in ("a bare string", 42, None, ["a", "list"]):
        assert normalize(junk) is None


# --- Anti-vacuity: the fixtures must actually discriminate ------------------

@IMPLS
def test_fixtures_discriminate_both_directions(normalize):
    """Per forge-skill Step 3.6 / guard-1793: assert the PER-CASE split, not a
    bare aggregate. A suite where every fixture returned the same verdict would
    have no discriminating power at all."""
    promoted = normalize(_entry(lesson=LONG))
    refused = normalize(_entry())
    assert promoted == "lesson" and refused is None, (
        "normalizer is VACUOUS -- promote and refuse produced the same verdict"
    )


def test_twins_agree_on_every_fixture():
    """Byte-level parity is owned elsewhere; this pins BEHAVIOURAL parity."""
    cases = [
        _entry(observation=LONG), _entry(lesson=LONG), _entry(takeaway=LONG),
        _entry(), _entry(lesson="short"), _entry(category="x" * 200),
        _entry(observation="  ", insight=LONG),
    ]
    for case in cases:
        a, b = copy.deepcopy(case), copy.deepcopy(case)
        assert wm._normalize_spark_capture_entry(a) == DAEMON_NORMALIZE(b), (
            f"twins disagree on {case!r} -- one of the two is inert in production"
        )
        assert a == b, f"twins mutated {case!r} differently"


# --- INTEGRATION PATH: the TRIGGER, not just the function -------------------
# Everything above injects at the normalizer itself, which is a silent scope
# declaration: the whole dispatch layer above the injection point is
# structurally unfalsifiable by those fixtures (guard-1462). In both twins the
# normalizer is reached from cmd_append behind
# `root_slot_for_validation == "spark_capture"`. If that gate drifted -- renamed
# slot constant, reordered validation block, an early return above the call site
# -- every fixture above would still pass while the normalizer never fired in
# production. These cases cover the trigger.
#
# Helpers mirror test_wm_append_unknown_slot.py rather than introducing a second
# harness for the same path.

import io  # noqa: E402
import json as _json  # noqa: E402
import os  # noqa: E402
import tempfile  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import yaml  # noqa: E402


def _init_tmp_wm(tmp: Path):
    os.environ["BODY_WM_PATH"] = str(tmp / "working-memory.yaml")
    wm.cmd_init(SimpleNamespace())


def _cli_append(slot: str, item: dict, monkeypatch):
    """Drive wm.cmd_append exactly as the CLI entry point does."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(_json.dumps(item)))
    wm.cmd_append(SimpleNamespace(slot=slot))


def _slot(name: str):
    data = yaml.safe_load(Path(os.environ["BODY_WM_PATH"]).read_text(encoding="utf-8"))
    cur = data
    for part in ("slots", name):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
    return cur


def test_append_path_normalizes_a_deviant_entry(monkeypatch):
    """Goal outcome 3, literally: an entry keyed `lesson` is READ.

    Asserts the STORED entry -- not the return value -- so this fails if the
    cmd_append gate stops reaching the normalizer.
    """
    original = os.environ.get("BODY_WM_PATH")
    with tempfile.TemporaryDirectory() as tmpd:
        try:
            _init_tmp_wm(Path(tmpd))
            _cli_append("spark_capture", _entry(lesson=LONG), monkeypatch)
            stored = _slot("spark_capture")
            assert isinstance(stored, list) and len(stored) == 1
            assert stored[0]["observation"] == LONG, (
                "the append path did not normalize -- the normalizer is correct "
                "in isolation but is not being REACHED from cmd_append"
            )
            assert stored[0]["observation_normalized_from"] == "lesson"
        finally:
            if original is None:
                os.environ.pop("BODY_WM_PATH", None)
            else:
                os.environ["BODY_WM_PATH"] = original


def test_append_path_leaves_an_empty_entry_empty(monkeypatch):
    """Direction 2 through the real path: no invented observation."""
    original = os.environ.get("BODY_WM_PATH")
    with tempfile.TemporaryDirectory() as tmpd:
        try:
            _init_tmp_wm(Path(tmpd))
            _cli_append("spark_capture", _entry(), monkeypatch)
            stored = _slot("spark_capture")
            assert len(stored) == 1
            assert "observation" not in stored[0]
            assert "observation_normalized_from" not in stored[0]
        finally:
            if original is None:
                os.environ.pop("BODY_WM_PATH", None)
            else:
                os.environ["BODY_WM_PATH"] = original


def test_normalization_is_scoped_to_the_spark_capture_slot(monkeypatch):
    """The gate is slot-scoped -- this fails if `== "spark_capture"` is dropped.

    Without this, a change that normalized EVERY array slot would pass both
    cases above. That is the mutation the two positive tests cannot see.
    """
    original = os.environ.get("BODY_WM_PATH")
    with tempfile.TemporaryDirectory() as tmpd:
        try:
            _init_tmp_wm(Path(tmpd))
            _cli_append("sensory_buffer", {"lesson": LONG}, monkeypatch)
            stored = _slot("sensory_buffer")
            assert len(stored) == 1
            assert "observation" not in stored[0], (
                "normalization leaked outside spark_capture -- the cmd_append "
                "gate is no longer slot-scoped"
            )
        finally:
            if original is None:
                os.environ.pop("BODY_WM_PATH", None)
            else:
                os.environ["BODY_WM_PATH"] = original
