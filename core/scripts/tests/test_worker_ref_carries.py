"""Tests for the tip-keyed CARRY ledger (worker_ref_carries.py) and the way
worker-ref-consume.sh --check consumes it.

The behaviour under test: a reducer that decides to leave a worker tip
outstanding on purpose can record that decision once, keyed on (ref, tip SHA),
and from then on `--check` (1) prints the tip as CARRIED with the reason,
(2) leaves it out of the STRANDED thresholds, and (3) leaves it out of the
dependency-pull inputs (pull_tip_count / pull_first_ref), which is the producer
that stamps a fleet-visible signal. Three things must stay true, and each has
its own test because a fix that got only the first one right would pass it:

  * a MOVED tip voids the hold with no action (the key is the SHA);
  * a NEW tip beside a held one still raises the signal (the hold is not a
    blanket mute);
  * every failure direction keeps the tip VISIBLE (lapsed, released, voided,
    unreadable ledger): a silenced detector is the dangerous direction.

Hermetic: tmp git repos, and the ledger is redirected with the
WORKER_REF_CARRY_LEDGER seam on EVERY invocation so no test can read or write
the production ledger. `--check` in a tmp repo is structurally unable to stamp
the shared pull signal (the producer refuses a foreign --repo), so the
producer-level assertions read the printed verdict line and the `--json`
footer, which carries the producer's own accumulators.
"""
import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_helpers import BASH  # noqa: E402  (guard-580: never bare "bash" argv)

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
CONSUME = SCRIPTS / "worker-ref-consume.sh"

import coordination_merge as cm  # noqa: E402
import worker_ref_carries as wrc  # noqa: E402

REF_HELD = "refs/workers/alpha/sid-held"
REF_NEW = "refs/workers/alpha/sid-new"
OWNER = "g-900-1"


# --------------------------------------------------------------------- helpers
def _run(cmd, cwd=None, env=None):
    e = os.environ.copy()
    if env:
        e.update(env)
    return subprocess.run(cmd, cwd=cwd, env=e, capture_output=True, text=True, timeout=120)


def _git(repo, *args):
    r = _run(["git", "-C", str(repo), *args])
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r.stdout.strip()


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "carries.jsonl"
    monkeypatch.setenv(wrc.LEDGER_ENV, str(path))
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    return path


def _consume(work, ledger_path, *args):
    env = {wrc.LEDGER_ENV: str(ledger_path), "STORAGE_BACKEND": "local",
           "MIND_AGENT": "alpha"}
    env_clean = {k: v for k, v in os.environ.items() if k != "MIND_SID"}
    env_clean.update(env)
    return subprocess.run([BASH, CONSUME.as_posix(), "--repo", str(work), *args],
                          env=env_clean, capture_output=True, text=True, timeout=120)


def _push_worker_commit(work, base, sid, path, text, on_top_of=None):
    """Commit `path` on top of `on_top_of` (default: base) and push it to the
    carrier ref for `sid`; leave the work clone parked at `base`."""
    _git(work, "checkout", "-q", "--detach", on_top_of or base)
    target = work / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", f"worker {sid} commit")
    sha = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "-q", "origin", f"{sha}:refs/workers/alpha/{sid}")
    _git(work, "checkout", "-q", "--detach", base)
    return sha


