"""Tests for worker_ref_retire_gate.py ( unit (d), guard-3660).

The gate answers one question for `worker-ref-consume.sh --retire` when the Body's
in_flight_bodies row is ABSENT: do two independent signals (the tip commit's clock and the
heartbeat carrier, read as body_row_reaper reads it) agree the Body is gone? Measured on the
live fleet while this was written: all 11 outstanding carrier refs belonged to Bodies whose
carriers read fresh-correct, with tips up to 4.7 days old, so a tip-clock-only gate would have
retired live Bodies' push targets.

Every CARRY branch is pinned by a BEHAVIOURAL test that asserts the refusal PAYLOAD (rc and the
printed line), not by a predicate test alone (guard-3803: a bug while composing a refusal turns
it into an approval, and nothing reports it). Each RETIRE test has a one-variable negative
control beside it (guard-5501).
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_helpers import BASH  # noqa: E402,F401  (guard-580)

import body_row_reaper as reaper  # noqa: E402
import worker_ref_retire_gate as g  # noqa: E402

GATE_PY = SCRIPTS / "worker_ref_retire_gate.py"
STALE = reaper.DEFAULT_REAP_STALE_MINUTES
AGENT = "alpha"
SID = "5f21a43b-0000-4000-8000-000000000001"
REF = "refs/workers/%s/%s" % (AGENT, SID)


def _git(repo, *args, env=None):
    e = os.environ.copy()
    e.update(env or {})
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=e, timeout=60)
    assert p.returncode == 0, "git %s failed: %s" % (args, p.stderr)
    return p.stdout.strip()


class Fx:
    """origin (bare) + work clone; one worker ref whose tip commit is dated `age_min` ago."""

    def __init__(self, tmp_path):
        self.origin = tmp_path / "origin.git"
        self.work = tmp_path / "work"
        subprocess.run(["git", "init", "-q", "--bare", "--initial-branch=main", str(self.origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.work)], check=True,
                       capture_output=True)
        _git(self.work, "config", "user.email", "t@t")
        _git(self.work, "config", "user.name", "t")
        _git(self.work, "checkout", "-q", "-b", "main")
        (self.work / "f.txt").write_text("base\n")
        _git(self.work, "add", ".")
        _git(self.work, "commit", "-q", "-m", "base")
        _git(self.work, "push", "-q", "origin", "main")
        self.n = 0

    def commit(self, age_min):
        self.n += 1
        when = int(time.time() - age_min * 60)
        (self.work / ("w%d.txt" % self.n)).write_text("w%d\n" % self.n)
        _git(self.work, "add", ".")
        _git(self.work, "commit", "-q", "-m", "worker commit %d" % self.n,
             env={"GIT_AUTHOR_DATE": "%d +0000" % when, "GIT_COMMITTER_DATE": "%d +0000" % when})
        return _git(self.work, "rev-parse", "HEAD")

    def carrier_ref(self, age_min, ref=REF, land=False):
        """Push a tip dated `age_min` ago to origin's `ref` and mirror it locally (what the
        script's own fetch does). `land` also merges it into main and pushes main."""
        sha = self.commit(age_min)
        _git(self.work, "push", "-q", "origin", "%s:%s" % (sha, ref))
        _git(self.work, "update-ref", ref, sha)
        if land:
            _git(self.work, "push", "-q", "origin", "main")
        return sha


@pytest.fixture()
def fx(tmp_path):
    return Fx(tmp_path)


class Reader:
    """Stands in for the shared carrier verdict and records every call."""

    def __init__(self, verdict, age=400.0, extra=None, boom=None):
        self.verdict, self.age, self.extra, self.boom, self.calls = verdict, age, extra or {}, boom, []

    def __call__(self, agent, sid, stale_minutes):
        self.calls.append((agent, sid, stale_minutes))
        if self.boom:
            raise self.boom
        return self.verdict, dict({"carrier_age_minutes": self.age}, **self.extra)


def _gate(fx, reader, age_min=STALE + 60, **kw):
    fx.carrier_ref(age_min)
    return g.gate(str(fx.work), REF, AGENT, SID, carrier_reader=reader, **kw)


# --- the one RETIRE path, and each single variable that turns it into CARRY ----------------

def test_old_tip_plus_a_stale_carrier_is_the_only_retire(fx):
    r = Reader("stale")
    out = _gate(fx, r)
    assert out["verdict"] == "RETIRE" and out["code"] == "retire", out
    assert r.calls == [(AGENT, SID, STALE)], "the carrier is read ONCE, with the reaper's threshold"
    assert "heartbeat carrier stale" in out["why"] and "origin's tip is the local copy" in out["why"]


