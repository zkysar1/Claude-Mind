"""Tests for carrier_merge_audit.py and the way worker-ref-consume.sh --check
prints its verdict beside the MIXED flag.

What the audit exists to answer: a carrier merge whose result differs from HEAD
by paired added and deleted lines (MIXED, +N / -M) tells the reducer nothing
about WHAT was overwritten. Every test below pins one answer the line count
cannot give, and each carries its control, because an audit that always says
CLEARS (or never does) passes any single-direction test:

  * died / invented / twins / changed each have a case that fires and a sibling
    that must not, built from the same shape;
  * the merge BASE is what separates a delivered Body edit from a stale
    overwrite, so the same inputs are run with and without it;
  * a duplicate-line collapse is not a loss (guard-6539), and a re-serialised
    record is not an edit (canonical keys);
  * aspirations are audited per GOAL, because the line holds every goal;
  * every failure prints UNMEASURED, never CLEARS.

The integration half builds tmp git repos and runs `worker-ref-consume.sh
--check`. `--check` in a tmp repo is structurally unable to stamp the shared
pull signal (the producer refuses a foreign --repo), and the carry ledger is
redirected with its seam on every invocation so no test reads production state.
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
CONSUME = SCRIPTS / "worker-ref-consume.sh"
CLI = SCRIPTS / "carrier_merge_audit.py"

import carrier_merge_audit as cma  # noqa: E402

PATH = "agents/alpha/ledger.jsonl"
EVENTS = "agents/alpha/events.jsonl"


# --------------------------------------------------------------------- helpers
def jl(*records):
    """One-record-per-line bytes. A str is written verbatim (for malformed lines)."""
    out = b""
    for r in records:
        out += (r if isinstance(r, str) else json.dumps(r)).encode("utf-8") + b"\n"
    return out


def rec(i, **kw):
    return {"id": "r%d" % i, "v": 0, **kw}


def run_audit(pre, tip, merged, base=cma.UNKNOWN):
    return cma.audit(pre, tip, merged, base, PATH)


FIVE = [rec(i) for i in range(1, 6)]


# ------------------------------------------------------------- died / invented
def test_identical_inputs_clear_with_every_count_zero():
    """The control every other test leans on: nothing happened, so CLEARS."""
    b = jl(*FIVE)
    rep = run_audit(b, b, b, b)
    assert rep["verdict"] == "CLEARS", rep
    assert rep["died"] == {"main_lost": 0, "tip_deleted": 0, "tip_only": 0}
    assert (rep["invented"], rep["twins"]) == (0, 0)
    assert rep["changed"] == {"delivered": 0, "drift": 0, "content": {}}


def test_record_both_inputs_hold_but_the_merge_dropped_is_main_lost():
    rep = run_audit(jl(*FIVE), jl(*FIVE), jl(*FIVE[:2], *FIVE[3:]), jl(*FIVE))
    assert rep["verdict"] == "NOT CLEARED"
    assert rep["died"]["main_lost"] == 1
    assert rep["samples"]["main_lost"] == ["r3"]


def test_tip_side_delete_of_an_untouched_record_is_tip_deleted_not_main_lost():
    """The base is what says main never touched r5 and the Body removed it."""
    pre = jl(*FIVE)
    tip = merged = jl(*FIVE[:4])
    rep = run_audit(pre, tip, merged, base=pre)
    assert rep["died"] == {"main_lost": 0, "tip_deleted": 1, "tip_only": 0}, rep
    assert rep["verdict"] == "NOT CLEARED" and rep["samples"]["tip_deleted"] == ["r5"]
    # CONTROL: the same bytes with no base cannot be explained, so it is main_lost.
    blind = run_audit(pre, tip, merged)
    assert blind["died"]["main_lost"] == 1 and blind["died"]["tip_deleted"] == 0, blind


def test_a_delete_main_made_and_the_merge_honoured_is_not_a_loss():
    """Main pruned r5; the Body's snapshot still holds its untouched copy. The merge
    result lacks r5 because main said so. rb-12672: the tip lacking or holding what
    main changed is not the merge losing anything."""
    base = jl(*FIVE)
    pre = jl(*FIVE[:4])
    rep = run_audit(pre, base, pre, base=base)
    assert rep["verdict"] == "CLEARS" and rep["died"] == {"main_lost": 0, "tip_deleted": 0, "tip_only": 0}, rep
    # CONTROL: without the base the same shape reads as a record that never arrived.
    blind = run_audit(pre, base, pre)
    assert blind["verdict"] == "NOT CLEARED" and blind["died"]["tip_only"] == 1, blind


def test_a_record_the_tip_added_that_never_arrives_is_tip_only():
    base = jl(*FIVE[:3])
    rep = run_audit(base, jl(*FIVE[:4]), base, base=base)
    assert rep["died"]["tip_only"] == 1 and rep["verdict"] == "NOT CLEARED", rep
    assert rep["samples"]["tip_only"] == ["r4"]


def test_invented_record_in_the_result_is_named():
    b = jl(*FIVE)
    rep = run_audit(b, b, jl(*FIVE, rec(9)), b)
    assert rep["invented"] == 1 and rep["samples"]["invented"] == ["r9"] and rep["verdict"] == "NOT CLEARED"


def test_conflict_marker_line_is_invented_and_counted_unparsed():
    b = jl(*FIVE)
    rep = run_audit(b, b, jl(*FIVE, "<<<<<<< HEAD"), b)
    assert rep["unparsed"] == {"pre": 0, "tip": 0, "merged": 1}, rep
    assert rep["invented"] == 1 and rep["verdict"] == "NOT CLEARED"
    assert rep["samples"]["invented"][0].startswith("unparsed: <<<<<<<")


def test_absent_blobs_are_empty_not_an_error():
    """A path the Body created: absent in pre and base, present in tip and merged."""
    tip = jl(*FIVE)
    rep = cma.audit(None, tip, tip, None, PATH)
    assert rep["verdict"] == "CLEARS" and rep["lines"]["pre"] == 0, rep
    # CONTROL: a merge that deletes the whole file loses every record.
    gone = cma.audit(tip, tip, None, tip, PATH)
    assert gone["verdict"] == "NOT CLEARED" and gone["died"]["main_lost"] == 5, gone


# ----------------------------------------------------------------------- twins
def test_a_record_that_survives_as_a_pair_is_a_twin():
    """rb-12184: two versions of one id survive a line-union merge as two lines."""
    base, pre, tip = jl(rec(1)), jl(rec(1)), jl(rec(1, v=1))
    rep = run_audit(pre, tip, jl(rec(1), rec(1, v=1)), base)
    assert rep["twins"] == 1 and rep["verdict"] == "NOT CLEARED", rep
    # CONTROL: the merge that picked the Body's copy and kept one line has no twin.
    one = run_audit(pre, tip, jl(rec(1, v=1)), base)
    assert one["twins"] == 0 and one["verdict"] == "CLEARS", one


def test_an_unchanged_duplicate_line_is_not_a_twin():
    dup = jl(rec(1), rec(1), rec(2))
    rep = run_audit(dup, dup, dup, dup)
    assert rep["twins"] == 0 and rep["verdict"] == "CLEARS" and rep["dup_lines"]["merged"] == 1, rep


def test_a_duplicate_line_collapse_is_not_a_loss():
    """guard-6539: a ledger with 1080 duplicate lines merged to fewer lines and the
    line diff read as data loss. By record nothing died."""
    pre = jl(rec(1), rec(1), rec(1), rec(2))
    tip = merged = jl(rec(1), rec(2))
    rep = run_audit(pre, tip, merged, tip)
    assert rep["verdict"] == "CLEARS", rep
    assert rep["dup_lines"] == {"pre": 2, "tip": 0, "merged": 0}, rep
    assert rep["lines"]["pre"] == 4 and rep["lines"]["merged"] == 2


# --------------------------------------------------------------------- changed
def test_a_body_only_edit_is_delivered_and_clears_only_with_the_base():
    base = pre = jl(*FIVE)
    tip = merged = jl(rec(1), rec(2, v=1), *FIVE[2:])
    rep = run_audit(pre, tip, merged, base)
    assert rep["changed"]["delivered"] == 1 and rep["verdict"] == "CLEARS", rep
    # CONTROL: the identical bytes with no base cannot be told from a stale overwrite.
    blind = run_audit(pre, tip, merged)
    assert blind["changed"]["content"] == {"took_tip": 1} and blind["verdict"] == "NOT CLEARED", blind


def test_both_sides_edited_and_the_body_copy_won_is_overwrote():
    base = jl(*FIVE)
    pre = jl(rec(1), rec(2, v=5), *FIVE[2:])
    tip = merged = jl(rec(1), rec(2, v=1), *FIVE[2:])
    rep = run_audit(pre, tip, merged, base)
    assert rep["changed"]["content"] == {"overwrote": 1}, rep
    assert rep["samples"]["overwrote"] == ["r2[v]"] and rep["verdict"] == "NOT CLEARED"


def test_a_main_edit_undone_by_an_untouched_body_copy_is_reverted():
    """rb-12157: the Body never touched r2, main edited it, and a clean merge put
    the OLD copy back. No conflict fires; only the record comparison sees it."""
    base = tip = merged = jl(*FIVE)
    pre = jl(rec(1), rec(2, v=5), *FIVE[2:])
    rep = run_audit(pre, tip, merged, base)
    assert rep["changed"]["content"] == {"reverted": 1}, rep
    assert rep["verdict"] == "NOT CLEARED"


def test_a_third_version_is_blended():
    base = pre = jl(*FIVE)
    tip = jl(rec(1), rec(2, v=1), *FIVE[2:])
    merged = jl(rec(1), rec(2, v=7), *FIVE[2:])
    rep = run_audit(pre, tip, merged, base)
    assert rep["changed"]["content"] == {"blended": 1} and rep["verdict"] == "NOT CLEARED", rep


def test_a_counter_only_difference_is_drift_and_does_not_block():
    """Measured on real ledger merges: every changed record differed in
    retrieval_stats alone."""
    base = jl(rec(1, retrieval_stats={"n": 1}))
    pre = jl(rec(1, retrieval_stats={"n": 2}))
    tip = merged = jl(rec(1, retrieval_stats={"n": 3}))
    rep = run_audit(pre, tip, merged, base)
    assert rep["changed"]["drift"] == 1 and rep["changed"]["content"] == {}, rep
    assert rep["verdict"] == "CLEARS"
    # CONTROL: the same overwrite with ONE other key changed is content.
    base2 = jl(rec(1, retrieval_stats={"n": 1}, status="open"))
    pre2 = jl(rec(1, retrieval_stats={"n": 2}, status="done"))
    tip2 = merged2 = jl(rec(1, retrieval_stats={"n": 3}, status="open"))
    rep2 = run_audit(pre2, tip2, merged2, base2)
    assert rep2["changed"]["drift"] == 0 and rep2["verdict"] == "NOT CLEARED", rep2
    assert rep2["samples"]["overwrote"] == ["r1[retrieval_stats,status]"]


# ---------------------------------------------------------------------- keying
def test_records_without_an_id_are_keyed_by_canonical_json():
    """A re-serialised record is the same record; an edited one is a died + invented."""
    a, b = {"ts": 1, "msg": "x"}, {"ts": 2, "msg": "y"}
    pre = jl(a, b)
    reordered = jl({"msg": "x", "ts": 1}, b)
    same = run_audit(pre, reordered, reordered, pre)
    assert same["verdict"] == "CLEARS" and same["keying"] == {"json": 2}, same
    edited = jl({"ts": 1, "msg": "x2"}, b)
    rep = run_audit(pre, edited, edited, pre)
    assert rep["died"]["tip_deleted"] == 1 and rep["verdict"] == "NOT CLEARED", rep


def test_aspirations_are_audited_per_goal_not_per_line():
    """guard-6539: the line holds every goal, so a stale rewrite of it reverts them
    all. The line-level key (asp-1) is present on both sides in every case below;
    only the goal-level unit can name the goal that moved."""
    def asp(g1, g2):
        return {"id": "asp-1", "goals": [{"id": "g-1", "status": g1}, {"id": "g-2", "status": g2}]}

    base = jl(asp("pending", "pending"))
    # The Body completed g-1; main did not touch the line. Delivered.
    ok = run_audit(base, jl(asp("completed", "pending")), jl(asp("completed", "pending")), base)
    assert ok["verdict"] == "CLEARS" and ok["changed"]["delivered"] == 1, ok
    assert ok["keying"] == {"goal": 2, "id": 1}
    # Main completed g-2 meanwhile; the merge took the Body's whole line. g-2 reverted.
    pre = jl(asp("pending", "completed"))
    tip = merged = jl(asp("completed", "pending"))
    bad = run_audit(pre, tip, merged, base)
    assert bad["verdict"] == "NOT CLEARED", bad
    assert bad["changed"]["content"] == {"reverted": 1}, bad
    assert bad["samples"]["reverted"] == ["g-2[status]"], bad
    # And the whole-line view this replaces: one line on each side, same id.
    assert bad["lines"] == {"pre": 1, "tip": 1, "merged": 1, "base": 1}


# --------------------------------------------------------------- failure modes
def test_render_is_ascii_and_leads_with_the_verdict():
    b = jl({"id": "réè", "v": 0})
    rep = run_audit(b, b, jl(), b)
    text = "\n".join(cma.render(rep))
    text.encode("ascii")
    assert text.splitlines()[0] == "audit %s: NOT CLEARED" % PATH
    assert "NOT CLEARED because:" in text


def test_unmeasured_renders_without_pretending_to_clear():
    rep = {"path": PATH, "verdict": "UNMEASURED", "reasons": ["blob over budget"]}
    assert cma.render(rep) == ["audit %s: UNMEASURED (blob over budget)" % PATH]


# ----------------------------------------------------------------------- CLI
def _run(cmd, cwd=None, env=None):
    e = os.environ.copy()
    if env:
        e.update(env)
    return subprocess.run(cmd, cwd=cwd, env=e, capture_output=True, text=True, timeout=120)


def _git(repo, *args):
    r = _run(["git", "-C", str(repo), *args])
    assert r.returncode == 0, "git %s failed: %s" % (args, r.stderr)
    return r.stdout.strip()


def _write(work, rel, text):
    p = Path(work) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _lines(*records):
    return "".join(json.dumps(r) + "\n" for r in records)


def _init(tmp_path, name="work"):
    work = tmp_path / name
    assert _run(["git", "init", "-q", "--initial-branch=main", str(work)]).returncode == 0
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    return work


def _commit(work, msg):
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", msg)
    return _git(work, "rev-parse", "HEAD")


def _cli(work, pre, tip, merged, *extra):
    return _run([sys.executable, str(CLI), "--repo", str(work), "--path", PATH,
                 "--pre", pre, "--tip", tip, "--merged", merged, *extra])


@pytest.fixture()
def divergent(tmp_path):
    """base -> main edits r1; base -> tip edits r5 and appends r6. A clean merge."""
    work = _init(tmp_path)
    _write(work, PATH, _lines(*FIVE))
    base = _commit(work, "base")
    _git(work, "checkout", "-q", "-b", "tip")
    _write(work, PATH, _lines(*FIVE[:4], rec(5, v=1), rec(6)))
    tip = _commit(work, "tip edits r5 and appends r6")
    _git(work, "checkout", "-q", "main")
    _write(work, PATH, _lines(rec(1, v=9), *FIVE[1:]))
    pre = _commit(work, "main edits r1")
    merged = _git(work, "merge-tree", pre, tip).splitlines()[0]
    return {"work": work, "base": base, "pre": pre, "tip": tip, "merged": merged}


def test_cli_reports_a_clean_three_way_merge_by_record(divergent):
    d = divergent
    r = _cli(d["work"], d["pre"], d["tip"], d["merged"])
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert out.splitlines()[0] == "audit %s: CLEARS" % PATH, out
    assert "changed 1 (delivered 1, drift 0, content 0)" in out, out
    assert "base 5 lines" in out, out


def test_cli_json_mode_round_trips_the_same_verdict(divergent):
    d = divergent
    rep = json.loads(_cli(d["work"], d["pre"], d["tip"], d["merged"], "--json").stdout)
    assert rep["verdict"] == "CLEARS" and rep["changed"]["delivered"] == 1, rep
    assert rep["base"] == "known" and rep["path"] == PATH


def test_cli_swapped_inputs_are_the_positive_control(divergent):
    """Feeding the audit a merged tree that is NOT this merge's result must fail:
    main's own commit as the 'merge result' lacks the Body's r6 and r5 edit."""
    d = divergent
    r = _cli(d["work"], d["pre"], d["tip"], d["pre"])
    assert "NOT CLEARED" in r.stdout.splitlines()[0], r.stdout
    assert "tip-only" in r.stdout, r.stdout


def test_cli_without_a_common_base_still_reports_with_an_unknown_base(tmp_path):
    work = _init(tmp_path)
    _write(work, PATH, _lines(*FIVE))
    pre = _commit(work, "main root")
    _git(work, "checkout", "-q", "--orphan", "other")
    _git(work, "rm", "-rfq", ".")
    _write(work, PATH, _lines(*FIVE, rec(6)))
    tip = _commit(work, "unrelated root")
    r = _cli(work, pre, tip, tip)
    assert r.returncode == 0, r.stderr
    assert "merge base UNKNOWN" in r.stdout, r.stdout


def test_cli_over_budget_is_unmeasured_never_clear(divergent):
    d = divergent
    r = _cli(d["work"], d["pre"], d["tip"], d["merged"], "--max-bytes", "10")
    assert r.returncode == 0, r.stderr
    first = r.stdout.splitlines()[0]
    assert first.startswith("audit %s: UNMEASURED (" % PATH) and "audit budget" in first, r.stdout
    assert "CLEARS" not in r.stdout
    # CONTROL: the same inputs under the default budget measure.
    assert "CLEARS" in _cli(d["work"], d["pre"], d["tip"], d["merged"]).stdout


def test_cli_unresolvable_revision_is_unmeasured(divergent):
    d = divergent
    r = _cli(d["work"], d["pre"], d["tip"], "0" * 40)
    assert r.returncode == 0, r.stderr
    assert "UNMEASURED" in r.stdout and "does not resolve" in r.stdout, r.stdout
    assert "CLEARS" not in r.stdout


def test_cli_a_crash_inside_the_audit_is_unmeasured(divergent, monkeypatch, capsys):
    """A bug in the audit itself must not become a verdict or a traceback."""
    d = divergent
    monkeypatch.setattr(cma, "audit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    rc = cma.main(["--repo", str(d["work"]), "--path", PATH, "--pre", d["pre"],
                   "--tip", d["tip"], "--merged", d["merged"]])
    out = capsys.readouterr().out
    assert rc == 0 and "UNMEASURED (RuntimeError: boom)" in out, out
    assert "CLEARS" not in out and "Traceback" not in out


def test_merge_tree_blob_equals_a_real_merge_under_a_custom_driver(tmp_path):
    """The audit reads the merge-tree result as the merge result. That holds only if
    merge-tree runs the repo's merge drivers; assert it for a driver that is NOT a
    plain line merge (this one keeps the Body's whole file)."""
    work = _init(tmp_path)
    _write(work, ".gitattributes", "*.jsonl merge=taketip\n")
    _git(work, "config", "merge.taketip.driver", "cp %B %A")
    _write(work, PATH, _lines(*FIVE))
    _commit(work, "base")
    _git(work, "checkout", "-q", "-b", "tip")
    _write(work, PATH, _lines(*FIVE[:4], rec(5, v=1)))
    tip = _commit(work, "tip")
    _git(work, "checkout", "-q", "main")
    _write(work, PATH, _lines(rec(1, v=9), *FIVE[1:]))
    pre = _commit(work, "main")
    tree = _git(work, "merge-tree", pre, tip).splitlines()[0]
    predicted = _git(work, "rev-parse", "%s:%s" % (tree, PATH))
    _git(work, "merge", "-q", "--no-edit", tip)
    real = _git(work, "rev-parse", "HEAD:" + PATH)
    assert predicted == real
    # And the file really is the driver's output, not the default line merge:
    assert json.loads((work / PATH).read_text().splitlines()[0])["v"] == 0


# ------------------------------------------- worker-ref-consume.sh --check wiring
def _consume(work, *args, env=None):
    e = {k: v for k, v in os.environ.items() if k != "MIND_SID"}
    e.update({"STORAGE_BACKEND": "local", "MIND_AGENT": "alpha",
              "WORKER_REF_CARRY_LEDGER": str(Path(work).parent / "carries.jsonl")})
    e.update(env or {})
    return subprocess.run([BASH, CONSUME.as_posix(), "--repo", str(work), *args],
                          env=e, capture_output=True, text=True, timeout=120)


def _ref_block(stdout, sid):
    lines, block, seen = stdout.splitlines(), [], False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("refs/workers/"):
            if seen:
                break
            seen = stripped.endswith("/" + sid)
            continue
        if seen:
            block.append(line)
    return "\n".join(block)


def _push_ref(work, base, sid, files):
    for rel, text in files.items():
        _write(work, rel, text)
    sha = _commit(work, "carrier " + sid)
    _git(work, "push", "-q", "origin", "%s:refs/workers/alpha/%s" % (sha, sid))
    _git(work, "reset", "-q", "--hard", base)
    return sha


@pytest.fixture()
def carriers(tmp_path):
    """origin + work. main holds a 5-record ledger. Sibling carriers branch from it:
    delivers (edits r2, appends r6), deletes (drops r5, appends r6), appends (r6
    only), stale (rewrites the ledger shorter). All paths are under agents/, so
    framework_files is 0 and every ref reaches the agent-store report branch.
    `delivers` also appends one record to a SECOND ledger that deletes nothing:
    the audit must name the first path and leave the second alone."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "w"
    assert _run(["git", "init", "-q", "--bare", "--initial-branch=main", str(origin)]).returncode == 0
    assert _run(["git", "clone", "-q", str(origin), str(work)]).returncode == 0
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    _git(work, "checkout", "-q", "-b", "main")
    _write(work, PATH, _lines(*FIVE))
    _write(work, EVENTS, _lines({"id": "e1"}, {"id": "e2"}))
    base = _commit(work, "base")
    _git(work, "push", "-q", "origin", "main")
    _push_ref(work, base, "sid-delivers", {PATH: _lines(rec(1), rec(2, v=1), *FIVE[2:], rec(6)),
                                           EVENTS: _lines({"id": "e1"}, {"id": "e2"}, {"id": "e3"})})
    _push_ref(work, base, "sid-deletes", {PATH: _lines(*FIVE[:4], rec(6))})
    _push_ref(work, base, "sid-appends", {PATH: _lines(*FIVE, rec(6))})
    _push_ref(work, base, "sid-stale", {PATH: _lines(*FIVE[:2])})
    return {"work": work, "base": base, "tmp": tmp_path}


def test_check_prints_a_clearing_verdict_beside_the_mixed_flag(carriers):
    out = _consume(carriers["work"], "--check").stdout
    block = _ref_block(out, "sid-delivers")
    assert "MIXED: +3 / -1" in block, block
    lines = block.splitlines()
    mixed = next(i for i, l in enumerate(lines) if "MIXED:" in l)
    verdict = next(i for i, l in enumerate(lines) if l.strip() == "audit %s: CLEARS" % PATH)
    assert verdict > mixed, "the verdict must come AFTER the flag it explains\n" + block
    assert "changed 1 (delivered 1, drift 0, content 0)" in block, block
    # The second ledger only appends, so it cannot have lost or altered a record.
    assert "audit %s" % EVENTS not in block, block


def test_check_names_the_records_when_the_merge_does_not_clear(carriers):
    block = _ref_block(_consume(carriers["work"], "--check").stdout, "sid-deletes")
    assert "MIXED: +1 / -1" in block, block
    assert "audit %s: NOT CLEARED" % PATH in block, block
    assert "died, tip-deleted" in block and "r5" in block, block
    assert "CLEARS" not in block.replace("NOT CLEARED", ""), block


def test_audit_is_scoped_to_mixed_refs_only(carriers):
    """The positive control for the wiring: the appending and the deletion-only refs
    run the same code path and must print NO audit line."""
    out = _consume(carriers["work"], "--check").stdout
    appends = _ref_block(out, "sid-appends")
    assert "append-only (+1 / -0): safe shape" in appends and "audit " not in appends, appends
    stale = _ref_block(out, "sid-stale")
    assert "DELETION-ONLY" in stale and "audit " not in stale, stale


def test_json_output_is_untouched_by_the_audit(carriers):
    r = _consume(carriers["work"], "--json")
    data = json.loads(r.stdout)
    assert {x["ref"].rsplit("/", 1)[-1] for x in data["refs"]} >= {"sid-delivers", "sid-deletes"}
    assert "audit " not in r.stdout


def test_a_failing_helper_prints_unmeasured_never_a_clear(carriers, tmp_path):
    fake = tmp_path / "boom.py"
    fake.write_text("import sys\nsys.exit(3)\n")
    out = _consume(carriers["work"], "--check", env={"WORKER_REF_AUDIT_PY": str(fake)}).stdout
    block = _ref_block(out, "sid-delivers")
    assert "audit %s: UNMEASURED (carrier_merge_audit.py exited non-zero)" % PATH in block, block
    assert "CLEARS" not in block, block


def test_a_helper_that_prints_nothing_is_not_read_as_a_clear(carriers, tmp_path):
    fake = tmp_path / "silent.py"
    fake.write_text("import sys\nsys.exit(0)\n")
    block = _ref_block(_consume(carriers["work"], "--check", env={"WORKER_REF_AUDIT_PY": str(fake)}).stdout,
                       "sid-delivers")
    assert "MIXED: +3 / -1" in block and "CLEARS" not in block, block


def test_a_mixed_ref_with_no_jsonl_deletion_says_there_is_nothing_to_measure(tmp_path):
    origin, work = tmp_path / "origin.git", tmp_path / "w"
    assert _run(["git", "init", "-q", "--bare", "--initial-branch=main", str(origin)]).returncode == 0
    assert _run(["git", "clone", "-q", str(origin), str(work)]).returncode == 0
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    _git(work, "checkout", "-q", "-b", "main")
    _write(work, "agents/alpha/notes.txt", "one\ntwo\nthree\n")
    base = _commit(work, "base")
    _git(work, "push", "-q", "origin", "main")
    _push_ref(work, base, "sid-notes", {"agents/alpha/notes.txt": "one\nTWO\nthree\nfour\n"})
    block = _ref_block(_consume(work, "--check").stdout, "sid-notes")
    assert "MIXED:" in block, block
    assert "no *.jsonl path in this merge deletes a line" in block, block
    assert "audit agents" not in block, block


def test_only_the_first_six_deleting_paths_are_audited_and_the_rest_are_counted(tmp_path):
    origin, work = tmp_path / "origin.git", tmp_path / "w"
    assert _run(["git", "init", "-q", "--bare", "--initial-branch=main", str(origin)]).returncode == 0
    assert _run(["git", "clone", "-q", str(origin), str(work)]).returncode == 0
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    _git(work, "checkout", "-q", "-b", "main")
    paths = ["agents/alpha/l%d.jsonl" % i for i in range(8)]
    for p in paths:
        _write(work, p, _lines(*FIVE))
    base = _commit(work, "base")
    _git(work, "push", "-q", "origin", "main")
    _push_ref(work, base, "sid-many", {p: _lines(*FIVE[:4], rec(6)) for p in paths})
    block = _ref_block(_consume(work, "--check").stdout, "sid-many")
    assert len(re.findall(r"^\s+audit agents/alpha/l\d\.jsonl: ", block, re.M)) == 6, block
    assert "(2 more *.jsonl path(s) that delete lines were NOT audited: cap 6 per ref)" in block, block


def test_audit_seam_default_is_the_real_sibling():
    src = CONSUME.read_text(encoding="utf-8")
    assert 'AUDIT_PY="${WORKER_REF_AUDIT_PY:-$SCRIPT_DIR/carrier_merge_audit.py}"' in src
    assert CLI.is_file()


def test_the_audit_call_sits_inside_the_mixed_branch_only_in_source():
    """Source pin: the call belongs directly after the MIXED line. Moving it out of
    that branch would audit append-only and deletion-only refs too (the scoped-wiring
    test above catches the behaviour; this names the line that has to stay put)."""
    src = CONSUME.read_text(encoding="utf-8")
    i = src.index('echo "      ⚠ MIXED:')
    j = src.index('merge_audit_paths "$tip_full" "$_mt"')
    k = src.index('elif [ "$mt_add" -gt 0 ] 2>/dev/null && [ "$mt_del" = 0 ]; then', i)
    assert i < j < k, (i, j, k)
    assert src.count('merge_audit_paths "$tip_full" "$_mt"') == 1
