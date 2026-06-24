"""test_skill_freshness_report.py -- .

Pins the SKILL.md-freshness cross-reference logic of skill-freshness-report.py:
  - cohort classification (stale_modified / fresh_stable / never_invoked) against
    a fixed `now` and os.utime-controlled mtimes
  - gap_days math + stale-cohort sort order
  - the false-positive control: never-invoked skills stay OUT of both cohorts
  - user_invocable front-matter parsing (both spellings + absent)
  - ledger last-invocation / count / window aggregation
  - data_window_sufficient flag
  - the guard-594 checkout-reset self-diagnostic (tight mtime span + >10 skills)
  - --top cap and the main() CLI contract (both cohorts in JSON)

The hyphen-named module is loaded via importlib (the pattern proven in
test_skill_coinvocation_discovery.py / test_rb_entry_type_taxonomy_sync.py).
Importing it resolves _paths (PROJECT_ROOT / WORLD bootstrap), so
MIND_WORLD/MIND_AGENT are stashed to a tmp dir FIRST and restored immediately
after the load (guard-588: a module-level os.environ mutation must not leak into
other tests in the same pytest session).

build_report/read_skill_mtimes/read_ledger_invocations all take explicit
skills_dir/root/now params, so the unit tests are fully hermetic with NO
monkeypatch; only the main() smoke test monkeypatches the SKILLS_DIR + agents_root
module globals (the CLI defaults). mtimes are set deterministically with os.utime;
guard-759: per-test dirs use the tmp_path fixture, never /tmp.

Cross-references:
  - g-304-14 -- Master plan Layer 5d (the build goal)
  - skill-coinvocation-discovery.py / its test -- ledger-read + importlib pattern
  - guard-588 -- module-level env stash discipline
  - guard-594 -- the checkout-reset self-diagnostic this suite pins
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

# guard-588: stash env BEFORE the import bootstraps _paths, restore right after.
_ORIG_WORLD = os.environ.get("MIND_WORLD")
_ORIG_AGENT = os.environ.get("MIND_AGENT")
_TMPDIR = tempfile.mkdtemp(prefix="freshness-test-")
os.environ["MIND_WORLD"] = _TMPDIR
os.environ.pop("MIND_AGENT", None)

_PATH = CORE_SCRIPTS / "skill-freshness-report.py"
_spec = importlib.util.spec_from_file_location("skill_freshness_report", _PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

if _ORIG_WORLD is not None:
    os.environ["MIND_WORLD"] = _ORIG_WORLD
elif "MIND_WORLD" in os.environ:
    del os.environ["MIND_WORLD"]
if _ORIG_AGENT is not None:
    os.environ["MIND_AGENT"] = _ORIG_AGENT

NOW = datetime(2026, 6, 19, 12, 0, 0)


def _make_skill(skills_dir, name, mtime_dt, user_invocable=None, raw=None):
    """Create skills_dir/<name>/SKILL.md and force its mtime via os.utime."""
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    md = d / "SKILL.md"
    if raw is not None:
        body = raw
    elif user_invocable is None:
        body = "---\nname: {}\n---\nbody\n".format(name)
    else:
        body = "---\nname: {}\nuser-invocable: {}\n---\nbody\n".format(
            name, "true" if user_invocable else "false")
    md.write_text(body, encoding="utf-8")
    ts = mtime_dt.timestamp()
    os.utime(md, (ts, ts))
    return md


def _rec(ts, skill, sid="s1", agent="alpha"):
    return {"ts": ts, "skill": skill, "agent": agent, "sid": sid,
            "invocation_source": "model"}


def _write_ledger(agents_dir, agent, records):
    d = agents_dir / agent
    d.mkdir(parents=True, exist_ok=True)
    (d / "skill-invocations.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _iso(dt):
    return dt.strftime(_mod.TS_FMT)


# --- _parse_ts / _days ------------------------------------------------------

def test_parse_ts_roundtrip():
    assert _mod._parse_ts("2026-06-19T03:00:00") == datetime(2026, 6, 19, 3, 0, 0)
    assert _mod._parse_ts("not-a-ts") is None
    assert _mod._parse_ts(None) is None


def test_days_signed():
    a = datetime(2026, 6, 19, 0, 0, 0)
    b = datetime(2026, 6, 17, 0, 0, 0)
    assert _mod._days(a, b) == 2.0
    assert _mod._days(b, a) == -2.0


# --- read_skill_mtimes ------------------------------------------------------

def test_read_skill_mtimes_parses_user_invocable(tmp_path):
    sd = tmp_path / "skills"
    _make_skill(sd, "hyphen-true", NOW, user_invocable=True)
    _make_skill(sd, "no-field", NOW)  # name only, no user-invocable line
    # underscore spelling, explicit false
    _make_skill(sd, "underscore-false", NOW,
                raw="---\nname: underscore-false\nuser_invocable: false\n---\nbody\n")
    out = _mod.read_skill_mtimes(sd)
    assert out["hyphen-true"]["user_invocable"] is True
    assert out["underscore-false"]["user_invocable"] is False
    assert out["no-field"]["user_invocable"] is None
    # mtime is a datetime round-tripped from os.utime
    assert isinstance(out["no-field"]["mtime"], datetime)


def test_read_skill_mtimes_empty_dir(tmp_path):
    assert _mod.read_skill_mtimes(tmp_path / "does-not-exist") == {}


# --- read_ledger_invocations ------------------------------------------------

def test_read_ledger_last_count_window(tmp_path):
    agents = tmp_path / "agents"
    _write_ledger(agents, "alpha", [
        _rec("2026-06-10T00:00:00", "x"),
        _rec("2026-06-15T00:00:00", "x"),   # later -> becomes 'last'
    ])
    _write_ledger(agents, "bravo", [
        _rec("2026-06-18T00:00:00", "x"),   # cross-agent: latest overall for x
        _rec("2026-06-12T00:00:00", "y"),
    ])
    per_skill, (lo, hi) = _mod.read_ledger_invocations(root=agents)
    assert per_skill["x"]["count"] == 3
    assert per_skill["x"]["last"] == datetime(2026, 6, 18, 0, 0, 0)
    assert per_skill["y"]["count"] == 1
    assert lo == datetime(2026, 6, 10, 0, 0, 0)
    assert hi == datetime(2026, 6, 18, 0, 0, 0)


def test_read_ledger_skips_malformed(tmp_path):
    agents = tmp_path / "agents"
    d = agents / "alpha"
    d.mkdir(parents=True)
    (d / "skill-invocations.jsonl").write_text(
        json.dumps(_rec("2026-06-10T00:00:00", "x")) + "\n"
        + "not json\n"
        + json.dumps({"ts": "bad-ts", "skill": "x"}) + "\n"   # unparseable ts -> skipped
        + json.dumps({"skill": "y"}) + "\n",                   # no ts -> skipped
        encoding="utf-8")
    per_skill, _ = _mod.read_ledger_invocations(root=agents)
    assert per_skill["x"]["count"] == 1
    assert "y" not in per_skill


# --- build_report cohort classification -------------------------------------

def test_build_report_classifies_all_cohorts(tmp_path):
    sd = tmp_path / "skills"
    agents = tmp_path / "agents"
    _make_skill(sd, "stale", NOW - timedelta(days=1))     # mtime 06-18
    _make_skill(sd, "fresh", NOW - timedelta(days=18))    # mtime 06-01 (>=7d stable)
    _make_skill(sd, "healthy", NOW - timedelta(days=2))   # mtime 06-17 (<7d stable)
    _make_skill(sd, "dormant", NOW - timedelta(days=18))  # no invocations
    _write_ledger(agents, "alpha", [
        _rec(_iso(NOW - timedelta(days=4)), "stale"),     # inv 06-15 < mtime -> stale_modified
        _rec(_iso(NOW - timedelta(days=2)), "fresh"),     # inv 06-17 > mtime -> fresh_stable
        _rec(_iso(NOW - timedelta(days=1)), "healthy"),   # inv 06-18 > mtime, 2d stable -> unreported
        _rec(_iso(NOW - timedelta(days=8)), "fresh"),     # widen window to ~8d
    ])
    rep = _mod.build_report(skills_dir=sd, root=agents, stable_days=7.0,
                            min_window_days=7.0, now=NOW)
    stale = {e["skill"] for e in rep["stale_modified"]}
    fresh = {e["skill"] for e in rep["fresh_stable"]}
    never = {e["skill"] for e in rep["never_invoked_in_window"]}
    assert stale == {"stale"}
    assert fresh == {"fresh"}
    assert never == {"dormant"}
    # healthy (invoked-after-mod but <stable_days) is in NONE of the three buckets
    assert "healthy" not in stale and "healthy" not in fresh and "healthy" not in never
    assert rep["skills_scanned"] == 4
    assert rep["data_window_sufficient"] is True


def test_build_report_gap_days_and_sort(tmp_path):
    sd = tmp_path / "skills"
    agents = tmp_path / "agents"
    _make_skill(sd, "big-gap", NOW)                       # mtime now
    _make_skill(sd, "small-gap", NOW - timedelta(days=3))
    _write_ledger(agents, "alpha", [
        _rec(_iso(NOW - timedelta(days=5)), "big-gap"),    # gap +5d
        _rec(_iso(NOW - timedelta(days=4)), "small-gap"),  # gap +1d
    ])
    rep = _mod.build_report(skills_dir=sd, root=agents, now=NOW)
    # sorted by gap_days descending -> big-gap first
    assert [e["skill"] for e in rep["stale_modified"]] == ["big-gap", "small-gap"]
    assert rep["stale_modified"][0]["gap_days"] == 5.0
    assert rep["stale_modified"][1]["gap_days"] == 1.0


def test_never_invoked_not_in_cohorts_fp_control(tmp_path):
    # The whole false-positive-control point: a never-invoked user/control skill
    # must NOT appear in either alert cohort, only in the supplementary bucket.
    sd = tmp_path / "skills"
    agents = tmp_path / "agents"
    _make_skill(sd, "control-skill", NOW - timedelta(days=2), user_invocable=True)
    _write_ledger(agents, "alpha", [_rec(_iso(NOW - timedelta(days=1)), "some-other-skill")])
    rep = _mod.build_report(skills_dir=sd, root=agents, now=NOW)
    assert rep["stale_modified"] == []
    assert rep["fresh_stable"] == []
    assert [e["skill"] for e in rep["never_invoked_in_window"]] == ["control-skill"]
    assert rep["never_invoked_in_window"][0]["user_invocable"] is True


def test_fresh_stable_requires_stable_days(tmp_path):
    # invoked-after-mod, but mtime only 3d old -> below default stable_days(7) -> unreported.
    sd = tmp_path / "skills"
    agents = tmp_path / "agents"
    _make_skill(sd, "recent", NOW - timedelta(days=3))
    _write_ledger(agents, "alpha", [_rec(_iso(NOW - timedelta(days=1)), "recent")])
    rep = _mod.build_report(skills_dir=sd, root=agents, stable_days=7.0, now=NOW)
    assert rep["fresh_stable"] == []
    # lowering stable_days to 2 promotes it
    rep2 = _mod.build_report(skills_dir=sd, root=agents, stable_days=2.0, now=NOW)
    assert [e["skill"] for e in rep2["fresh_stable"]] == ["recent"]


# --- data window + checkout-reset guard -------------------------------------

def test_data_window_insufficient(tmp_path):
    sd = tmp_path / "skills"
    agents = tmp_path / "agents"
    _make_skill(sd, "s", NOW - timedelta(days=1))
    _write_ledger(agents, "alpha", [
        _rec(_iso(NOW - timedelta(days=2)), "s"),
        _rec(_iso(NOW - timedelta(days=1)), "s"),   # window ~1d < 7d
    ])
    rep = _mod.build_report(skills_dir=sd, root=agents, min_window_days=7.0, now=NOW)
    assert rep["data_window_sufficient"] is False
    assert rep["ledger_window_days"] < 7.0


def test_checkout_reset_suspected_guard(tmp_path):
    # >10 skills whose mtimes are clustered within minutes -> checkout-reset signature.
    sd = tmp_path / "skills"
    agents = tmp_path / "agents"
    base = NOW - timedelta(days=3)
    for i in range(12):
        _make_skill(sd, "sk{}".format(i), base + timedelta(minutes=i))
    rep = _mod.build_report(skills_dir=sd, root=agents, now=NOW)
    assert rep["checkout_reset_suspected"] is True
    assert rep["mtime_signal"] == "git"
    assert rep["mtime_span_days"] < _mod.MTIME_SPAN_FLOOR_DAYS


def test_no_checkout_reset_when_spread(tmp_path):
    sd = tmp_path / "skills"
    agents = tmp_path / "agents"
    for i in range(12):
        _make_skill(sd, "sk{}".format(i), NOW - timedelta(days=i * 2))  # spread 0..22d
    rep = _mod.build_report(skills_dir=sd, root=agents, now=NOW)
    assert rep["checkout_reset_suspected"] is False
    assert rep["mtime_signal"] == "st_mtime"


# --- top cap ----------------------------------------------------------------

def test_top_cap(tmp_path):
    sd = tmp_path / "skills"
    agents = tmp_path / "agents"
    recs = []
    for i in range(5):
        _make_skill(sd, "stale{}".format(i), NOW)
        recs.append(_rec(_iso(NOW - timedelta(days=i + 1)), "stale{}".format(i)))
    _write_ledger(agents, "alpha", recs)
    rep = _mod.build_report(skills_dir=sd, root=agents, top=2, now=NOW)
    assert rep["stale_modified_count"] == 2


# --- main() CLI contract ----------------------------------------------------

def test_main_smoke_json(tmp_path, monkeypatch, capsys):
    sd = tmp_path / "skills"
    agents = tmp_path / "agents"
    _make_skill(sd, "stale", NOW - timedelta(days=1))
    _make_skill(sd, "fresh", NOW - timedelta(days=18))
    _write_ledger(agents, "alpha", [
        _rec(_iso(NOW - timedelta(days=4)), "stale"),
        _rec(_iso(NOW - timedelta(days=2)), "fresh"),
    ])
    monkeypatch.setattr(_mod, "SKILLS_DIR", sd)
    monkeypatch.setattr(_mod, "agents_root", lambda: agents)
    rc = _mod.main(["--output", "json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert "stale_modified" in out and "fresh_stable" in out
    assert out["skills_scanned"] == 2


def test_main_smoke_human(tmp_path, monkeypatch, capsys):
    sd = tmp_path / "skills"
    agents = tmp_path / "agents"
    _make_skill(sd, "stale", NOW - timedelta(days=1))
    _write_ledger(agents, "alpha", [_rec(_iso(NOW - timedelta(days=4)), "stale")])
    monkeypatch.setattr(_mod, "SKILLS_DIR", sd)
    monkeypatch.setattr(_mod, "agents_root", lambda: agents)
    rc = _mod.main(["--output", "human"])
    assert rc == 0
    text = capsys.readouterr().out
    assert "STALE-MODIFIED" in text and "FRESH-STABLE" in text


if __name__ == "__main__":
    import subprocess
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-v"]))
