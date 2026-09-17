"""Readers whose wrong answer is destructive read a PEER agent's store from the
store bytes, not the local mirror (g-358-140).

The fake backend models the state measured on an own-cloud box on 2026-09-17:
the local mirror of a peer's ledger is SHORT of the store, and refresh() is a
no-op (the backend's no_clobber decision), so only read_authoritative_bytes
sees the tail. Every reader test checks the mirror first (the positive control)
so a green run cannot come from a mirror that was already current, and a reader
that goes back to a plain local read, or to refresh-then-read, fails.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))  # core/scripts
# Bind _fileops.get_backend before any test patches storage_backend.get_backend
# (test_fresh_read.py records the leak this prevents).
import _fileops  # noqa: E402,F401
import _fresh_read  # noqa: E402


def _load(filename, modname):
    spec = importlib.util.spec_from_file_location(modname, str(SCRIPT_DIR / filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FrozenMirrorStore:
    """Store bytes keyed by path. refresh() records the call and changes
    nothing, which is what no_clobber does to a stale peer mirror."""

    def __init__(self, store=None, fail=None):
        self.store = {Path(k).resolve(): v for k, v in (store or {}).items()}
        self.fail = fail
        self.refreshed = []

    def refresh(self, path):
        self.refreshed.append(Path(path))

    def read_authoritative_bytes(self, path):
        if self.fail is not None:
            raise self.fail
        key = Path(path).resolve()
        if key not in self.store:
            raise FileNotFoundError(str(path))
        return self.store[key]


@pytest.fixture
def backend(monkeypatch):
    import storage_backend

    def install(**kw):
        be = _FrozenMirrorStore(**kw)
        monkeypatch.setattr(storage_backend, "get_backend", lambda: be)
        return be
    return install


def _lines(*recs):
    return "".join(json.dumps(r) + "\n" for r in recs).encode("utf-8")


# --- the helper -------------------------------------------------------------

def test_store_that_extends_the_mirror_wins(tmp_path, backend):
    f = tmp_path / "ledger.jsonl"
    f.write_bytes(b"a\n")
    backend(store={f: b"a\nb\n"})
    assert _fresh_read.read_text_authoritative(f, label="t") == "a\nb\n"


def test_mirror_that_extends_the_store_wins(tmp_path, backend):
    # This box appended bytes the store does not have yet: never read shorter.
    f = tmp_path / "ledger.jsonl"
    f.write_bytes(b"a\nb\n")
    backend(store={f: b"a\n"})
    assert _fresh_read.read_text_authoritative(f, label="t") == "a\nb\n"


def test_diverged_copies_read_the_store(tmp_path, backend):
    f = tmp_path / "ledger.jsonl"
    f.write_bytes(b"a\nx\n")
    backend(store={f: b"a\nb\n"})
    assert _fresh_read.read_text_authoritative(f, label="t") == "a\nb\n"


def test_store_error_falls_back_to_the_mirror_loudly(tmp_path, backend, capsys):
    f = tmp_path / "ledger.jsonl"
    f.write_bytes(b"a\n")
    backend(fail=RuntimeError("unreachable"))
    assert _fresh_read.read_text_authoritative(f, label="lbl") == "a\n"
    err = capsys.readouterr().err
    assert "[lbl]" in err and "unreachable" in err


def test_store_only_object_is_read(tmp_path, backend):
    f = tmp_path / "ledger.jsonl"
    backend(store={f: b"a\n"})
    assert _fresh_read.read_text_authoritative(f, label="t") == "a\n"


def test_absent_everywhere_raises(tmp_path, backend):
    backend(store={})
    with pytest.raises(FileNotFoundError):
        _fresh_read.read_text_authoritative(tmp_path / "none.jsonl", label="t")


# --- the destructive readers --------------------------------------------------

def test_retire_candidates_counts_invocations_only_the_store_holds(tmp_path, backend, monkeypatch):
    mod = _load("skill-retire-candidates.py", "skill_retire_candidates_g358140")
    agents = tmp_path / "agents"
    ledger = agents / "alpha" / "skill-invocations.jsonl"
    ledger.parent.mkdir(parents=True)
    head = {"skill": "kept-skill", "ts": "2026-09-16T10:00:00"}
    tail = {"skill": "tail-only-skill", "ts": "2026-09-16T11:00:00"}
    ledger.write_bytes(_lines(head))
    be = backend(store={ledger: _lines(head, tail)})
    monkeypatch.setattr(mod, "agents_root", lambda: agents)

    assert "tail-only-skill" not in ledger.read_text()  # POSITIVE CONTROL: mirror is short
    cutoff = mod._parse_ts("2026-09-01T00:00:00")
    counts = mod.load_invocation_counts(cutoff)

    assert counts.get("tail-only-skill") == 1
    assert counts.get("kept-skill") == 1
    assert be.refreshed == []  # the count came from the store read, not a refresh


def test_cited_blob_includes_a_sid_only_the_store_holds(tmp_path, backend):
    hk = _load("housekeeping-tick.py", "housekeeping_tick_g358140")
    agents = tmp_path / "agents"
    exp = agents / "echo" / "experience.jsonl"
    exp.parent.mkdir(parents=True)
    head = {"id": "exp-a", "summary": "nothing cited"}
    tail = {"id": "exp-b", "summary": "session sid-tail-only-0001 cited here"}
    exp.write_bytes(_lines(head))
    backend(store={exp: _lines(head, tail)})
    world = tmp_path / "world"
    world.mkdir()

    assert "sid-tail-only-0001" not in exp.read_text()  # POSITIVE CONTROL
    blob = hk.build_cited_blob(world_dir=world, agents_root_fn=lambda: agents)

    assert blob is not None and "sid-tail-only-0001" in blob


# --- : DIVERGED copies, where neither is a prefix of the other ------
#
# experience.jsonl is REWRITTEN by the store (rotation), not only appended, so
# mirror and store can each hold lines the other lacks. read_text_authoritative
# picks the store and DROPS every mirror-only line; for a membership reader that
# renders a cited SID uncited, and Lane B deletes on the answer.

def _diverged(tmp_path, backend):
    """Mirror cites SID A only; store cites SID B only. Neither is a prefix."""
    agents = tmp_path / "agents"
    exp = agents / "echo" / "experience.jsonl"
    exp.parent.mkdir(parents=True)
    shared = {"id": "exp-shared", "summary": "no sid here"}
    mirror_only = {"id": "exp-m", "summary": "session sid-mirror-only-AAAA cited"}
    store_only = {"id": "exp-s", "summary": "session sid-store-only-BBBB cited"}
    exp.write_bytes(_lines(shared, mirror_only))
    be = backend(store={exp: _lines(shared, store_only)})
    return agents, exp, be


def test_membership_read_returns_both_copies_when_they_diverge(tmp_path, backend):
    _, exp, _ = _diverged(tmp_path, backend)

    # POSITIVE CONTROL: each copy alone is missing one of the two SIDs, so a
    # test that passed on a single copy would prove nothing.
    mirror_text = exp.read_text()
    assert "sid-mirror-only-AAAA" in mirror_text
    assert "sid-store-only-BBBB" not in mirror_text
    store_text = _fresh_read.read_text_authoritative(exp, label="t")
    assert "sid-store-only-BBBB" in store_text
    assert "sid-mirror-only-AAAA" not in store_text

    both = _fresh_read.read_text_for_membership(exp, label="t")
    assert "sid-mirror-only-AAAA" in both
    assert "sid-store-only-BBBB" in both


def test_cited_blob_keeps_a_sid_only_the_mirror_holds(tmp_path, backend):
    """The live consequence: Lane B keeps a dir when its name is in the blob."""
    agents, _, _ = _diverged(tmp_path, backend)
    hk = _load("housekeeping-tick.py", "housekeeping_tick_g358144")
    world = tmp_path / "world"
    world.mkdir()

    blob = hk.build_cited_blob(world_dir=world, agents_root_fn=lambda: agents)

    assert blob is not None
    assert "sid-store-only-BBBB" in blob   #  must not regress
    assert "sid-mirror-only-AAAA" in blob  # 


def test_mutation_control_store_wins_read_loses_the_mirror_only_sid(tmp_path, backend):
    """Reverting the housekeeping site to read_text_authoritative drops it.

    Without this, the test above could pass against a helper that never changed
    — a mirror that happens to agree with the store is indistinguishable from a
    union.
    """
    _, exp, _ = _diverged(tmp_path, backend)
    store_wins = _fresh_read.read_text_authoritative(exp, label="t")
    assert "sid-mirror-only-AAAA" not in store_wins


def test_membership_read_does_not_duplicate_when_one_extends_the_other(tmp_path, backend):
    """An append ledger must still read as ONE copy — duplicating a shared
    prefix is inert for `in` but wasteful, and it is the shape a counting
    reader would be harmed by if anyone ever wired one to this function."""
    f = tmp_path / "ledger.jsonl"
    f.write_bytes(b"a\n")
    backend(store={f: b"a\nb\n"})
    assert _fresh_read.read_text_for_membership(f, label="t") == "a\nb\n"

    g = tmp_path / "other.jsonl"
    g.write_bytes(b"a\nb\n")
    backend(store={g: b"a\n"})
    assert _fresh_read.read_text_for_membership(g, label="t") == "a\nb\n"


def test_counting_reader_is_untouched_by_the_membership_change(tmp_path, backend, monkeypatch):
    """skill-retire-candidates must keep ONE copy: a union double-counts the
    shared prefix and inflates a peer's invocation count."""
    agents = tmp_path / "agents"
    led = agents / "echo" / "skill-invocations.jsonl"
    led.parent.mkdir(parents=True)
    rec = {"skill": "kept-skill", "ts": "2026-09-17T00:00:00"}
    led.write_bytes(_lines(rec))
    backend(store={led: _lines(rec, rec)})

    src = _fresh_read.read_text_authoritative(led, label="t")
    assert src.count("kept-skill") == 2, "store extends the mirror: 2, not 3"
    union = _fresh_read.read_text_for_membership(led, label="t")
    assert union.count("kept-skill") == 2, "prefix case must not duplicate"


