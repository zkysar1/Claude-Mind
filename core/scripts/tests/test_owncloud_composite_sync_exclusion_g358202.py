"""The sync layer never mirrors a composite segment object ( outcome 3).

Under the composite layout the LOCAL file stays one legacy file and the segment objects exist only
on the remote, under `<dir>/.composite/<store>/<asp>/<n>.<md5>.jsonl`. They are pieces of ONE
logical file, so no merge handler is registered for them and none is needed: the handler is
registered for the legacy path and receives joined bytes. That holds only while nothing turns a
segment key into a local path. The exclusion in owncloud_sync._EXCLUDE_DIRS had a constant and a
source-text check (test_store_cutover_check.py) but no behavioural test; these pin it.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _owncloud_composite as c  # noqa: E402
import owncloud_sync as ocs  # noqa: E402
from test_eager_pull_exclusion import _FakeBackend, _record_pull_one  # noqa: E402

_NAME = c.segment_object_name("asp-1/0", "0" * 32)
_SEGMENT = c.segment_s3_key("aspirations.jsonl", _NAME)  # relative to the governed root, as a LIST returns it
_LOOKALIKE = "composite/aspirations.jsonl/" + _NAME      # the same shape under a directory that is NOT excluded
_BASENAME = _NAME.rsplit("/", 1)[-1]  # _record_pull_one keeps Path(full).name, the basename only


def _sweep(tmp_path, monkeypatch, names):
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    monkeypatch.setattr(ocs, "_load_manifest", lambda: {})
    seen = _record_pull_one(monkeypatch)
    stats = ocs.pull_sweep(_FakeBackend(tmp_path, names), only_root="world", dry_run=True)
    assert stats["skipped"] is None, "must reach the object loop, not early-exit"
    return seen, stats


def test_the_segment_key_is_under_the_excluded_directory():
    assert _SEGMENT.split("/")[0] == c.SEGMENT_DIR == ".composite"
    assert ocs._is_excluded_dir(c.SEGMENT_DIR)
    assert not ocs._is_excluded_dir("composite")  # the control for the lookalike below


def test_pull_sweep_never_pulls_a_segment_object(tmp_path, monkeypatch):
    seen, stats = _sweep(tmp_path, monkeypatch, ("aspirations.jsonl", _SEGMENT))
    assert seen == ["aspirations.jsonl"], "a segment object reached _pull_one"
    assert stats["skipped_machine_local"] == 1


def test_the_same_shape_under_a_plain_directory_does_pull(tmp_path, monkeypatch):
    """Control: without the excluded directory name the key reaches _pull_one, so the test above can fail."""
    seen, stats = _sweep(tmp_path, monkeypatch, ("aspirations.jsonl", _LOOKALIKE))
    assert sorted(seen) == sorted(["aspirations.jsonl", _BASENAME])
    assert stats["skipped_machine_local"] == 0
