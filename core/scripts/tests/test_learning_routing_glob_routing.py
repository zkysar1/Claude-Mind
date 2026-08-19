"""Pins : the learning-routing pair must enumerate agents via agents_root().

Both `learning-routing-audit.py::load_all_experiences` and
`learning-routing-repair.py::_resolve_store_path` globbed `PROJECT_ROOT.glob("*/…")`,
which matches NOTHING once agent dirs moved under an `agents/` parent. Neither
raised, neither logged, and both failure modes are silent in the direction that
looks like a working tool:

  - the AUDIT loaded 0 experience records, so every cross-ref pointing INTO the
    experience store dangled by construction. It reported 319 dangling refs of
    which 305 (95.6%) were false positives. `exp:0` in the run header was the
    only tell, and it reads as "no data" rather than "wrong path".
  - the REPAIR resolved every experience record to None, so `--apply` silently
    skipped them. Run against the broken audit it would have NULLED 305 valid
    cross-references — the two defects compose into data loss.

This class is invisible to all three CLAUDE.md audit greps (it neither defines a
constant, nor writes a literal `agents/*`, nor uses `.parent`), so the only other
audit surface is the "cross-agent glob consumers" table. These tests are the
executable half.

THE LOAD-BEARING ASSERTION IS NONZERO-ness. A zero corpus is the silent-failure
signature; it is what let this ship, and it is what a redrift reproduces.
"""
import ast
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from _paths import AGENTS_PARENT_DIR, PROJECT_ROOT, agents_root  # noqa: E402

AUDIT_PY = SCRIPTS / "learning-routing-audit.py"
REPAIR_PY = SCRIPTS / "learning-routing-repair.py"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


audit = _load(AUDIT_PY, "_lr_audit")
repair = _load(REPAIR_PY, "_lr_repair")


def _seed(root, agent, filename, records):
    d = root / agent
    d.mkdir(parents=True, exist_ok=True)
    (d / filename).write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
    )


# ------------------------------------------------------ the live-tree pin

def test_loader_returns_a_nonzero_corpus_on_the_live_tree():
    """The goal's explicit ask, and the pin that would have caught the ship.

    The precondition is measured INDEPENDENTLY of the loader — directly off
    agents_root() — so a redrift cannot satisfy its own precondition and skip.
    An empty checkout genuinely has no experience files and skips loudly; a
    populated tree that yields 0 records is the defect.
    """
    on_disk = sorted(agents_root().glob("*/experience.jsonl")) + \
        sorted(agents_root().glob("*/experience-archive.jsonl"))
    if not on_disk:
        pytest.skip(
            f"no experience files under {agents_root()} — genuinely empty tree, "
            "not the depth-1 defect"
        )
    records = audit.load_all_experiences()
    assert len(records) > 0, (
        f"load_all_experiences() returned 0 records while {len(on_disk)} experience "
        f"file(s) exist under {agents_root()}. This is the g-115-5646 signature: the "
        f"glob is enumerating the wrong depth. Every experience-axis cross-ref will "
        f"now be reported dangling, and learning-routing-repair.py --apply will null it."
    )


def test_depth_one_glob_matches_nothing_on_this_layout():
    """Pins WHY the regression is silent rather than loud.

    The pre-fix expression is not merely wrong — it returns an empty iterator, so
    the loader completes successfully with an empty corpus. If this ever starts
    matching, the layout changed and the fix above needs re-deriving rather than
    trusting.
    """
    if not AGENTS_PARENT_DIR:
        pytest.skip("legacy layout: PROJECT_ROOT *is* agents_root, no depth to confuse")
    assert list(PROJECT_ROOT.glob("*/experience.jsonl")) == [], (
        "the depth-1 glob now matches — agent dirs may have moved back to "
        "PROJECT_ROOT. Re-derive the routing in both learning-routing files."
    )


# ------------------------------------------------- hermetic routing proofs