def test_membership_read_degrades_to_the_mirror_on_a_store_error(tmp_path, backend, capsys):
    f = tmp_path / "ledger.jsonl"
    f.write_bytes(b"local-only\n")

    class _Boom:
        def read_authoritative_bytes(self, path):
            raise RuntimeError("no credentials")

    import storage_backend
    monkeypatch_target = storage_backend
    old = monkeypatch_target.get_backend
    monkeypatch_target.get_backend = lambda: _Boom()
    try:
        assert _fresh_read.read_text_for_membership(f, label="t") == "local-only\n"
    finally:
        monkeypatch_target.get_backend = old
    assert "store read failed" in capsys.readouterr().err


# --- : the five NON-destructive skill-ledger readers -----------------
#
# They produce reports and proposals, not deletions, so  left them out.
# Same frozen mirror: each POSITIVE CONTROL asserts the local copy lacks the
# record, so a reader that goes back to a plain local read fails. The readers
# that tally must also count ONE copy (guard-6888): the store extends the
# mirror, so 2 records, never 3.

def _short_ledger(tmp_path, backend):
    """alpha's ledger mirror is SHORT of the store; returns (agents, backend)."""
    from datetime import date

    agents = tmp_path / "agents"
    led = agents / "alpha" / "skill-invocations.jsonl"
    led.parent.mkdir(parents=True)
    today = date.today().isoformat()
    head = {"skill": "kept-skill", "ts": today + "T10:00:00", "agent": "alpha"}
    tail = {"skill": "tail-only-skill", "ts": today + "T11:00:00", "agent": "alpha"}
    led.write_bytes(_lines(head))
    be = backend(store={led: _lines(head, tail)})
    assert "tail-only-skill" not in led.read_text()  # POSITIVE CONTROL: mirror is short
    return agents, be