def test_a_recent_tip_carries_and_never_pays_for_the_carrier_read(fx):
    r = Reader("stale")                         # the retire-permitting reader, so only AGE differs
    out = _gate(fx, r, age_min=5)
    assert out["verdict"] == "CARRY" and out["code"] == "tip-recent", out
    assert r.calls == [], "a tip this young already decides it: no store read is spent"
    assert "5 min old" in out["why"]


def test_the_tip_age_boundary_is_the_reapers_constant_on_both_sides(fx):
    below = g.decide({"local_tip": "a" * 40, "remote_tip": "a" * 40, "tip_age_min": STALE - 0.1,
                      "carrier": {"verdict": "stale", "dead": True, "age_min": 400}}, STALE)
    at = g.decide({"local_tip": "a" * 40, "remote_tip": "a" * 40, "tip_age_min": STALE,
                   "carrier": {"verdict": "stale", "dead": True, "age_min": 400}}, STALE)
    assert (below["verdict"], below["code"]) == ("CARRY", "tip-recent")
    assert at["verdict"] == "RETIRE"


def test_a_fresh_correct_carrier_carries_however_old_the_tip(fx):
    """THE MEASURED CASE: tip 4.7 days old, carrier 0 minutes old. A tip clock alone says retire."""
    out = _gate(fx, Reader("fresh-correct", age=0.3), age_min=6788)
    assert out["verdict"] == "CARRY" and out["code"] == "carrier-alive", out
    assert "fresh-correct (0 min old)" in out["why"] and "6788 min old" in out["why"]


@pytest.mark.parametrize("verdict", ["closed", "absent", "fresh-wrong", "unreadable"])
def test_every_carrier_verdict_but_stale_is_carry(fx, verdict):
    out = _gate(fx, Reader(verdict))
    assert (out["verdict"], out["code"]) == ("CARRY", "carrier-not-dead"), out
    assert "'%s'" % verdict in out["why"]


def test_a_stale_carrier_written_by_another_sid_is_not_a_death_certificate(fx):
    """guard-358 as the reaper holds it: the `carrier_sid` key's PRESENCE marks a carrier this
    Body did not write, even when its age reads stale."""
    out = _gate(fx, Reader("stale", extra={"carrier_sid": "0ther"}))
    assert (out["verdict"], out["code"]) == ("CARRY", "carrier-not-dead"), out


@pytest.mark.parametrize("boom", [RuntimeError("store down"), SystemExit(1), ValueError("bad")])
def test_a_failed_carrier_read_is_carry_not_retire(fx, boom):
    out = _gate(fx, Reader("stale", boom=boom))
    assert (out["verdict"], out["code"]) == ("CARRY", "carrier-unmeasured"), out
    assert type(boom).__name__ in out["why"]


def test_a_ref_that_moved_on_origin_is_a_running_body(fx):
    fx.carrier_ref(STALE + 60)
    newer = fx.commit(1)
    _git(fx.work, "push", "-q", "origin", "%s:%s" % (newer, REF))        # origin moves, local copy stays
    r = Reader("stale")
    out = g.gate(str(fx.work), REF, AGENT, SID, carrier_reader=r)
    assert (out["verdict"], out["code"]) == ("CARRY", "ref-moved"), out
    assert newer[:12] in out["why"] and r.calls == []


def test_a_ref_origin_no_longer_has_is_carry(fx):
    fx.carrier_ref(STALE + 60)
    _git(fx.work, "push", "-q", "origin", ":" + REF)
    out = g.gate(str(fx.work), REF, AGENT, SID, carrier_reader=Reader("stale"))
    assert (out["verdict"], out["code"]) == ("CARRY", "remote-unreadable"), out


def test_a_future_dated_tip_is_recent_not_old(fx):
    out = _gate(fx, Reader("stale"), age_min=-30)
    assert (out["verdict"], out["code"]) == ("CARRY", "tip-recent"), out


def test_a_quote_backslash_or_non_ascii_in_an_error_cannot_reach_the_receipt_line(fx):
    """The one-line form is written into a JSON receipt by printf with no escaping."""
    out = _gate(fx, Reader("stale", boom=RuntimeError('bad "quote" and \\ slash café\nsecond line')))
    line = g.line_of(out)
    assert out["code"] == "carrier-unmeasured" and "quote" in line and "slash" in line
    assert not re.search(r'["\\\n\r]', line) and line.isascii(), line