@pytest.fixture()
def held(tmp_path):
    """origin + work clone, parked at base, with ONE outstanding tip
    (refs/workers/alpha/sid-held) that carries a framework-path file."""
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    assert _run(["git", "init", "--bare", "--initial-branch=main", str(origin)]).returncode == 0
    assert _run(["git", "clone", str(origin), str(work)]).returncode == 0
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    _git(work, "checkout", "-b", "main")
    (work / "f.txt").write_text("base\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "base")
    _git(work, "push", "origin", "main")
    base = _git(work, "rev-parse", "HEAD")
    tip = _push_worker_commit(work, base, "sid-held", "core/held.sh", "echo held\n")
    return {"work": work, "base": base, "tip": tip}


def _json(r):
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    return {x["sid"]: x for x in data["refs"]}, data


def _carry(held, ledger_path, ref=REF_HELD, tip=None, **kw):
    args = ["--carry", ref, "--tip", (tip or held["tip"])[:12],
            "--owner-goal", kw.get("owner", OWNER),
            "--reason", kw.get("reason", "placement hold, owner gate pending")]
    if "ttl" in kw:
        args += ["--ttl-h", str(kw["ttl"])]
    return _consume(held["work"], ledger_path, *args)


# --------------------------------------------------- ledger unit: status values
NOW = wrc._parse_ts("2026-10-04T12:00:00")
T1, T2 = "a" * 40, "b" * 40


def _rec(disposition="carry", ref=REF_HELD, tip=T1, ts="2026-10-04T10:00:00",
         expires="2026-10-05T10:00:00", **extra):
    r = {"ts": ts, "disposition": disposition, "ref": ref, "tip_sha": tip,
         "owner_goal": OWNER, "reason": "r", "expires_at": expires, "recorded_by": "alpha"}
    r.update(extra)
    return r


def test_status_none_held_voided_lapsed_released():
    assert wrc.evaluate([], REF_HELD, T1, NOW)["state"] == "none"
    assert wrc.evaluate([_rec()], REF_HELD, T1, NOW)["state"] == "held"
    assert wrc.evaluate([_rec()], REF_HELD, T2, NOW)["state"] == "voided", \
        "a different tip SHA must void the hold: the SHA is the key"
    assert wrc.evaluate([_rec(expires="2026-10-04T11:59:59")], REF_HELD, T1, NOW)["state"] == "lapsed"
    assert wrc.evaluate([_rec(), _rec("release", ts="2026-10-04T11:00:00")], REF_HELD, T1, NOW)["state"] == "released"
    assert wrc.evaluate([_rec(ref="refs/workers/alpha/other")], REF_HELD, T1, NOW)["state"] == "none"


def test_a_hold_without_a_readable_expiry_is_not_honoured():
    for bad in (None, "", "not-a-date"):
        rec = _rec()
        rec["expires_at"] = bad
        assert wrc.evaluate([rec], REF_HELD, T1, NOW)["state"] == "lapsed", bad


def test_unknown_disposition_is_not_a_hold():
    assert wrc.evaluate([_rec("mute")], REF_HELD, T1, NOW)["state"] == "none"


def test_latest_record_wins_by_ts_not_by_file_position():
    """A line-union merge interleaves lines, so position cannot be the order."""
    newer_release = _rec("release", ts="2026-10-04T11:00:00")
    older_carry = _rec(ts="2026-10-04T09:00:00")
    assert wrc.evaluate([newer_release, older_carry], REF_HELD, T1, NOW)["state"] == "released"
    assert wrc.evaluate([older_carry, newer_release], REF_HELD, T1, NOW)["state"] == "released"
    newer_carry = _rec(ts="2026-10-04T11:30:00")
    older_release = _rec("release", ts="2026-10-04T09:00:00")
    assert wrc.evaluate([newer_carry, older_release], REF_HELD, T1, NOW)["state"] == "held"


def test_same_second_tie_goes_to_the_later_line():
    a, b = _rec("release", ts="2026-10-04T11:00:00"), _rec(ts="2026-10-04T11:00:00")
    assert wrc.evaluate([a, b], REF_HELD, T1, NOW)["state"] == "held"
    assert wrc.evaluate([b, a], REF_HELD, T1, NOW)["state"] == "released"


def test_sha_comparison_is_case_insensitive():
    assert wrc.evaluate([_rec(tip="A" * 40)], REF_HELD, "a" * 40, NOW)["state"] == "held"


def test_status_line_has_seven_nonempty_tab_fields():
    """bash `IFS=$'\\t' read` collapses empty fields between tabs, so an empty
    field would shift every later one: absent values must be a placeholder."""
    for info in (wrc.evaluate([], REF_HELD, T1, NOW), wrc.evaluate([_rec()], REF_HELD, T1, NOW),
                 {"state": "held", "reason": "line one\nline\ttwo"}):
        parts = wrc.status_line(info).split("\t")
        assert len(parts) == 7 and all(parts), parts
    assert "\n" not in wrc.status_line({"state": "held", "reason": "a\nb\tc"})


# ------------------------------------------------------- ledger unit: the writer
def test_record_then_read_round_trip(ledger):
    rec = wrc.record_carry(ledger, REF_HELD, T1, OWNER, "  placement   hold\nwaiting ", ttl_h=5)
    assert rec["reason"] == "placement hold waiting"
    records, bad = wrc.read_records(ledger)
    assert bad == 0 and len(records) == 1
    info = wrc.evaluate(records, REF_HELD, T1, wrc.utcnow())
    assert info["state"] == "held" and info["owner_goal"] == OWNER
    granted = wrc._parse_ts(rec["expires_at"]) - wrc._parse_ts(rec["ts"])
    assert granted == timedelta(hours=5)


def test_release_is_a_record_not_a_deletion(ledger):
    wrc.record_carry(ledger, REF_HELD, T1, OWNER, "hold")
    wrc.record_release(ledger, REF_HELD, "gate cleared")
    lines = ledger.read_text().splitlines()
    assert len(lines) == 2, "append-only: the release must not remove the carry line"
    assert json.loads(lines[0])["disposition"] == "carry"
    assert wrc.evaluate(wrc.read_records(ledger)[0], REF_HELD, T1, wrc.utcnow())["state"] == "released"


@pytest.mark.parametrize("kwargs, needle", [
    (dict(ref="not-a-ref"), "worker carrier ref"),
    (dict(tip_sha="abc123"), "full commit SHA"),
    (dict(owner_goal="goal-1"), "g-NNN-NN"),
    (dict(ttl_h=0), "ttl"),
    (dict(ttl_h=wrc.MAX_TTL_H + 1), "ttl"),
    (dict(reason="   "), "reason is required"),
    (dict(reason="x" * (wrc.MAX_REASON + 1)), "nothing was truncated"),
])
def test_writer_refuses_malformed_requests_and_writes_nothing(ledger, kwargs, needle):
    args = dict(ref=REF_HELD, tip_sha=T1, owner_goal=OWNER, reason="ok", ttl_h=5)
    args.update(kwargs)
    with pytest.raises(wrc.RecordError, match=needle):
        wrc.record_carry(ledger, args["ref"], args["tip_sha"], args["owner_goal"],
                         args["reason"], args["ttl_h"])
    assert not ledger.exists() or ledger.read_text() == ""


def test_unparseable_lines_are_skipped_not_fatal(ledger):
    ledger.write_text(json.dumps(_rec()) + "\n{broken\n[1, 2]\n\n")
    records, bad = wrc.read_records(ledger)
    assert len(records) == 1 and bad == 2
    assert wrc.evaluate(records, REF_HELD, T1, NOW)["state"] == "held"


def test_missing_ledger_is_empty_but_unreadable_ledger_is_not(tmp_path):
    assert wrc.read_records(tmp_path / "absent.jsonl") == ([], 0)
    (tmp_path / "isdir").mkdir()
    with pytest.raises(OSError):
        wrc.read_records(tmp_path / "isdir")


def test_ledger_is_registered_with_the_append_only_merge_handler():
    assert cm.merge_handler_for("world/worker-ref-carries.jsonl") is cm.merge_append_only_jsonl
    # negative control: an unregistered basename in the same directory is class (b)
    assert cm.merge_handler_for("world/worker-ref-carries-not-registered.jsonl") is None


# ------------------------------------------- worker-ref-consume.sh, end to end
def test_a_held_tip_is_carried_and_leaves_the_pull_inputs(held, ledger):
    # POSITIVE CONTROL, same instrument and same ref, before any hold: the tip
    # carries real framework content, so it IS the producer's input.
    by_sid, data = _json(_consume(held["work"], ledger, "--json"))
    assert by_sid["sid-held"]["carry"] == "none"
    assert data["outstanding"] == 1 and data["carried"] == 0
    assert data["pull_tip_count"] == 1 and data["pull_first_ref"] == REF_HELD

    r = _carry(held, ledger)
    assert r.returncode == 0, r.stderr
    assert "CARRY recorded" in r.stdout

    by_sid, data = _json(_consume(held["work"], ledger, "--json"))
    assert by_sid["sid-held"]["carry"] == "held"
    assert by_sid["sid-held"]["carry_owner_goal"] == OWNER
    assert by_sid["sid-held"]["tip_sha"] == held["tip"]
    assert data["outstanding"] == 1, "a held tip is still outstanding, on purpose"
    assert data["carried"] == 1
    assert data["pull_tip_count"] == 0 and data["pull_first_ref"] == "", \
        "a held tip must not feed the dependency-pull producer"


def test_a_held_tip_is_not_a_stranded_breach_and_names_its_reason(held, ledger):
    breach = _consume(held["work"], ledger, "--check", "--max-depth", "0", "--max-age-h", "9999")
    assert "STRANDED WORKER WORK" in breach.stdout and "sid-held: depth=1" in breach.stdout, breach.stdout
    assert "--carry <ref> --tip <sha>" in breach.stdout, "the banner must say how to hold a tip"

    assert _carry(held, ledger, reason="placement hold, owner gate pending").returncode == 0
    r = _consume(held["work"], ledger, "--check", "--max-depth", "0", "--max-age-h", "9999")
    assert r.returncode == 0, r.stderr
    assert "STRANDED WORKER WORK" not in r.stdout, r.stdout
    assert "CARRIED: held on this exact tip" in r.stdout
    assert "carry reason: placement hold, owner gate pending" in r.stdout
    assert "1 of them CARRIED" in r.stdout
    assert f"tip={held['tip'][:12]}" in r.stdout


def test_check_with_only_a_held_tip_reaches_no_pull_producer_verdict(held, ledger):
    """The producer-level claim: with only a held tip outstanding, --check never
    reaches the pull-signal producer. In a tmp repo the producer's verdict is a
    printed SKIP-foreign-repo line, so its presence/absence is observable
    without any shared-store write."""
    control = _consume(held["work"], ledger, "--check")
    assert "pull: SKIP-foreign-repo" in control.stdout, \
        ("positive control: an unheld framework tip must reach the producer", control.stdout)
    assert _carry(held, ledger).returncode == 0
    r = _consume(held["work"], ledger, "--check")
    assert r.returncode == 0, r.stderr
    assert "pull:" not in r.stdout, r.stdout


def test_a_moved_tip_voids_the_hold_automatically(held, ledger):
    assert _carry(held, ledger).returncode == 0
    assert _json(_consume(held["work"], ledger, "--json"))[0]["sid-held"]["carry"] == "held"

    new_tip = _push_worker_commit(held["work"], held["base"], "sid-held", "core/held2.sh",
                                  "echo more\n", on_top_of=held["tip"])
    by_sid, data = _json(_consume(held["work"], ledger, "--json"))
    row = by_sid["sid-held"]
    assert row["tip_sha"] == new_tip != held["tip"]
    assert row["carry"] == "voided"
    assert data["carried"] == 0 and data["pull_tip_count"] == 1 and data["pull_first_ref"] == REF_HELD

    r = _consume(held["work"], ledger, "--check", "--max-depth", "0", "--max-age-h", "9999")
    assert "sid-held: depth=2" in r.stdout, "a voided hold is a breach again"
    assert "carry VOIDED" in r.stdout and held["tip"][:12] in r.stdout


def test_a_new_tip_beside_a_held_one_still_raises_the_signal(held, ledger):
    assert _carry(held, ledger).returncode == 0
    _push_worker_commit(held["work"], held["base"], "sid-new", "core/new.sh", "echo new\n")
    by_sid, data = _json(_consume(held["work"], ledger, "--json"))
    assert by_sid["sid-held"]["carry"] == "held" and by_sid["sid-new"]["carry"] == "none"
    assert data["outstanding"] == 2 and data["carried"] == 1
    assert data["pull_tip_count"] == 1
    assert data["pull_first_ref"] == REF_NEW, \
        "the signal must name the NEW tip, never the held one"
    r = _consume(held["work"], ledger, "--check")
    assert "pull: SKIP-foreign-repo" in r.stdout, "the new tip must still reach the producer"


def test_default_hold_lifetime_is_72_hours_and_a_hold_always_expires(held, ledger):
    """A hold with no expiry would be a mute switch on the detector it silences;
    the CLI default must therefore be bounded, and the bound must be what the
    header documents."""
    assert _carry(held, ledger).returncode == 0
    rec = json.loads(ledger.read_text().splitlines()[0])
    granted = wrc._parse_ts(rec["expires_at"]) - wrc._parse_ts(rec["ts"])
    assert granted == timedelta(hours=wrc.DEFAULT_TTL_H) == timedelta(hours=72), rec
    assert _carry(held, ledger, ttl=1).returncode == 0
    rec = json.loads(ledger.read_text().splitlines()[1])
    assert wrc._parse_ts(rec["expires_at"]) - wrc._parse_ts(rec["ts"]) == timedelta(hours=1)
    too_long = _carry(held, ledger, ttl=wrc.MAX_TTL_H + 1)
    assert too_long.returncode == 2 and "ttl must be" in too_long.stderr
    assert len(ledger.read_text().splitlines()) == 2, "the refused ttl must write nothing"


def test_a_lapsed_hold_counts_again(held, ledger):
    past = wrc.utcnow() - timedelta(hours=100)
    wrc.record_carry(ledger, REF_HELD, held["tip"], OWNER, "stale hold", ttl_h=72, now=past)
    by_sid, data = _json(_consume(held["work"], ledger, "--json"))
    assert by_sid["sid-held"]["carry"] == "lapsed"
    assert data["pull_tip_count"] == 1 and data["carried"] == 0
    r = _consume(held["work"], ledger, "--check")
    assert "carry LAPSED" in r.stdout and "stale hold" in r.stdout


def test_uncarry_makes_the_tip_count_again(held, ledger):
    assert _carry(held, ledger).returncode == 0
    r = _consume(held["work"], ledger, "--uncarry", REF_HELD, "--reason", "owner gate cleared")
    assert r.returncode == 0, r.stderr
    assert len(ledger.read_text().splitlines()) == 2
    by_sid, data = _json(_consume(held["work"], ledger, "--json"))
    assert by_sid["sid-held"]["carry"] == "released"
    assert data["pull_tip_count"] == 1


def test_carry_refuses_a_tip_that_moved_since_the_audit(held, ledger):
    _push_worker_commit(held["work"], held["base"], "sid-held", "core/held2.sh",
                        "echo more\n", on_top_of=held["tip"])
    r = _carry(held, ledger, tip=held["tip"])      # the OLD tip the operator audited
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "REFUSED" in r.stderr and "moved since you audited it" in r.stderr
    assert not ledger.exists() or ledger.read_text() == "", "a refused carry must write nothing"


@pytest.mark.parametrize("extra, needle", [
    ([], "needs --tip"),
    (["--tip", "abc"], "needs --tip"),
])
def test_carry_requires_a_real_tip(held, ledger, extra, needle):
    r = _consume(held["work"], ledger, "--carry", REF_HELD, "--owner-goal", OWNER,
                 "--reason", "why", *extra)
    assert r.returncode == 1 and needle in r.stderr, (r.stdout, r.stderr)
    assert not ledger.exists() or ledger.read_text() == ""


def test_carry_requires_owner_and_reason(held, ledger):
    tip12 = held["tip"][:12]
    no_reason = _consume(held["work"], ledger, "--carry", REF_HELD, "--tip", tip12, "--owner-goal", OWNER)
    assert no_reason.returncode == 2 and "reason is required" in no_reason.stderr
    no_owner = _consume(held["work"], ledger, "--carry", REF_HELD, "--tip", tip12, "--reason", "why")
    assert no_owner.returncode == 2 and "g-NNN-NN" in no_owner.stderr
    assert not ledger.exists() or ledger.read_text() == ""


def test_carry_of_an_unknown_ref_is_refused(held, ledger):
    r = _consume(held["work"], ledger, "--carry", "refs/workers/alpha/nope", "--tip", "a" * 12,
                 "--owner-goal", OWNER, "--reason", "why")
    assert r.returncode == 1 and "no such ref locally" in r.stderr


def test_an_unreadable_ledger_keeps_the_tip_visible_and_says_so(held, tmp_path):
    bad = tmp_path / "ledger-is-a-directory"
    bad.mkdir()
    by_sid, data = _json(_consume(held["work"], bad, "--json"))
    assert by_sid["sid-held"]["carry"] == "unreadable"
    assert data["carried"] == 0 and data["pull_tip_count"] == 1, \
        "an error must never read as a healthy hold"
    r = _consume(held["work"], bad, "--check", "--max-depth", "0", "--max-age-h", "9999")
    assert "carry ledger UNREADABLE" in r.stdout and "sid-held: depth=1" in r.stdout


def test_a_hold_on_a_superseded_ancestor_is_not_evaluated(held, ledger):
    """Only TIPS are outstanding; an ancestor of another ref is contained in it."""
    anc = _push_worker_commit(held["work"], held["base"], "sid-anc", "core/anc.sh", "echo a\n")
    _push_worker_commit(held["work"], held["base"], "sid-tipper", "core/tip.sh", "echo t\n", on_top_of=anc)
    by_sid, data = _json(_consume(held["work"], ledger, "--json"))
    assert by_sid["sid-anc"]["superseded_by"].endswith("sid-tipper")
    assert by_sid["sid-anc"]["carry"] == "none" and by_sid["sid-tipper"]["carry"] == "none"
