""" — normalize-terminal-defer must refresh the store before reading it.

Every target of that script is an eager-pull EXCLUDED store
(`owncloud_sync._EAGER_PULL_EXCLUDE_GLOBS = ("*-archive.jsonl",)` over
`_EAGER_PULL_ROOTS = ("world", "meta")`), so on a remote-backed box the local
copy is never re-pulled once materialized and can lag the store by days. Reading
it with a plain `open()` makes BOTH of the script's outputs a statement about a
stale copy:

  * `--check` is the /verify-learning TGD regression guard, so a stale read lets
    it print PASS while records only the store holds carry anomalies;
  * `normalize_file` gates its write on `before > 0` computed locally, so an
    anomaly living only in the newer remote records is never healed.

The fix is one call — `get_backend().refresh(path)` — placed INSIDE the lock in
`normalize_file` and at the top of `scan_file`. These tests pin the call, its
ORDERING against the lock, and its best-effort failure mode.

CONTROL DISCIPLINE. `test_a_stale_read_is_what_the_refresh_prevents` is the
positive control and is the ONLY test here expected to stay green under sabotage
of the call sites: it runs the SAME fixture through a backend whose `refresh` is
a no-op — the pre-fix behaviour — and asserts the stale answer comes back. If it
ever reports 2, the fixture stopped discriminating and every other test here
proves nothing.

MEASURED, and it corrected this docstring rather than confirming it
(`mutation-proof-test.sh`, 2026-09-17, sabotage `_refresh(path)` -> `pass`,
verdict PASS, restore byte-verified): **6 of 7 go red**, including
`test_refresh_failure_is_best_effort`, which this file had declared a second
sabotage-immune control. It is not one — its stderr assertion can only fire if
the call site exists, so it pins the CALL as well as the degradation. Declaring
a test a control does not make it one (guard-2435); read `red_count` and WHICH
tests went red, never the bare verdict.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent


def _load_module():
    """Import the hyphen-named script by path (it is not a legal module name)."""
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "normalize_terminal_defer_under_test",
        _SCRIPTS / "normalize-terminal-defer.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _goal(gid, **kw):
    g = {"id": gid, "title": f"goal {gid}", "status": "completed",
         "completed_at": "2026-09-10T00:00:00"}
    g.update(kw)
    return g


def _asp(aid, goals):
    return {"id": aid, "title": f"asp {aid}", "status": "completed",
            "goals": goals}


def _write(path, rows):
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=True) + "\n" for r in rows),
        encoding="utf-8")


# The STALE local copy: one clean terminal goal, zero anomalies.
STALE = [_asp("asp-900", [_goal("g-900-01")])]
# What the STORE actually holds: the same goal plus two the local copy has never
# seen, one of which carries residual defer state and one of which is missing
# `completed_at` — i.e. two anomalies that are invisible to a stale read.
FRESH = [_asp("asp-900", [
    _goal("g-900-01"),
    _goal("g-900-02", defer_reason="residual"),
    _goal("g-900-03", completed_at=None),
])]


class _MaterialisingBackend:
    """Stands in for the remote backend: `refresh` pulls the store's real
    content into the local path, exactly as `OwnCloudBackend.refresh` does via
    `_refresh(path, force_fresh=True)`.

    IT HAS NO `atomic_write`, DELIBERATELY -- AND IT WAS NEVER THE THING KEEPING
    THESE TESTS OFF A REAL STORE. `monkeypatch.setattr(mod, "get_backend", ...)`
    rebinds the NAME in the module under test only; `_atomic_write_with_fallback`
    lives in `_fileops` and resolves `get_backend` in THAT namespace
    (`_fileops.py:1109`), so the two `normalize_file` tests below write through
    the REAL backend. That is stronger, end-to-end coverage, not a leak.

    MEASURED (2026-09-17, alpha, cc-04, `uname -r` 6.8.0-139-generic): after the
    patch, `_fileops.get_backend is mod.get_backend` is False, and both writing
    tests pass with `before == 2` -- so a fake `atomic_write` could never have
    been reached. One used to sit here raising
    `AssertionError("no write expected in these tests")` behind a
    `# pragma: no cover`: an unreachable guard asserting something false, which
    is guard-2435's shape a second time in this same file (declaring a thing a
    control does not make it one; a fake's guard is not one either until
    something proves it can fire).

    What ACTUALLY keeps these hermetic is `conftest.py` -- `STORAGE_BACKEND=local`
    plus the session-wide `MIND_ALLOW_TMP_OWNCLOUD_PUT=1`. Run outside pytest on
    an own-cloud box, `_s3_key` ignores the local path (customer_prefix + env_id
    + `_rel(path)`) and `OwnCloudBackend._assert_not_tempdir_put` is the real
    backstop (guard-955 / rb-2983).
    """

    def __init__(self, fresh_rows):
        self.fresh_rows = fresh_rows
        self.refreshed = []
        self.lock_held_at_refresh = []

    def refresh(self, path):
        path = Path(path)
        self.refreshed.append(path)
        self.lock_held_at_refresh.append(path.with_suffix(".lock").exists())
        _write(path, self.fresh_rows)


class _InertBackend:
    """The PRE-FIX world: refresh reads nothing and changes nothing."""

    def refresh(self, path):
        return None


def test_scan_file_refreshes_before_counting(tmp_path, monkeypatch):
    """--check must count the STORE's anomalies, not the stale copy's."""
    mod = _load_module()
    backend = _MaterialisingBackend(FRESH)
    monkeypatch.setattr(mod, "get_backend", lambda: backend)

    target = tmp_path / "aspirations-archive.jsonl"
    _write(target, STALE)

    result = mod.scan_file(target)

    assert backend.refreshed == [target], "scan_file did not refresh the store"
    assert result["exists"] is True
    assert result["before"] == 2, (
        "scan_file counted the stale copy (0 anomalies) instead of the store's 2")


def test_a_stale_read_is_what_the_refresh_prevents(tmp_path, monkeypatch):
    """POSITIVE CONTROL for the test above.

    Same fixture, a backend whose refresh does nothing — the behaviour before
    g-358-124. The scan reports the stale ZERO, which is the false all-clear the
    fix exists to remove. If this ever reports 2, the fixture has stopped
    discriminating and the test above proves nothing.
    """
    mod = _load_module()
    monkeypatch.setattr(mod, "get_backend", lambda: _InertBackend())

    target = tmp_path / "aspirations-archive.jsonl"
    _write(target, STALE)

    assert mod.scan_file(target)["before"] == 0


def test_normalize_file_refreshes_inside_the_lock(tmp_path, monkeypatch):
    """The refresh must land INSIDE the lock, and the heal must cover the
    records only the store held.

    Ordering is load-bearing: a refresh taken before the lock can be overtaken
    by a peer write between the pull and the open, which is the same stale base
    with extra steps. The fake backend records whether the lock file existed at
    the moment refresh was called.
    """
    mod = _load_module()
    backend = _MaterialisingBackend(FRESH)
    monkeypatch.setattr(mod, "get_backend", lambda: backend)

    target = tmp_path / "aspirations-archive.jsonl"
    _write(target, STALE)

    result = mod.normalize_file(target)

    assert backend.refreshed == [target], "normalize_file did not refresh"
    assert backend.lock_held_at_refresh == [True], (
        "refresh ran OUTSIDE the lock — a peer write can land between the pull "
        "and the read")
    assert result["before"] == 2, (
        "the write was gated on the stale copy's anomaly count, so the two "
        "store-only anomalies would never be healed")
    assert result["after"] == 0
    assert result["wrote"] is True

    healed = {g["id"]: g
              for line in target.read_text(encoding="utf-8").splitlines() if line
              for g in json.loads(line)["goals"]}
    assert set(healed) == {"g-900-01", "g-900-02", "g-900-03"}, (
        "the store-only goals were dropped instead of healed")
    assert healed["g-900-02"].get("defer_reason") is None
    assert healed["g-900-03"].get("completed_at") is not None


def test_normalize_file_materialises_a_store_only_file(tmp_path, monkeypatch):
    """A cold box may hold no local copy at all — the eager pull excludes these
    archives, so `exists()` is False until something asks for them. The
    existence check therefore has to FOLLOW the refresh; checking first skips
    the entire file in silence.
    """
    mod = _load_module()
    backend = _MaterialisingBackend(FRESH)
    monkeypatch.setattr(mod, "get_backend", lambda: backend)

    target = tmp_path / "aspirations-archive.jsonl"   # deliberately absent
    assert not target.exists()

    result = mod.normalize_file(target)

    assert result["exists"] is True, (
        "a store-only archive was reported absent — the existence check ran "
        "before the refresh")
    assert result["before"] == 2


def test_refresh_failure_is_best_effort(tmp_path, monkeypatch, capsys):
    """A transport fault must not wedge a backfill that could previously run
    against the local copy: the scan degrades to the local answer and says so on
    stderr.

    NOT a control, despite reading like one — measured red under the call-site
    sabotage, because the stderr assertion can only fire if the call exists. It
    therefore pins two things at once: that `_refresh` is called, and that a
    raising refresh is survivable.
    """
    mod = _load_module()

    class _Exploding:
        def refresh(self, path):
            raise RuntimeError("transport down")

    monkeypatch.setattr(mod, "get_backend", lambda: _Exploding())

    target = tmp_path / "aspirations-archive.jsonl"
    _write(target, STALE)

    result = mod.scan_file(target)

    assert result["before"] == 0, "a failed refresh must not break the scan"
    assert "could not refresh" in capsys.readouterr().err


@pytest.mark.parametrize("fn_name", ["scan_file", "normalize_file"])
def test_both_read_paths_are_covered(fn_name, tmp_path, monkeypatch):
    """Neither read path may be left on a raw local read — the script has two,
    and fixing only the writing one leaves the /verify-learning guard blind.
    """
    mod = _load_module()
    backend = _MaterialisingBackend(FRESH)
    monkeypatch.setattr(mod, "get_backend", lambda: backend)

    target = tmp_path / "aspirations-archive.jsonl"
    _write(target, STALE)

    getattr(mod, fn_name)(target)

    assert backend.refreshed == [target], f"{fn_name} read without refreshing"