def test_usage_report_counts_an_invocation_only_the_store_holds(tmp_path, backend, monkeypatch, capsys):
    mod = _load("skill-analytics.py", "skill_analytics_g358141")
    agents, be = _short_ledger(tmp_path, backend)
    monkeypatch.setattr(mod, "agents_root", lambda: agents)
    monkeypatch.setattr(mod, "WORLD_DIR", tmp_path / "world")  # no forged registry read

    mod.cmd_usage_report(type("Args", (), {"window": 30})())
    report = json.loads(capsys.readouterr().out)

    assert {r["skill"]: r["count"] for r in report["most_used"]} == {
        "kept-skill": 1, "tail-only-skill": 1}
    assert report["total_invocations"] == 2
    assert be.refreshed == []


def test_coinvocation_ledger_reads_a_record_only_the_store_holds(tmp_path, backend):
    mod = _load("skill-coinvocation-discovery.py", "skill_coinvocation_discovery_g358141")
    agents, be = _short_ledger(tmp_path, backend)

    records = mod.read_ledger(root=agents)

    assert sorted(r["skill"] for r in records) == ["kept-skill", "tail-only-skill"]
    assert be.refreshed == []


def test_discovery_ledger_dates_include_a_fire_only_the_store_holds(tmp_path, backend, monkeypatch):
    mod = _load("skill-discovery.py", "skill_discovery_ledger_g358141")
    agents, _ = _short_ledger(tmp_path, backend)
    monkeypatch.setattr(mod, "agents_root", lambda: agents)

    dates = mod.collect_ledger_skill_dates(["kept-skill", "tail-only-skill"])

    assert {k: len(v) for k, v in dates.items()} == {"kept-skill": 1, "tail-only-skill": 1}


