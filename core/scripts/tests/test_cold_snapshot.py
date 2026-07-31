"""Pins for cold_snapshot.py ().

The capability exists because `.history` is in owncloud_sync._EXCLUDE_DIRS and
therefore never reaches the object store — so for ~1867 store objects the
14-day noncurrent expiry is the only recovery layer. Two properties carry that
whole argument and are pinned here:

  1. The archive/exclusion split is a MEASUREMENT, not taste. Rolled-off audit
     tails were 66% of the bytes (289MB of 437MB) against a 13.5MB
     irreplaceable core. If `_is_archive` silently stopped matching, every run
     would cost ~20x and the cadence would be quietly abandoned — a backup that
     stops running looks exactly like one that never had to.
  2. Excluding archives must never exclude the crown jewels. `aspirations.jsonl`,
     `reasoning-bank.jsonl` and every knowledge-tree node are `.jsonl`/`.md`
     files that live beside the archives; a broadened predicate (e.g. a blanket
     `*archive*` or `*.jsonl` skip) would drop them and still pass a
     size-only assertion.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cold_snapshot as cs  # noqa: E402


# --- property 1: the archive predicate matches what was measured -------------

@pytest.mark.parametrize("rel", [
    "world/changelog-archive.jsonl",
    "world/board/coordination-archive.jsonl",
    "world/reasoning-bank-archive.jsonl",
    "meta/gate-firings.jsonl",
    # sidecar copy of an archive -- same bytes twice, measured live at 56.5MB
    "world/changelog-archive.jsonl.8cA3dEA5",
    "world/aspirations.jsonl.bak-xw-migration",
])
def test_archive_markers_are_excluded_by_default(rel):
    assert cs._is_archive(rel) is True


# --- property 2: exclusion never reaches the irreplaceable core --------------

@pytest.mark.parametrize("rel", [
    "world/aspirations.jsonl",
    "world/reasoning-bank.jsonl",
    "world/guardrails.jsonl",
    "world/pipeline.jsonl",
    "world/knowledge/tree/_tree.yaml",
    "world/knowledge/tree/system/some-node.md",
    "world/conventions/capability-routing.md",
    "world/scripts/efs-ssh.sh",
    "meta/goal-selection-strategy.yaml",
    "world/forged-skills.yaml",
])
def test_precious_files_are_never_treated_as_archives(rel):
    assert cs._is_archive(rel) is False, (
        f"{rel} would be dropped from the cold snapshot -- this is the failure "
        "mode where a size-only check still passes but the crown jewels are gone"
    )


def test_archive_flag_changes_the_enumerated_set(tmp_path):
    """--include-archives must actually widen the set, not just the byte count."""
    root = tmp_path / "world"
    (root / "knowledge" / "tree").mkdir(parents=True)
    (root / "knowledge" / "tree" / "node.md").write_text("precious", encoding="utf-8")
    (root / "changelog-archive.jsonl").write_text('{"a":1}\n', encoding="utf-8")

    default = {rel for _, rel in cs._iter_precious(root)}
    widened = {rel for _, rel in cs._iter_precious(root, include_archives=True)}

    assert "knowledge/tree/node.md" in default
    assert "changelog-archive.jsonl" not in default
    assert "changelog-archive.jsonl" in widened
    assert default < widened


def test_history_dir_is_never_archived(tmp_path):
    """.history is the local snapshot store; including it squares the size.

    It is also the store whose machine-local exclusion is the whole reason this
    script exists -- shipping it would archive shadows of files already in the
    same tarball.
    """
    root = tmp_path / "world"
    (root / ".history" / "snapshots" / "aspirations.jsonl").mkdir(parents=True)
    (root / ".history" / "snapshots" / "aspirations.jsonl" / "s.yaml").write_text(
        "x", encoding="utf-8")
    (root / "aspirations.jsonl").write_text('{"id":1}\n', encoding="utf-8")

    rels = {rel for _, rel in cs._iter_precious(root, include_archives=True)}
    assert rels == {"aspirations.jsonl"}


# --- property 3: agent dirs are in the snapshot, scratch is not --------------
#
# The originating goal's verification names "world/meta/agent" paths and its one
# named example was `agents/<agent>/health/*.jsonl`, so agent state reads as a
# footnote to world/meta -- and the first cut of this script shipped covering
# world+meta only. Measured on cc-03 2026-07-31, agent dirs are the LARGEST
# synced surface: 6089 files reach the store against 1959 for world+meta, and
# only ONE agent dir had any `.history` at all. Dropping them silently would
# leave 3914 experience records and every `self.md` on the 14-day net alone,
# while every other assertion in this file still passed.

def test_agent_dirs_are_snapshot_and_temp_is_not(tmp_path, monkeypatch):
    agents = tmp_path / "agents"
    (agents / "echo" / "experience").mkdir(parents=True)
    (agents / "echo" / "experience" / "exp-1.md").write_text("trace", encoding="utf-8")
    (agents / "echo" / "self.md").write_text("identity", encoding="utf-8")
    (agents / "echo" / "health").mkdir()
    (agents / "echo" / "health" / "2026-07-31.jsonl").write_text("{}\n", encoding="utf-8")
    # scratch: has its own drain lifecycle (temp-store.md), never restored
    (agents / "echo" / "temp").mkdir()
    (agents / "echo" / "temp" / "suite.log").write_text("noise", encoding="utf-8")

    monkeypatch.setattr(cs, "WORLD_DIR", tmp_path / "nonexistent-world")
    monkeypatch.setattr(cs, "META_DIR", tmp_path / "nonexistent-meta")
    monkeypatch.setattr(cs, "AGENTS_DIR", agents)

    paths = {e["path"] for e in cs.build_manifest()[0]}
    assert paths == {
        "agents/echo/experience/exp-1.md",
        "agents/echo/self.md",
        "agents/echo/health/2026-07-31.jsonl",
    }, "agent state must be archived; agents/<agent>/temp must not"


def test_every_agent_dir_is_covered_not_just_the_bound_one(tmp_path, monkeypatch):
    """A per-box snapshot must cover every agent resident on that box.

    Snapshotting only $MIND_AGENT would look identical on a single-agent box
    and silently drop the rest of the fleet's state on a shared one.
    """
    agents = tmp_path / "agents"
    for name in ("alpha", "bravo", "echo"):
        (agents / name).mkdir(parents=True)
        (agents / name / "self.md").write_text(name, encoding="utf-8")

    monkeypatch.setattr(cs, "WORLD_DIR", tmp_path / "nonexistent-world")
    monkeypatch.setattr(cs, "META_DIR", tmp_path / "nonexistent-meta")
    monkeypatch.setattr(cs, "AGENTS_DIR", agents)

    paths = {e["path"] for e in cs.build_manifest()[0]}
    assert paths == {f"agents/{n}/self.md" for n in ("alpha", "bravo", "echo")}


def test_missing_agents_root_still_snapshots_world(tmp_path, monkeypatch):
    """An unresolvable agents root must degrade, never abort the snapshot."""
    root = tmp_path / "world"
    root.mkdir(parents=True)
    (root / "aspirations.jsonl").write_text('{"id":1}\n', encoding="utf-8")
    monkeypatch.setattr(cs, "WORLD_DIR", root)
    monkeypatch.setattr(cs, "META_DIR", tmp_path / "nonexistent-meta")
    monkeypatch.setattr(cs, "AGENTS_DIR", None)

    assert [e["path"] for e in cs.build_manifest()[0]] == ["world/aspirations.jsonl"]


def test_manifest_records_sha256_per_file(tmp_path, monkeypatch):
    """The manifest is the integrity baseline (archive-before-delete step 1)."""
    root = tmp_path / "world"
    root.mkdir(parents=True)
    (root / "aspirations.jsonl").write_text('{"id":1}\n', encoding="utf-8")
    monkeypatch.setattr(cs, "WORLD_DIR", root)
    monkeypatch.setattr(cs, "META_DIR", tmp_path / "nonexistent-meta")
    monkeypatch.setattr(cs, "AGENTS_DIR", None)

    entries, total = cs.build_manifest()
    assert len(entries) == 1
    assert entries[0]["path"] == "world/aspirations.jsonl"
    assert len(entries[0]["sha256"]) == 64
    assert total == entries[0]["bytes"] > 0


def test_tarball_round_trips_manifest_content(tmp_path, monkeypatch):
    """Extracted bytes must equal source bytes -- an archive that cannot restore
    is not an archive, and byte-count equality alone does not prove it."""
    import io
    import tarfile

    root = tmp_path / "world"
    (root / "knowledge" / "tree").mkdir(parents=True)
    payload = "# node\n\nirreplaceable content\n"
    (root / "knowledge" / "tree" / "node.md").write_text(payload, encoding="utf-8")
    monkeypatch.setattr(cs, "WORLD_DIR", root)
    monkeypatch.setattr(cs, "META_DIR", tmp_path / "nonexistent-meta")
    monkeypatch.setattr(cs, "AGENTS_DIR", None)

    _, _, blob = cs.build_snapshot()
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        got = tar.extractfile("world/knowledge/tree/node.md").read()
    assert got.decode("utf-8") == payload


def test_manifest_hash_matches_archived_bytes_under_concurrent_write(
        tmp_path, monkeypatch):
    """The receipt's sha256 must describe the bytes ACTUALLY in the archive.

    This is the pin for the defect fresh-eyes found on 2026-07-31: the original
    build_manifest + build_tarball pair walked the tree TWICE and read every file
    TWICE, so on a live fleet (where aspirations.jsonl, the board and the
    changelog are written continuously) a file could change between the two
    reads. The manifest then described the file's PREVIOUS content while the
    archive held its NEW content -- silently, and in the direction that reads as
    corruption during a restore.

    Twenty-one passing tests did not catch it, because every one of them checked
    the manifest and the archive against the SOURCE, and never against EACH
    OTHER. That is the property under test here.
    """
    import hashlib
    import io
    import tarfile

    root = tmp_path / "world"
    root.mkdir(parents=True)
    target = root / "aspirations.jsonl"
    target.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cs, "WORLD_DIR", root)
    monkeypatch.setattr(cs, "META_DIR", tmp_path / "nonexistent-meta")
    monkeypatch.setattr(cs, "AGENTS_DIR", None)

    # Mutate the file DURING the walk, right after its bytes are read -- the
    # concurrent-writer window. A single-read implementation is immune by
    # construction; a two-read one records one version and archives the other.
    real_read = cs.Path.read_bytes
    seen = []

    def read_then_mutate(self):
        data = real_read(self)
        if self.name == "aspirations.jsonl" and not seen:
            seen.append(1)
            real_write = "MUTATED BY A CONCURRENT AGENT MID-WALK"
            self.write_text(real_write, encoding="utf-8")
        return data

    monkeypatch.setattr(cs.Path, "read_bytes", read_then_mutate)
    entries, total, blob = cs.build_snapshot()

    entry = next(e for e in entries if e["path"] == "world/aspirations.jsonl")
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        archived = tar.extractfile("world/aspirations.jsonl").read()

    assert hashlib.sha256(archived).hexdigest() == entry["sha256"], (
        "receipt sha256 does not describe the archived bytes -- the manifest is "
        "not an integrity baseline (archive-before-delete.md steps 1 and 4)"
    )
    assert len(archived) == entry["bytes"]
    assert total == entry["bytes"]


def test_unreadable_file_is_reported_not_silently_dropped(tmp_path, monkeypatch):
    """A file that cannot be read must appear in the receipt as an EXCLUSION.

    Silently omitting it would make file_count look healthy while the archive
    is short -- the same "clean output over an incomplete run" shape the
    full-suite VERDICT line exists to prevent.
    """
    root = tmp_path / "world"
    root.mkdir(parents=True)
    target = root / "aspirations.jsonl"
    target.write_text('{"id":1}\n', encoding="utf-8")
    monkeypatch.setattr(cs, "WORLD_DIR", root)
    monkeypatch.setattr(cs, "META_DIR", tmp_path / "nonexistent-meta")
    monkeypatch.setattr(cs, "AGENTS_DIR", None)

    orig = Path.read_bytes

    def boom(self):
        if self.name == "aspirations.jsonl":
            raise OSError("simulated unreadable file")
        return orig(self)

    monkeypatch.setattr(Path, "read_bytes", boom)
    entries, total = cs.build_manifest()
    assert total == 0
    assert len(entries) == 1
    assert "error" in entries[0]
    assert "simulated unreadable" in entries[0]["error"]
