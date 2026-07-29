"""test_skill_relations_amended_at_writer.py -- g-115-3639.

The WRITER half of the _merge_skill_relation amendment-loss fix. The reader
half (tier-0 amended_at in coordination_merge._merge_skill_relation) is pinned
by the _sr_ amendment family in test_coordination_merge.py; those tests
CONSTRUCT the field, so they pass whether or not anything on the write path
ever sets it. rb-5493 names that exact trap: a merge-tier fix with no writer
that stamps the field is a reader with no writer -- the tier never fires in
production and the tests pass vacuously. This file is the other half.

Two contracts are pinned:
  - cmd_add STAMPS amended_at on every new edge, in the naive ISO form the
    merge compares lexicographically.
  - the duplicate refusal STATES the amended_at contract. cmd_add refuses
    duplicates, which is precisely what makes amending an existing edge a
    HAND-EDIT -- so the refusal is the moment of use, and the only place an
    amender reliably reads.

Lives in its own file rather than in test_coordination_merge.py because that
module declares "pure functions, no moto / no daemon / no I/O" and these tests
write a tmp YAML file.

The hyphen-named module is loaded via importlib (pattern from
test_skill_coinvocation_discovery.py). Importing it resolves _paths, so
AYOAI_WORLD/AYOAI_AGENT are stashed to a tmp dir FIRST and restored right after
the load (guard-588: a module-level os.environ mutation must not leak into
other tests in the same pytest session).

Cross-references:
  - g-115-3639 -- the build goal (both halves)
  - g-115-3638 -- the sibling fix on _merge_forged_skill
  - guard-1153 -- LWW on a timestamp written BY THE SAME MUTATION
  - guard-1072 -- union-merged stores have no deletion semantics
  - rb-5493 -- the two-halves rule
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

# guard-588: stash env BEFORE the import bootstraps _paths, restore right after.
_ORIG_WORLD = os.environ.get("AYOAI_WORLD")
_ORIG_AGENT = os.environ.get("AYOAI_AGENT")
_TMPDIR = tempfile.mkdtemp(prefix="skill-relations-writer-test-")
os.environ["AYOAI_WORLD"] = _TMPDIR
os.environ.pop("AYOAI_AGENT", None)

_PATH = CORE_SCRIPTS / "skill-relations.py"
_spec = importlib.util.spec_from_file_location("skill_relations", _PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

if _ORIG_WORLD is not None:
    os.environ["AYOAI_WORLD"] = _ORIG_WORLD
elif "AYOAI_WORLD" in os.environ:
    del os.environ["AYOAI_WORLD"]
if _ORIG_AGENT is not None:
    os.environ["AYOAI_AGENT"] = _ORIG_AGENT

_ISO_NAIVE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")


def _add(monkeypatch, tmp_path, entry, existing=None):
    """Run cmd_add against an isolated world file. Returns the written doc."""
    relpath = tmp_path / "skill-relations.yaml"
    if existing is not None:
        relpath.write_text(yaml.dump(existing, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(_mod, "WORLD_RELATIONS_PATH", relpath)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(entry)))
    _mod.cmd_add(None)
    return yaml.safe_load(relpath.read_text(encoding="utf-8"))


def test_cmd_add_stamps_amended_at(monkeypatch, tmp_path):
    """Every edge this path produces carries the tier-0 stamp, so a later
    hand-edit amendment has a baseline to beat."""
    doc = _add(monkeypatch, tmp_path,
               {"source": "skill-a", "target": "skill-b", "type": "compose_with"})
    rel = doc["forged_relations"][0]
    assert "amended_at" in rel, "cmd_add did not stamp amended_at (rb-5493)"
    assert _ISO_NAIVE.match(rel["amended_at"]), rel["amended_at"]


def test_stamp_is_the_form_the_merge_compares(monkeypatch, tmp_path):
    """The stamp must be a naive ISO STRING. _merge_skill_relation compares it
    lexicographically (via str()), and only this form makes lexicographic order
    agree with chronological order -- a datetime would render with a space
    where ISO uses 'T' and sort wrong against quoted peers."""
    doc = _add(monkeypatch, tmp_path,
               {"source": "skill-a", "target": "skill-b", "type": "compose_with"})
    stamp = doc["forged_relations"][0]["amended_at"]
    assert isinstance(stamp, str), type(stamp)
    parsed = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S")
    assert abs((datetime.now() - parsed).total_seconds()) < 300
    # Lexicographic vs chronological agreement, which is what tier 0 relies on.
    assert stamp > "2026-07-20T00:00:00"


def test_stamp_preserves_optional_fields(monkeypatch, tmp_path):
    """The stamp is additive -- confidence/evidence still land unchanged."""
    doc = _add(monkeypatch, tmp_path,
               {"source": "skill-a", "target": "skill-b", "type": "compose_with",
                "confidence": 0.9, "evidence": "probe log"})
    rel = doc["forged_relations"][0]
    assert rel["confidence"] == 0.9 and rel["evidence"] == "probe log"
    assert "amended_at" in rel


def test_duplicate_refusal_states_the_amended_at_contract(monkeypatch, tmp_path,
                                                          capsys):
    """cmd_add REFUSES duplicates, which is what makes amending an existing
    edge a hand-edit. That refusal is therefore the moment of use for the
    contract -- it must tell the amender to bump amended_at, not just say no."""
    existing = {"forged_relations": [
        {"source": "skill-a", "target": "skill-b", "type": "compose_with",
         "amended_at": "2026-07-20T00:00:00"}]}
    relpath = tmp_path / "skill-relations.yaml"
    relpath.write_text(yaml.dump(existing, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(_mod, "WORLD_RELATIONS_PATH", relpath)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"source": "skill-a", "target": "skill-b", "type": "compose_with"})))

    with pytest.raises(SystemExit) as exc:
        _mod.cmd_add(None)
    assert exc.value.code == 1

    err = capsys.readouterr().err
    assert "duplicate relation already exists" in err
    assert "amended_at" in err, "refusal does not state the contract (g-115-3639)"
    # The refusal must not have mutated the store.
    assert yaml.safe_load(relpath.read_text(encoding="utf-8")) == existing