def test_a_ref_name_that_disagrees_with_agent_and_sid_is_carry(fx):
    fx.carrier_ref(STALE + 60)
    out = g.gate(str(fx.work), REF, "bravo", SID, carrier_reader=Reader("stale"))
    assert (out["verdict"], out["code"]) == ("CARRY", "ref-name-mismatch"), out


def test_an_unusable_repo_is_carry_not_an_exception(tmp_path):
    out = g.gate(str(tmp_path / "no-such-repo"), REF, AGENT, SID, carrier_reader=Reader("stale"))
    assert out["verdict"] == "CARRY", out


def test_no_remote_mode_skips_the_origin_reads_and_says_so(fx):
    fx.carrier_ref(STALE + 60)
    out = g.gate(str(fx.work), REF, AGENT, SID, carrier_reader=Reader("stale"), check_remote=False)
    assert out["verdict"] == "RETIRE" and "origin" not in out["why"], out
    assert out["signals"]["check_remote"] is False and "remote_tip" not in out["signals"]


# --- decide(): the signals it needs are named, not guessed ---------------------------------

def test_decide_asks_for_the_carrier_only_after_the_cheap_signals_pass():
    sig = {"local_tip": "a" * 40, "remote_tip": "a" * 40, "tip_age_min": STALE + 1}
    assert g.decide(sig, STALE)["code"] == "need-carrier"
    assert g.decide(dict(sig, tip_age_min=None), STALE)["code"] == "tip-clock-unreadable"
    assert g.decide(dict(sig, remote_tip=None), STALE)["code"] == "remote-unreadable"
    assert g.decide(dict(sig, carrier={"verdict": None, "dead": None, "why": "x"}), STALE)["code"] == "carrier-unmeasured"


# --- the CLI: the printed payload and the exit code, which is what the shell reads -----------

def _cli(fx, *extra, env=None):
    e = os.environ.copy()
    e.update(env or {})
    return subprocess.run([sys.executable, str(GATE_PY), "check", "--repo", str(fx.work), "--ref", REF,
                           "--agent", AGENT, "--sid", SID, *extra],
                          capture_output=True, text=True, env=e, timeout=120, stdin=subprocess.DEVNULL)


def _seam(tmp_path, verdict, age=400, body=None):
    p = tmp_path / ("seam-%s.py" % verdict)
    p.write_text(body or ("import json\nprint(json.dumps({'verdict': %r, 'evidence': {'carrier_age_minutes': %r}}))\n"
                          % (verdict, age)))
    return str(p)


def test_cli_carry_prints_one_line_and_exits_nonzero(fx):
    fx.carrier_ref(5)
    r = _cli(fx)
    assert r.returncode == 1, r.stderr
    assert r.stdout.startswith("CARRY tip-recent: ") and r.stdout.count("\n") == 1, r.stdout
    assert not re.search(r"[\"\\]", r.stdout), "the line goes into a JSON receipt: no quote, no backslash"


def test_cli_retire_through_the_carrier_seam_prints_retire_and_exits_zero(fx, tmp_path):
    fx.carrier_ref(STALE + 60)
    r = _cli(fx, env={g.CARRIER_READER_ENV: _seam(tmp_path, "stale")})
    assert (r.returncode, r.stdout.startswith("RETIRE: ")) == (0, True), (r.stdout, r.stderr)


def test_cli_one_variable_negative_control_alive_carrier_refuses(fx, tmp_path):
    fx.carrier_ref(STALE + 60)
    r = _cli(fx, env={g.CARRIER_READER_ENV: _seam(tmp_path, "fresh-correct", age=1)})
    assert r.returncode == 1 and r.stdout.startswith("CARRY carrier-alive: "), (r.stdout, r.stderr)


def test_cli_a_crashing_carrier_reader_refuses(fx, tmp_path):
    fx.carrier_ref(STALE + 60)
    r = _cli(fx, env={g.CARRIER_READER_ENV: _seam(tmp_path, "x", body="raise SystemExit(7)\n")})
    assert r.returncode == 1 and r.stdout.startswith("CARRY carrier-unmeasured: "), (r.stdout, r.stderr)


def test_cli_garbage_from_the_carrier_reader_refuses(fx, tmp_path):
    fx.carrier_ref(STALE + 60)
    r = _cli(fx, env={g.CARRIER_READER_ENV: _seam(tmp_path, "x", body="print('not json')\n")})
    assert r.returncode == 1 and r.stdout.startswith("CARRY carrier-unmeasured: "), (r.stdout, r.stderr)