def test_audit_reads_at_depth_one_under_agents_root(tmp_path, monkeypatch):
    """Two-root proof: the corpus follows agents_root(), at exactly one level under it."""
    root = tmp_path / "agents"
    _seed(root, "fake-a", "experience.jsonl", [{"id": "exp-a"}])
    # decoy at the WRONG depth — directly under the root, no agent dir
    (root / "experience.jsonl").write_text(
        json.dumps({"id": "exp-decoy"}) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(audit, "agents_root", lambda: root)

    ids = {r["id"] for r in audit.load_all_experiences()}
    assert ids == {"exp-a"}, f"expected only the depth-1 record, got {ids}"


def test_audit_unions_the_archive(tmp_path, monkeypatch):
    """A cross-ref does not stop being valid when its target is rotated to archive.

    Reading only the live file reproduces the same false-positive class one
    rotation later, which is why the union is part of the fix and not a nicety.
    """
    root = tmp_path / "agents"
    _seed(root, "fake-a", "experience.jsonl", [{"id": "exp-live"}])
    _seed(root, "fake-a", "experience-archive.jsonl", [{"id": "exp-rotated"}])
    monkeypatch.setattr(audit, "agents_root", lambda: root)

    ids = {r["id"] for r in audit.load_all_experiences()}
    assert ids == {"exp-live", "exp-rotated"}


def test_audit_spans_multiple_agents(tmp_path, monkeypatch):
    """Cross-agent coverage is the function's stated purpose — pin it, not just depth."""
    root = tmp_path / "agents"
    _seed(root, "fake-a", "experience.jsonl", [{"id": "exp-a"}])
    _seed(root, "fake-b", "experience.jsonl", [{"id": "exp-b"}])
    monkeypatch.setattr(audit, "agents_root", lambda: root)

    assert {r["id"] for r in audit.load_all_experiences()} == {"exp-a", "exp-b"}


# ----------------------------------------------- the destructive write side

def test_repair_resolves_an_experience_record_to_its_file(tmp_path, monkeypatch):
    """The write-side twin, and the one with teeth.

    Pre-fix this returned None for EVERY experience record. None means "not
    found", which the repair reports as a warning and skips — so the defect
    presented as a tidy partial run, never as an error.
    """
    root = tmp_path / "agents"
    _seed(root, "fake-a", "experience.jsonl", [{"id": "exp-target"}])
    monkeypatch.setattr(repair, "agents_root", lambda: root)

    resolved = repair._resolve_store_path("experience", "exp-target")
    assert resolved == root / "fake-a" / "experience.jsonl"


def test_repair_resolves_archived_records_too(tmp_path, monkeypatch):
    """An unresolvable archived record is indistinguishable from 'not mine'.

    Missing the archive here does not merely skip a repair — it lets a record
    that EXISTS be treated as absent by anything reading this resolution.
    """
    root = tmp_path / "agents"
    _seed(root, "fake-a", "experience.jsonl", [{"id": "exp-live"}])
    _seed(root, "fake-a", "experience-archive.jsonl", [{"id": "exp-rotated"}])
    monkeypatch.setattr(repair, "agents_root", lambda: root)

    assert repair._resolve_store_path("experience", "exp-rotated") == \
        root / "fake-a" / "experience-archive.jsonl"


def test_repair_prefers_the_live_file_when_an_id_is_in_both(tmp_path, monkeypatch):
    """A rotation can leave the same id in both files; the live copy is the newer write."""
    root = tmp_path / "agents"
    _seed(root, "fake-a", "experience.jsonl", [{"id": "exp-dup"}])
    _seed(root, "fake-a", "experience-archive.jsonl", [{"id": "exp-dup"}])
    monkeypatch.setattr(repair, "agents_root", lambda: root)

    assert repair._resolve_store_path("experience", "exp-dup") == \
        root / "fake-a" / "experience.jsonl"


def test_repair_returns_none_for_a_genuinely_absent_record(tmp_path, monkeypatch):
    """The fix must not turn resolution into something that always answers.

    Without this, every test above is also satisfied by a function that returns
    the first file it sees.
    """
    root = tmp_path / "agents"
    _seed(root, "fake-a", "experience.jsonl", [{"id": "exp-a"}])
    monkeypatch.setattr(repair, "agents_root", lambda: root)

    assert repair._resolve_store_path("experience", "exp-nowhere") is None


# ------------------------------------------------------- source-level pin

@pytest.mark.parametrize("path", [AUDIT_PY, REPAIR_PY])
def test_neither_file_reintroduces_the_depth_one_glob(path):
    """Cheap review-time catch, detected STRUCTURALLY rather than by text.

    A text scan is wrong here and was wrong on first run: both files now carry
    rationale that QUOTES the defective expression on purpose, so a substring
    match flags the documentation as the defect. Skipping `#`-prefixed lines does
    not save it either — the audit's rationale lives in a docstring. That is
    guard-1099's shape exactly (a scan counting its own explanatory prose as live
    code), and the fail direction is the bad one: it goes red on a healthy file,
    which trains a reader to delete the check.

    Matching the AST call shape instead is immune to prose by construction, and
    strictly stronger — it also catches `PROJECT_ROOT.glob(pattern_var)`, which no
    literal-string scan can see.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "glob"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "PROJECT_ROOT"
    ]
    assert offenders == [], (
        f"{path.name} reintroduced PROJECT_ROOT.glob(...) at line(s) {offenders}. "
        f"Route through agents_root() (g-115-5646)."
    )
