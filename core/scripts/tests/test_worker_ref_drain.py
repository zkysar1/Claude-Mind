"""Tests for worker_ref_drain.py and `worker-ref-consume.sh --drain` (, unit 1).

The plan is READ-ONLY and its verdicts decide what a reducer merges, so every
test here pins a verdict against a real git fixture and, where a verdict is
built from a measurement that can fail quietly, against the failing read too.
The measurement that fails quietly is `git merge-tree`: a bad ref exits 1 with
NOTHING on stdout, exactly like a conflict's rc, so a classifier keyed on rc
alone reads an unreadable tip as a conflicted one (and a different one reads it
as clean). Both directions are pinned.

All git fixtures are self-contained tmp repos; no network, no live daemon, no
world store (--drain never runs --check, whose producer gate writes one).
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_helpers import BASH  # noqa: E402  (guard-580: never bare "bash" argv)

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import worker_ref_drain as wrd  # noqa: E402

CONSUME = SCRIPTS / "worker-ref-consume.sh"
DRAIN_PY = SCRIPTS / "worker_ref_drain.py"
AUDIT_PY = SCRIPTS / "carrier_merge_audit.py"
OID = "a" * 40
DAY1 = "2026-01-01T00:00:00Z"
DAY2 = "2026-01-02T00:00:00Z"


def _run(cmd, cwd=None, env=None, stdin=None):
    e = os.environ.copy()
    if env:
        e.update(env)
    return subprocess.run(cmd, cwd=cwd, env=e, input=stdin, capture_output=True, text=True, timeout=120)


def _git(repo, *args, env=None):
    r = _run(["git", "-C", str(repo), *args], env=env)
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r.stdout.strip()


class Fx:
    def __init__(self, origin, work):
        self.origin, self.work = origin, work

    def tip(self, sid, files, when=DAY1, base="main", force_add=False):
        """One commit on `base` carrying `files`, pushed to refs/workers/alpha/<sid>."""
        env = {"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
        _git(self.work, "checkout", "-q", "-b", "tmp-" + sid, base)
        for rel, text in files.items():
            p = Path(self.work) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        if force_add:
            _git(self.work, "add", "-f", "--", *files)
        else:
            _git(self.work, "add", "-A")
        _git(self.work, "commit", "-q", "-m", "worker " + sid, env=env)
        sha = _git(self.work, "rev-parse", "HEAD")
        _git(self.work, "push", "-q", "origin", f"{sha}:refs/workers/alpha/{sid}")
        _git(self.work, "checkout", "-q", "main")
        _git(self.work, "branch", "-q", "-D", "tmp-" + sid)
        return sha

    def commit_main(self, files, msg="main change"):
        for rel, text in files.items():
            p = Path(self.work) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "-q", "-m", msg)
        return _git(self.work, "rev-parse", "HEAD")

    def fetch_workers(self):
        _git(self.work, "fetch", "-q", "--prune", "origin", "+refs/workers/*:refs/workers/*")

    def consume(self, *args):
        return _run([BASH, CONSUME.as_posix(), "--repo", str(self.work), *args])

    def plan(self, *extra):
        r = self.consume("--drain", "--json", *extra)
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)

    def report(self):
        """The existing --json report (fetches the worker refs pushed to origin first)."""
        r = self.consume("--json")
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)


@pytest.fixture()
def fx(tmp_path):
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    assert _run(["git", "init", "-q", "--bare", "--initial-branch=main", str(origin)]).returncode == 0
    assert _run(["git", "clone", "-q", str(origin), str(work)]).returncode == 0
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    _git(work, "checkout", "-q", "-b", "main")
    (work / "f.txt").write_text("a\nb\nc\n")
    (work / "other.txt").write_text("o\n")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "base")
    _git(work, "push", "-q", "origin", "main")
    return Fx(origin, work)


def _by_sid(plan):
    return {t["ref"].rsplit("/", 1)[1]: t for t in plan["tips"]}


def _codes(tip):
    return [r["code"] for r in tip["reasons"]]


def _stub_audit(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(body)
    return str(p)


# ---------------------------------------------------------------- pure parsers

def test_merge_tree_clean_is_clean():
    assert wrd.parse_merge_tree_z(0, OID.encode() + b"\0") == ("clean", OID, [])


def test_merge_tree_conflict_lists_names_and_ignores_messages():
    out = (OID.encode() + b"\0f.txt\0g.txt\0\0" + b"1\0f.txt\0CONFLICT (contents)\0"
           b"CONFLICT (content): Merge conflict in f.txt\n\0")
    assert wrd.parse_merge_tree_z(1, out) == ("conflict", OID, ["f.txt", "g.txt"])


def test_merge_tree_conflict_with_no_parsed_name_is_still_a_conflict():
    assert wrd.parse_merge_tree_z(1, OID.encode() + b"\0\0")[0] == "conflict"


def test_merge_tree_rc1_with_empty_stdout_is_an_error_not_a_conflict():
    # A bad ref exits 1 with nothing on stdout (measured, git 2.43).
    assert wrd.parse_merge_tree_z(1, b"") == ("error", None, [])


@pytest.mark.parametrize("rc,out", [
    (128, b""),                                  # unrelated histories
    (128, OID.encode() + b"\0"),                 # a tree id does not rescue a fatal rc
    (0, OID.encode() + b"\0f.txt\0"),            # rc 0 naming conflicts is inconsistent
    (0, b""),
    (2, OID.encode() + b"\0f.txt\0\0"),
])
def test_merge_tree_inconsistent_or_failed_runs_are_errors(rc, out):
    assert wrd.parse_merge_tree_z(rc, out)[0] == "error"


def test_numstat_binary_row_is_none_never_zero():
    rows = wrd.parse_numstat_z(b"3\t1\tf.txt\0-\t-\timg.bin\0")
    assert rows == [(3, 1, "f.txt"), (None, None, "img.bin")]


def test_numstat_malformed_record_raises():
    with pytest.raises(ValueError):
        wrd.parse_numstat_z(b"3\tf.txt\0")


# ------------------------------------------------------------------ the ladder

def _m(**kw):
    m = {"status": "clean", "error": "", "tree": OID, "conflicts": [], "paths": ["docs/x.md"],
         "added": 1, "deleted": 0, "binary": 0, "framework": 0, "fw_deleting": 0, "surface": [], "ledger": []}
    m.update(kw)
    return m


def test_decide_clean_tip_is_merge():
    assert wrd.decide(_m(), 0, []) == ("MERGE", [])


def test_decide_surface_hit_is_verify_first():
    v, rs = wrd.decide(_m(surface=["mind_api/src/x.py"]), 0, [])
    assert v == "MERGE-VERIFY-FIRST" and [r["code"] for r in rs] == ["surface"]


def test_decide_unclassified_surface_is_verify_first_never_merge():
    v, rs = wrd.decide(_m(surface=None), 0, [], "detector unreadable")
    assert v == "MERGE-VERIFY-FIRST" and rs[0]["code"] == "surface-unmeasured"
    assert "detector unreadable" in rs[0]["text"]


def test_decide_phantom_is_carry():
    v, rs = wrd.decide(_m(paths=[]), 0, [])
    assert v == "CARRY" and [r["code"] for r in rs] == ["phantom"]


def test_decide_retrack_is_carry_and_unmeasured_retrack_is_stop():
    assert wrd.decide(_m(), 2, [])[0] == "CARRY"
    v, rs = wrd.decide(_m(), -1, [])
    assert v == "STOP" and rs[0]["code"] == "retrack-unmeasured"


def test_decide_unmeasured_retrack_does_not_stop_a_tip_that_changes_nothing():
    assert wrd.decide(_m(paths=[]), -1, [])[0] == "CARRY"


def test_decide_dirty_overlap_and_unreadable_status_are_stop():
    v, rs = wrd.decide(_m(), 0, ["f.txt"])
    assert v == "STOP" and rs[0]["code"] == "dirty-overlap" and "f.txt" in rs[0]["text"]
    v, rs = wrd.decide(_m(), 0, None)
    assert v == "STOP" and rs[0]["code"] == "dirty-unmeasured"


@pytest.mark.parametrize("verdict", ["NOT CLEARED", "UNMEASURED"])
def test_decide_ledger_that_is_not_clears_is_stop(verdict):
    led = [{"path": "a.jsonl", "verdict": verdict, "reasons": ["why"]}]
    v, rs = wrd.decide(_m(ledger=led), 0, [])
    assert v == "STOP" and rs[0]["code"] == "ledger" and verdict in rs[0]["text"]


def test_decide_ledger_clears_is_not_a_reason():
    led = [{"path": "a.jsonl", "verdict": "CLEARS", "reasons": []}]
    assert wrd.decide(_m(ledger=led), 0, []) == ("MERGE", [])


def test_decide_conflict_and_failed_preview_are_stop():
    v, rs = wrd.decide(_m(status="conflict", conflicts=["f.txt"], paths=[]), 0, [])
    assert v == "STOP" and [r["code"] for r in rs] == ["conflict"] and "f.txt" in rs[0]["text"]
    v, rs = wrd.decide(_m(status="error", error="boom", paths=[]), 0, [])
    assert v == "STOP" and [r["code"] for r in rs] == ["preview-failed"] and "boom" in rs[0]["text"]
    assert "conflict" not in rs[0]["text"].replace("not a conflict", "")


def test_decide_lists_every_reason_and_the_strongest_decides():
    v, rs = wrd.decide(_m(surface=["mind_api/src/x.py"]), 0, ["f.txt"])
    assert v == "STOP" and sorted(r["code"] for r in rs) == ["dirty-overlap", "surface"]


def test_decide_broken_chain_is_stop():
    v, rs = wrd.decide(_m(), 0, [], chain_err="no preview commit")
    assert v == "STOP" and rs[0]["code"] == "chain-broken"


# ------------------------------------------------------- real-repo verdicts

def test_clean_tip_is_merge_and_report_numbers_agree(fx):
    fx.tip("sid-clean", {"docs/x.md": "x\n"})
    plan = fx.plan()
    t = _by_sid(plan)["sid-clean"]
    assert t["verdict"] == "MERGE" and t["reasons"] == []
    m = t["preview"]
    assert (m["status"], m["paths"], m["added"], m["deleted"]) == ("clean", ["docs/x.md"], 1, 0)
    # The independent reading of the existing --json report must agree.
    row = next(r for r in fx.report()["refs"] if r["sid"] == "sid-clean")
    assert (row["merge_paths_real"], row["merge_added_real"], row["merge_deleted_real"]) == (1, 1, 0)
    assert plan["summary"]["MERGE"] == 1 and plan["summary"]["tips"] == 1


def test_conflicting_tip_is_stop_naming_the_path_and_a_clean_tip_beside_it_is_not(fx):
    fx.tip("sid-conf", {"f.txt": "a\nTIP\nc\n"})
    fx.tip("sid-ok", {"docs/ok.md": "ok\n"}, when=DAY2)
    fx.commit_main({"f.txt": "a\nMAIN\nc\n"})
    by = _by_sid(fx.plan())
    assert by["sid-conf"]["verdict"] == "STOP" and _codes(by["sid-conf"]) == ["conflict"]
    assert by["sid-conf"]["preview"]["conflicts"] == ["f.txt"]
    assert by["sid-ok"]["verdict"] == "MERGE"


def test_unresolvable_tip_is_stop_preview_failed_never_a_conflict_or_clean(fx):
    fx.tip("sid-real", {"docs/x.md": "x\n"})
    doc = fx.report()
    doc["refs"].append({"ref": "refs/workers/alpha/sid-ghost", "tip_sha": "0123456789abcdef" * 2 + "01234567",
                        "commits_ahead": 1, "unreadable": 0, "is_self": 0, "superseded_by": "",
                        "carry": "none", "merge_retrack_real": 0})
    plan = wrd.build_plan(str(fx.work), doc, str(AUDIT_PY))
    ghost = _by_sid(plan)["sid-ghost"]
    assert ghost["verdict"] == "STOP" and _codes(ghost) == ["preview-failed"]
    assert ghost["preview"]["status"] == "error" and ghost["preview"]["conflicts"] == []
    assert _by_sid(plan)["sid-real"]["verdict"] == "MERGE"


def test_daemon_surface_tip_is_verify_first_and_a_doc_tip_is_not(fx):
    fx.tip("sid-daemon", {"mind_api/src/new_mod.py": "x = 1\n"})
    fx.tip("sid-doc", {"docs/y.md": "y\n"}, when=DAY2)
    by = _by_sid(fx.plan())
    assert by["sid-daemon"]["verdict"] == "MERGE-VERIFY-FIRST"
    assert by["sid-daemon"]["preview"]["surface"] == ["mind_api/src/new_mod.py"]
    assert by["sid-doc"]["verdict"] == "MERGE" and by["sid-doc"]["preview"]["surface"] == []
    assert by["sid-daemon"]["preview"]["framework"] == 1  # mind_api/src is framework too


def test_record_deleting_ledger_tip_is_stop_not_cleared_and_pure_append_is_not_audited(fx):
    ledger = "".join(json.dumps({"id": "r%d" % i, "v": i}) + "\n" for i in (1, 2, 3))
    fx.commit_main({"agents/alpha/experience.jsonl": ledger}, "ledger base")
    _git(fx.work, "push", "-q", "origin", "main")
    base = _git(fx.work, "rev-parse", "HEAD")
    fx.tip("sid-drop", {"agents/alpha/experience.jsonl": ledger.split("\n", 1)[1]}, base=base)
    fx.commit_main({"agents/alpha/experience.jsonl": ledger + json.dumps({"id": "r4", "v": 4}) + "\n"}, "main appends r4")
    fx.tip("sid-append", {"agents/beta/experience.jsonl": json.dumps({"id": "b1"}) + "\n"}, when=DAY2)
    by = _by_sid(fx.plan())
    drop = by["sid-drop"]
    assert drop["verdict"] == "STOP" and _codes(drop) == ["ledger"]
    assert [(r["path"], r["verdict"]) for r in drop["preview"]["ledger"]] == [("agents/alpha/experience.jsonl", "NOT CLEARED")]
    assert by["sid-append"]["verdict"] == "MERGE" and by["sid-append"]["preview"]["ledger"] == []


def test_ledger_audit_verdicts_through_a_stub_helper(fx, tmp_path):
    ledger = json.dumps({"id": "r1"}) + "\n" + json.dumps({"id": "r2"}) + "\n"
    fx.commit_main({"a.jsonl": ledger}, "ledger")
    fx.tip("sid-l", {"a.jsonl": json.dumps({"id": "r1"}) + "\n"}, base=_git(fx.work, "rev-parse", "HEAD"))
    doc = fx.report()
    cases = {
        "clears": ('import json,sys; print(json.dumps({"verdict":"CLEARS","reasons":[]}))', "MERGE", "CLEARS"),
        "notcleared": ('import json; print(json.dumps({"verdict":"NOT CLEARED","reasons":["died=1"]}))', "STOP", "NOT CLEARED"),
        "garbage": ("print('not json')", "STOP", "UNMEASURED"),
        "crash": ("import sys; sys.exit(7)", "STOP", "UNMEASURED"),
        "alien": ('import json; print(json.dumps({"verdict":"FINE"}))', "STOP", "UNMEASURED"),
    }
    for name, (body, want_verdict, want_audit) in cases.items():
        stub = _stub_audit(tmp_path, name + ".py", body + "\n")
        t = _by_sid(wrd.build_plan(str(fx.work), doc, stub))["sid-l"]
        assert (t["verdict"], t["preview"]["ledger"][0]["verdict"]) == (want_verdict, want_audit), name


def test_ledger_paths_past_the_audit_cap_read_unmeasured(fx, tmp_path):
    names = ["l%d.jsonl" % i for i in range(wrd.AUDIT_CAP + 2)]
    fx.commit_main({n: json.dumps({"id": "r1"}) + "\n" + json.dumps({"id": "r2"}) + "\n" for n in names}, "ledgers")
    fx.tip("sid-many", {n: json.dumps({"id": "r1"}) + "\n" for n in names}, base=_git(fx.work, "rev-parse", "HEAD"))
    stub = _stub_audit(tmp_path, "clears.py", 'import json; print(json.dumps({"verdict":"CLEARS","reasons":[]}))\n')
    t = _by_sid(wrd.build_plan(str(fx.work), fx.report(), stub))["sid-many"]
    led = t["preview"]["ledger"]
    assert len(led) == wrd.AUDIT_CAP + 1 and led[-1]["verdict"] == "UNMEASURED" and "cap" in led[-1]["reasons"][0]
    assert t["verdict"] == "STOP" and _codes(t) == ["ledger"]


def test_chained_preview_finds_a_conflict_that_exists_only_after_the_earlier_tip(fx):
    fx.tip("sid-t1", {"f.txt": "a\nB1\nc\n"}, when=DAY1)
    t2 = fx.tip("sid-t2", {"f.txt": "a\nB2\nc\n"}, when=DAY2)
    # Control: against HEAD alone the second tip is clean, so only the chain can see it.
    specs, _ = wrd.surface_specs()
    alone = wrd.measure_tip(str(fx.work), "HEAD", t2, specs, str(AUDIT_PY))
    assert alone["status"] == "clean"
    plan = fx.plan()
    by = _by_sid(plan)
    assert [t["ref"].rsplit("/", 1)[1] for t in plan["tips"]] == ["sid-t1", "sid-t2"]  # oldest tip first
    assert by["sid-t1"]["verdict"] == "MERGE"
    assert by["sid-t2"]["verdict"] == "STOP" and _codes(by["sid-t2"]) == ["conflict"]
    assert by["sid-t2"]["preview"]["conflicts"] == ["f.txt"]


def test_tips_merge_in_tip_date_order_not_ref_name_order(fx):
    fx.tip("sid-a-newer", {"docs/a.md": "a\n"}, when=DAY2)
    fx.tip("sid-z-older", {"docs/z.md": "z\n"}, when=DAY1)
    assert [t["ref"].rsplit("/", 1)[1] for t in fx.plan()["tips"]] == ["sid-z-older", "sid-a-newer"]


def test_a_stop_tip_is_left_out_of_the_chain(fx):
    fx.tip("sid-conf", {"f.txt": "a\nTIP\nc\n"}, when=DAY1)
    fx.tip("sid-after", {"docs/x.md": "x\n"}, when=DAY2)
    fx.commit_main({"f.txt": "a\nMAIN\nc\n"})
    by = _by_sid(fx.plan())
    assert by["sid-conf"]["verdict"] == "STOP"
    assert by["sid-after"]["verdict"] == "MERGE" and by["sid-after"]["shared"] == []


def test_shared_path_is_reported_for_a_later_tip_that_touches_the_same_file(fx):
    fx.tip("sid-s1", {"f.txt": "A1\nb\nc\n"}, when=DAY1)
    fx.tip("sid-s2", {"f.txt": "a\nb\nC2\n"}, when=DAY2)
    by = _by_sid(fx.plan())
    assert by["sid-s1"]["verdict"] == "MERGE" and by["sid-s2"]["verdict"] == "MERGE"
    assert by["sid-s2"]["shared"] == ["f.txt"] and by["sid-s1"]["shared"] == []


def test_dirty_overlap_is_stop_and_an_unrelated_dirty_file_is_not(fx):
    fx.tip("sid-f", {"f.txt": "a\nB\nc\n"})
    (Path(fx.work) / "other.txt").write_text("dirty but unrelated\n")
    assert _by_sid(fx.plan())["sid-f"]["verdict"] == "MERGE"
    (Path(fx.work) / "f.txt").write_text("dirty and overlapping\n")
    t = _by_sid(fx.plan())["sid-f"]
    assert t["verdict"] == "STOP" and _codes(t) == ["dirty-overlap"] and "f.txt" in t["reasons"][0]["text"]


def test_phantom_tip_whose_content_is_already_at_head_is_carry(fx):
    fx.tip("sid-ph", {"p.txt": "same\n"})
    fx.commit_main({"p.txt": "same\n"}, "landed by another route")
    t = _by_sid(fx.plan())["sid-ph"]
    assert t["verdict"] == "CARRY" and _codes(t) == ["phantom"] and t["preview"]["paths"] == []


def test_retrack_comes_from_the_report_field_and_is_carry(fx):
    fx.commit_main({".gitignore": "spool/\n"}, "ignore spool")
    _git(fx.work, "push", "-q", "origin", "main")
    fx.tip("sid-rt", {"spool/x.txt": "x\n"}, base=_git(fx.work, "rev-parse", "HEAD"), force_add=True)
    fx.tip("sid-ok", {"docs/ok.md": "ok\n"}, when=DAY2)
    rows = {r["sid"]: r for r in fx.report()["refs"]}
    assert rows["sid-rt"]["merge_retrack_real"] == 1 and rows["sid-ok"]["merge_retrack_real"] == 0
    by = _by_sid(fx.plan())
    assert by["sid-rt"]["verdict"] == "CARRY" and _codes(by["sid-rt"]) == ["retrack"]
    assert by["sid-ok"]["verdict"] == "MERGE"


def test_stale_base_is_flagged_and_a_tip_already_on_origin_main_is_marked(fx):
    base = _git(fx.work, "rev-parse", "HEAD")
    tip = fx.tip("sid-up", {"u.txt": "u\n"})
    _git(fx.work, "merge", "-q", "--no-ff", "-m", "land sid-up", tip)
    _git(fx.work, "push", "-q", "origin", "main")
    _git(fx.work, "reset", "-q", "--hard", base)
    plan = fx.plan()
    assert plan["base"]["fresh"] is False and plan["base"]["behind"] == 2
    t = _by_sid(plan)["sid-up"]
    assert t["landed_upstream"] is True
    assert [c["ref"].rsplit("/", 1)[1] for c in plan["retire_candidates"]] == ["sid-up"]
    r = fx.consume("--drain")
    assert "STALE BASE" in r.stdout and "behind=2" in r.stdout and "already on origin/main" in r.stdout


def test_fresh_base_has_no_stale_warning_and_a_pin(fx):
    fx.tip("sid-x", {"docs/x.md": "x\n"})
    plan = fx.plan()
    assert plan["base"]["fresh"] is True and plan["pin"]["refs"] == 1 and len(plan["pin"]["digest"]) == 12
    assert "STALE BASE" not in fx.consume("--drain").stdout


def test_pin_changes_when_a_worker_ref_moves(fx):
    fx.tip("sid-p", {"docs/p.md": "p\n"})
    before = fx.plan()["pin"]["digest"]
    fx.tip("sid-q", {"docs/q.md": "q\n"}, when=DAY2)
    assert fx.plan()["pin"]["digest"] != before


def test_merged_refs_are_retire_candidates_and_the_own_body_never_is(fx):
    old = fx.tip("sid-old", {"docs/o.md": "o\n"})
    _git(fx.work, "merge", "-q", "--ff-only", old)
    _git(fx.work, "push", "-q", "origin", "main")
    own = fx.tip("sid-own", {"docs/w.md": "w\n"}, base="main")
    _git(fx.work, "merge", "-q", "--ff-only", own)
    _git(fx.work, "push", "-q", "origin", "main")
    fx.fetch_workers()
    r = _run([BASH, CONSUME.as_posix(), "--repo", str(fx.work), "--drain", "--json", "--no-fetch"],
             env={"MIND_SID": "sid-own"})
    plan = json.loads(r.stdout)
    assert plan["tips"] == []
    assert [c["ref"].rsplit("/", 1)[1] for c in plan["retire_candidates"]] == ["sid-old"]


def test_a_failed_preview_commit_breaks_the_chain_and_stops_the_later_tips(fx, monkeypatch):
    fx.tip("sid-1", {"docs/1.md": "1\n"}, when=DAY1)
    fx.tip("sid-2", {"docs/2.md": "2\n"}, when=DAY2)
    doc = fx.report()
    monkeypatch.setattr(wrd, "preview_commit", lambda repo, tree, base, tip: (None, "boom"))
    by = _by_sid(wrd.build_plan(str(fx.work), doc, str(AUDIT_PY)))
    assert by["sid-1"]["verdict"] == "MERGE"
    assert by["sid-2"]["verdict"] == "STOP" and _codes(by["sid-2"]) == ["chain-broken"]
    assert "boom" in by["sid-2"]["reasons"][0]["text"]


def test_head_moving_while_planning_is_flagged(fx, monkeypatch):
    fx.tip("sid-x", {"docs/x.md": "x\n"})
    doc = fx.report()
    real, seen = wrd.rev_commit, {"HEAD": 0}

    def moving(repo, name):
        if name == "HEAD":
            seen["HEAD"] += 1
            if seen["HEAD"] > 1:
                return "b" * 40
        return real(repo, name)
    monkeypatch.setattr(wrd, "rev_commit", moving)
    plan = wrd.build_plan(str(fx.work), doc, str(AUDIT_PY))
    assert plan["head_moved"] is True and plan["head_end"] == "b" * 40
    assert "WARN HEAD moved from" in "\n".join(wrd.render(plan))


# ------------------------------------------------------- partition and render

def test_partition_separates_tips_held_unreadable_and_others(fx):
    tip = fx.tip("sid-t", {"docs/t.md": "t\n"})
    row = {"commits_ahead": 1, "unreadable": 0, "is_self": 0, "superseded_by": "", "carry": "none"}
    doc = {"refs": [
        dict(row, ref="refs/workers/alpha/sid-t", tip_sha=tip),
        dict(row, ref="refs/workers/alpha/sid-held", tip_sha=OID, carry="held", carry_owner_goal="g-1-1"),
        dict(row, ref="refs/workers/alpha/sid-bad", tip_sha=OID, unreadable=1),
        dict(row, ref="not-a-worker-ref", tip_sha=OID),
        dict(row, ref="refs/workers/alpha/sid-nosha", tip_sha=""),
        dict(row, ref="refs/workers/alpha/sid-self", tip_sha=OID, is_self=1),
        dict(row, ref="refs/workers/alpha/sid-anc", tip_sha=OID, superseded_by="refs/workers/alpha/sid-t"),
        dict(row, ref="refs/workers/alpha/sid-zero", tip_sha=OID, commits_ahead=0),
    ]}
    tips, held, unreadable, others = wrd.partition(str(fx.work), doc)
    assert [t["ref"] for t in tips] == ["refs/workers/alpha/sid-t"]
    assert [(h["ref"], h["owner_goal"]) for h in held] == [("refs/workers/alpha/sid-held", "g-1-1")]
    assert sorted(u["ref"] for u in unreadable) == sorted(
        ["refs/workers/alpha/sid-bad", "not-a-worker-ref", "refs/workers/alpha/sid-nosha"])
    assert sorted(o["ref"] for o in others) == sorted(
        ["refs/workers/alpha/sid-self", "refs/workers/alpha/sid-anc", "refs/workers/alpha/sid-zero"])


def test_held_and_unreadable_rows_are_listed_and_never_previewed(fx):
    doc = {"refs": [
        {"ref": "refs/workers/alpha/sid-held", "tip_sha": OID, "commits_ahead": 2, "carry": "held",
         "carry_owner_goal": "g-9-9", "is_self": 0, "superseded_by": "", "unreadable": 0},
        {"ref": "refs/workers/alpha/sid-bad", "tip_sha": OID, "commits_ahead": 0, "unreadable": 1,
         "is_self": 0, "superseded_by": ""}]}
    plan = wrd.build_plan(str(fx.work), doc, str(AUDIT_PY))
    assert plan["tips"] == [] and len(plan["held"]) == 1 and len(plan["unreadable"]) == 1
    text = "\n".join(wrd.render(plan))
    assert "held on their exact tip" in text and "g-9-9" in text and "UNREADABLE (not planned)" in text
    assert "no outstanding TIP to plan" in text and "held=1 unreadable=1" in text


def test_render_names_the_verdict_the_reason_and_ends_on_the_summary_line(fx):
    fx.tip("sid-conf", {"f.txt": "a\nTIP\nc\n"})
    fx.commit_main({"f.txt": "a\nMAIN\nc\n"})
    r = fx.consume("--drain")
    assert r.returncode == 0, r.stderr
    lines = r.stdout.rstrip("\n").split("\n")
    assert any(l.strip().startswith("1. STOP") for l in lines)
    assert any("conflict: merge conflicts in 1 path(s): f.txt" in l for l in lines)
    assert re.match(r"drain plan: 1 tip\(s\): MERGE=0 MERGE-VERIFY-FIRST=0 CARRY=0 STOP=1 \| held=0 unreadable=0 \|",
                    lines[-1])


# ------------------------------------------------------------- the CLI itself

def test_plan_is_read_only(fx):
    fx.tip("sid-conf", {"f.txt": "a\nTIP\nc\n"}, when=DAY1)
    fx.tip("sid-t2", {"f.txt": "a\nT2\nc\n"}, when=DAY2)
    fx.tip("sid-ok", {"docs/ok.md": "ok\n"}, when="2026-01-03T00:00:00Z")
    fx.commit_main({"f.txt": "a\nMAIN\nc\n"})
    (Path(fx.work) / "other.txt").write_text("dirty\n")
    (Path(fx.work) / "untracked.txt").write_text("u\n")
    fx.fetch_workers()

    def snap():
        return {
            "head": _git(fx.work, "rev-parse", "HEAD"),
            "refs": _git(fx.work, "for-each-ref", "--format=%(refname) %(objectname)"),
            "status": _run(["git", "-C", str(fx.work), "status", "--porcelain=v1", "-z", "-uall"]).stdout,
            "index": _git(fx.work, "ls-files", "-s"),
            "files": {p.name: p.read_text() for p in Path(fx.work).iterdir() if p.is_file()},
            "merge_head": (Path(fx.work) / ".git" / "MERGE_HEAD").exists(),
        }
    before = snap()
    for args in (["--drain", "--no-fetch"], ["--drain", "--json", "--no-fetch"]):
        r = fx.consume(*args)
        assert r.returncode == 0, r.stderr
    assert snap() == before
    assert before["merge_head"] is False


def test_json_plan_parses_and_carries_the_summary_and_pin(fx):
    fx.tip("sid-x", {"docs/x.md": "x\n"})
    plan = fx.plan()
    assert set(plan) >= {"head", "base", "pin", "tips", "held", "unreadable", "retire_candidates", "summary"}
    assert plan["summary"] == {"MERGE": 1, "MERGE-VERIFY-FIRST": 0, "CARRY": 0, "STOP": 0, "tips": 1,
                               "held": 0, "unreadable": 0, "retire_candidates": 0}
    assert plan["head_moved"] is False


def test_empty_fleet_prints_a_plan_that_says_no_tip_not_drained(fx):
    r = fx.consume("--drain")
    assert r.returncode == 0, r.stderr
    assert "no outstanding TIP to plan" in r.stdout and "drain plan: 0 tip(s)" in r.stdout
    assert "drained" not in r.stdout.lower()


def test_helper_refuses_a_report_that_does_not_parse(fx):
    for bad in ("", "not json", "[]", '{"refs": 3}'):
        r = _run([sys.executable, str(DRAIN_PY), "plan", "--repo", str(fx.work), "--refs-json", "-",
                  "--audit-py", str(AUDIT_PY)], stdin=bad)
        assert r.returncode == 2 and "REFUSED" in r.stderr and "nothing was planned" in r.stderr, bad


def test_drain_outside_a_git_repo_is_refused(tmp_path):
    r = _run([BASH, CONSUME.as_posix(), "--repo", str(tmp_path), "--drain"])
    assert r.returncode == 1 and "not a git repo" in r.stdout + r.stderr


def test_help_mentions_the_drain_mode():
    r = _run([BASH, CONSUME.as_posix(), "--help"])
    assert r.returncode == 0 and "--drain" in r.stdout


# ------------------------------------------------- structural pins (no drift)

def test_helper_only_runs_read_only_git_subcommands():
    src = DRAIN_PY.read_text()
    used = set(re.findall(r'git\(\s*repo,\s*"([a-z-]+)"', src))
    allowed = {"rev-parse", "rev-list", "merge-tree", "diff", "status", "for-each-ref", "merge-base", "log",
               "commit-tree"}
    assert used == allowed, used ^ allowed
    # Every git call goes through git(), so the set above is the whole set.
    assert src.count('["git"') == 1 and src.count("subprocess.run(") == 1
    assert "shell=True" not in src and "os.system" not in src


def test_drain_branch_runs_only_the_no_fetch_json_report():
    src = CONSUME.read_text()
    m = re.search(r'^if \[ "\$DO_DRAIN" = 1 \]; then\n(.*?)^fi$', src, re.S | re.M)
    assert m, "drain branch not found"
    body = m.group(1)
    assert "--no-fetch --json" in body and "DRAIN_PY" in body
    for forbidden in ("--check", "--merge", "--retire", "--carry", "git -C", "push"):
        assert forbidden not in body, forbidden


def test_framework_predicate_matches_the_shell_instrument():
    shell = set(re.findall(r"grep -c?E '(\^\(core/[^']+)'", CONSUME.read_text()))
    assert shell == {wrd.FRAMEWORK_RE.pattern}, (shell, wrd.FRAMEWORK_RE.pattern)