def test_cli_usage_error_is_nonzero_and_never_prints_retire(fx):
    r = subprocess.run([sys.executable, str(GATE_PY), "check", "--repo", str(fx.work), "--ref", REF],
                       capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert r.returncode != 0 and "RETIRE" not in r.stdout


def test_cli_json_carries_the_measured_signals(fx):
    fx.carrier_ref(5)
    doc = json.loads(_cli(fx, "--json").stdout)
    assert doc["verdict"] == "CARRY" and doc["code"] == "tip-recent"
    assert doc["signals"]["tip_age_min"] == pytest.approx(5, abs=1.5)
    assert doc["signals"]["local_tip"] == doc["signals"]["remote_tip"]


# --- one opinion about one Body: nothing here restates the reaper's, and the real functions run ---

def test_the_threshold_is_the_reapers_constant_not_a_second_literal():
    src = GATE_PY.read_text(encoding="utf-8")
    assert "DEFAULT_REAP_STALE_MINUTES" in src
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert not re.search(r"\b180(?:\.0)?\b", code), "a numeric literal here would be a second threshold"


@pytest.mark.parametrize("verdict,evidence,dead", [
    (reaper.CV_STALE, {}, True),
    (reaper.CV_STALE, {"carrier_sid": "0ther"}, False),
    (reaper.CV_FRESH_CORRECT, {}, False),
    (reaper.CV_FRESH_WRONG, {}, False),
    (reaper.CV_ABSENT, {}, False),
    (reaper.CV_UNREADABLE, {}, False),
    (reaper.CV_CLOSED, {}, False),
    ("a-sixth-token-added-upstream", {}, False),
])
def test_only_the_reapers_stale_verdict_calls_a_body_dead(verdict, evidence, dead):
    assert g.reaper_calls_dead(SID, verdict, evidence) is dead


def _scs(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("scs_gate_ut", SCRIPTS / "stranded-claim-sweep.py")
    scs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scs)
    agent_dir = tmp_path / "agents" / AGENT
    (agent_dir / "session").mkdir(parents=True)
    monkeypatch.setattr(scs, "agent_dir", lambda name: agent_dir)
    monkeypatch.delenv(g.CARRIER_READER_ENV, raising=False)
    monkeypatch.setattr(g, "_load_sweep", lambda: scs)
    return agent_dir


def _write_carrier(agent_dir, minutes_ago, sid=SID, state="active"):
    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - minutes_ago * 60))
    (agent_dir / "session" / ("body-heartbeat-%s.json" % SID)).write_text(
        json.dumps({"sid": sid, "agent": AGENT, "host": "box-1", "ts": ts, "body_state": state}),
        encoding="utf-8")


def test_the_real_carrier_verdict_and_the_real_reaper_decide_end_to_end(fx, tmp_path, monkeypatch):
    """No stub on either side: real files through the shared verdict function into the reaper."""
    agent_dir = _scs(tmp_path, monkeypatch)
    fx.carrier_ref(STALE + 60)
    _write_carrier(agent_dir, STALE + 90)
    assert g.gate(str(fx.work), REF, AGENT, SID)["verdict"] == "RETIRE"
    _write_carrier(agent_dir, 5)                                   # ONE variable: the carrier is fresh
    out = g.gate(str(fx.work), REF, AGENT, SID)
    assert (out["verdict"], out["code"]) == ("CARRY", "carrier-alive"), out
    _write_carrier(agent_dir, STALE + 90, sid="0ther-sid")         # old, but another Body's carrier
    assert g.gate(str(fx.work), REF, AGENT, SID)["verdict"] == "CARRY"
    (agent_dir / "session" / ("body-heartbeat-%s.json" % SID)).unlink()    # no carrier at all
    out = g.gate(str(fx.work), REF, AGENT, SID)
    assert (out["verdict"], out["code"]) == ("CARRY", "carrier-not-dead"), out
    assert "'absent'" in out["why"]


def test_the_default_reader_calls_the_sweeps_verdict_with_the_reapers_threshold(monkeypatch):
    seen = []

    class FakeSweep:
        @staticmethod
        def _body_carrier_verdict(agent, sid, fresh_minutes):
            seen.append((agent, sid, fresh_minutes))
            return "stale", {}

    monkeypatch.delenv(g.CARRIER_READER_ENV, raising=False)
    monkeypatch.setattr(g, "_load_sweep", lambda: FakeSweep)
    assert g.read_carrier(AGENT, SID, STALE) == ("stale", {})
    assert seen == [(AGENT, SID, int(STALE))]
    sweep_src = (SCRIPTS / "stranded-claim-sweep.py").read_text(encoding="utf-8")
    assert "def _body_carrier_verdict(" in sweep_src, "the function the default reader calls must exist"