def test_freshness_report_counts_a_fire_only_the_store_holds(tmp_path, backend):
    mod = _load("skill-freshness-report.py", "skill_freshness_report_g358141")
    agents, be = _short_ledger(tmp_path, backend)

    per_skill, window = mod.read_ledger_invocations(root=agents)

    assert {k: v["count"] for k, v in per_skill.items()} == {"kept-skill": 1, "tail-only-skill": 1}
    assert window[1] == per_skill["tail-only-skill"]["last"]  # the newest fire is store-only
    assert be.refreshed == []


def test_discovery_journal_dates_keep_both_diverged_copies_once(tmp_path, backend, monkeypatch):
    """journal.jsonl DIVERGES (1 of 5 on cc-02), so this reader takes the
    membership union, and drops the repeats that union makes of shared lines."""
    mod = _load("skill-discovery.py", "skill_discovery_journal_g358141")
    agents = tmp_path / "agents"
    jr = agents / "echo" / "journal.jsonl"
    jr.parent.mkdir(parents=True)
    shared = {"timestamp": "2026-09-17T08:00:00", "summary": "ran shared-skill"}
    mirror_only = {"timestamp": "2026-09-17T09:00:00", "summary": "ran mirror-skill"}
    store_only = {"timestamp": "2026-09-17T10:00:00", "summary": "ran store-skill"}
    jr.write_bytes(_lines(shared, mirror_only))
    backend(store={jr: _lines(shared, store_only)})
    monkeypatch.setattr(mod, "agents_root", lambda: agents)

    # POSITIVE CONTROLS: a mirror read misses store-skill, and the store-wins
    # read (read_text_authoritative) misses mirror-skill.
    assert "store-skill" not in jr.read_text()
    assert "mirror-skill" not in _fresh_read.read_text_authoritative(jr, label="t")

    dates = mod.collect_journal_skill_dates(["shared-skill", "mirror-skill", "store-skill"])

    assert {k: len(v) for k, v in dates.items()} == {
        "shared-skill": 1, "mirror-skill": 1, "store-skill": 1}
