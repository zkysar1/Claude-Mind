"""Tests for worker-ref-consume.sh (): ancestor collapse, --check
thresholds, --retire receipt discipline, and the wired-caller regression pin.

The wired-caller pin is the load-bearing test: the script shipped 2026-08-06
(g-306-264) with ZERO executable call sites and sat invisible for a week while
worker refs accumulated 200+ stranded commits (g-115-5945). A consumer whose
caller regresses away reverts to exactly that state, silently — so the pin
greps iteration-close.sh for a NON-comment invocation (guard-1099: an
unanchored grep counts comments quoting the call as live code).

All git fixtures are self-contained tmp repos; no network, no live daemon, no
world store. STORAGE_BACKEND is irrelevant here but the suite-wide local pin
is harmless.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_helpers import BASH  # noqa: E402  (guard-580: never bare "bash" argv)

SCRIPTS = Path(__file__).resolve().parents[1]
CONSUME = SCRIPTS / "worker-ref-consume.sh"
ITERATION_CLOSE = SCRIPTS / "iteration-close.sh"


def _run(cmd, cwd=None, env=None):
    e = os.environ.copy()
    if env:
        e.update(env)
    return subprocess.run(
        cmd, cwd=cwd, env=e, capture_output=True, text=True, timeout=120
    )


def _git(repo, *args):
    r = _run(["git", "-C", str(repo), *args])
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r.stdout.strip()


def _consume(repo, *args, env=None):
    return _run([BASH, CONSUME.as_posix(), "--repo", str(repo), *args], env=env)


@pytest.fixture()
def repo(tmp_path):
    """origin (bare) + work clone with: main@base on origin, worker ref A
    (1 commit past base), worker ref B (A's commit + 1 more, touching a
    framework path) — A is a strict ancestor of B. Work HEAD stays at base,
    so both refs carry commits HEAD lacks."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    r = _run(["git", "init", "--bare", "--initial-branch=main", str(origin)])
    assert r.returncode == 0, r.stderr
    r = _run(["git", "clone", str(origin), str(work)])
    assert r.returncode == 0, r.stderr
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    _git(work, "checkout", "-b", "main")
    (work / "f.txt").write_text("base\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "base")
    _git(work, "push", "origin", "main")

    # ref A: one commit past base
    (work / "a.txt").write_text("a\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "worker A commit")
    sha_a = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", f"{sha_a}:refs/workers/alpha/sid-aaaa")

    # ref B: A + a framework-path commit (strict superset of A)
    (work / "core").mkdir(exist_ok=True)
    (work / "core" / "x.sh").write_text("echo x\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "worker B framework commit")
    sha_b = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", f"{sha_b}:refs/workers/alpha/sid-bbbb")

    # park HEAD back at base so both refs are ahead of it
    _git(work, "reset", "--hard", "HEAD~2")
    return {"origin": origin, "work": work, "sha_a": sha_a, "sha_b": sha_b}


def _refs_by_sid(json_out):
    data = json.loads(json_out)
    return {r["sid"]: r for r in data["refs"]}, data


def test_report_marks_ancestor_superseded_and_counts_tips_only(repo):
    r = _consume(repo["work"], "--json")
    assert r.returncode == 0, r.stderr
    by_sid, data = _refs_by_sid(r.stdout)
    a, b = by_sid["sid-aaaa"], by_sid["sid-bbbb"]
    # A is contained in B: tagged, and not counted outstanding
    assert a["superseded_by"].endswith("sid-bbbb"), a
    assert b["superseded_by"] == "", b
    assert a["commits_ahead"] == 1 and b["commits_ahead"] == 2
    assert data["outstanding"] == 1, (
        "outstanding must count TIPS only — an ancestor's commits are "
        "contained in its tip (g-115-5945 N1: enumerating ancestors inflated "
        "the review burden 3x)"
    )


def test_json_carries_stranding_age_field(repo):
    r = _consume(repo["work"], "--json")
    assert r.returncode == 0, r.stderr
    by_sid, _ = _refs_by_sid(r.stdout)
    # Fresh fixture commits: present and integer, ~0 — the field existing is
    # the contract; its growth over time is what --check thresholds read.
    assert by_sid["sid-bbbb"]["oldest_unlanded_age_h"] == 0


def test_self_ref_is_excluded_from_outstanding(repo):
    r = _consume(repo["work"], "--json", env={"MIND_SID": "sid-bbbb"})
    assert r.returncode == 0, r.stderr
    by_sid, data = _refs_by_sid(r.stdout)
    assert by_sid["sid-bbbb"]["is_self"] == 1
    assert data["outstanding"] == 0, (
        "a body's own ref is not consumable work for it; with the tip self, "
        "the superseded ancestor must not resurface as outstanding"
    )


def test_check_banner_fires_past_threshold_and_reports_both_axes_capable(repo):
    r = _consume(repo["work"], "--check", "--max-depth", "0", "--max-age-h", "9999")
    assert r.returncode == 0, "advisory mode must exit 0 even on breach"
    assert "STRANDED WORKER WORK" in r.stdout
    assert "sid-bbbb" in r.stdout and "depth=2" in r.stdout
    # ancestor ref must NOT breach independently (tips only)
    assert "sid-aaaa: depth" not in r.stdout


def test_check_quiet_under_threshold(repo):
    r = _consume(repo["work"], "--check", "--max-depth", "100", "--max-age-h", "9999")
    assert r.returncode == 0
    assert "STRANDED WORKER WORK" not in r.stdout


def test_retire_refuses_ref_not_reachable_from_origin_main(repo):
    r = _consume(repo["work"], "--retire", "refs/workers/alpha/sid-bbbb")
    assert r.returncode == 1
    assert "REFUSED" in r.stderr
    # the ref must survive the refusal, on origin and locally
    ls = _git(repo["work"], "ls-remote", "origin", "refs/workers/*")
    assert "sid-bbbb" in ls
    receipts = repo["work"] / "core" / "logs" / "worker-ref-retirements.jsonl"
    assert not receipts.exists(), "a refused retire must not write a receipt"


# Default: agent `alpha` present (every fixture ref is refs/workers/alpha/...)
# and carrying the in_flight_bodies key, so the schema reads INTACT and an
# absent child row is a GENUINE absence. Tests that want drift override it.
INTACT_STATUS = '{"alpha": {"in_flight_bodies": {}}}'


def _stub_reader(tmp_path, payload, rc=0, status_payload=INTACT_STATUS, name=None):
    """Hermetic stand-in for team-state-read.sh (WORKER_REF_TEAM_STATE_READER
    seam, g-306-286). Emits `payload` on stdout and exits `rc` — so the retire
    liveness gate's parse/decide path runs UNCHANGED; only the JSON's emitter
    differs. test_liveness_seam_default_is_the_real_sibling pins that
    production resolves to the real sibling, not this stub.

    FIELD-AWARE since g-306-339. The gate now asks TWO different questions of
    the reader: the child row (agent_status.<agent>.in_flight_bodies.<sid>) and
    the schema probe (agent_status). A stub that answered both with one payload
    could not model the real reader, which returns rc=0 "null" for a path that
    does not exist — the exact ambiguity g-306-339 fixes. `status_payload`
    answers the schema probe; `payload` answers everything else."""
    stub = tmp_path / (name or "stub-team-state-read.sh")
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'field=""\n'
        'while [ $# -gt 0 ]; do\n'
        '  case "$1" in --field) field="${2:-}"; shift 2;; *) shift;; esac\n'
        "done\n"
        'if [ "$field" = "agent_status" ]; then\n'
        "  printf '%s' '" + status_payload + "'\n"
        "  exit 0\n"
        "fi\n"
        "printf '%s' '" + payload + "'\n"
        "exit " + str(rc) + "\n"
    )
    return str(stub)


def _merge_and_push_a(repo):
    """Make ref A retire-eligible on the REACHABILITY axis: merge into main and
    push main to origin. Merge by SHA: the clone has A's commit locally (it
    authored it) but acquires the refs/workers/* NAME only via the consume
    script's own fetch, which these tests deliberately haven't run yet."""
    work = repo["work"]
    _git(work, "merge", "--no-edit", repo["sha_a"])
    _git(work, "push", "origin", "main")
    return work


LIVE_ROW = '{"goal_id": "g-999-9", "claimed_at": "2026-08-13T10:00:00"}'


def test_retire_deletes_merged_ref_and_writes_receipt(repo, tmp_path):
    # consume ref A properly (remote durability), with NO body row for its sid
    # (reader returns null) — the no-regression case the  ancestors
    # exercised: absent row must not block a legitimate retire.
    work = _merge_and_push_a(repo)
    r = _consume(work, "--retire", "refs/workers/alpha/sid-aaaa",
                 env={"WORKER_REF_TEAM_STATE_READER": _stub_reader(tmp_path, "null")})
    assert r.returncode == 0, r.stderr + r.stdout
    ls = _git(work, "ls-remote", "origin", "refs/workers/*")
    assert "sid-aaaa" not in ls, "retired ref must be deleted from origin"
    assert "sid-bbbb" in ls, "unconsumed sibling ref must be untouched"
    receipts = work / "core" / "logs" / "worker-ref-retirements.jsonl"
    assert receipts.exists()
    rec = json.loads(receipts.read_text().strip().splitlines()[-1])
    assert rec["ref"] == "refs/workers/alpha/sid-aaaa"
    assert rec["tip_sha"] == repo["sha_a"], (
        "receipt must carry the tip SHA (rb-7598 discipline) — it is the "
        "recreate handle: git push origin <tip>:<ref>"
    )
    assert rec["recreate_with"].startswith("git push origin ")
    assert rec["body_row"] == "absent"
    assert "liveness_override" not in rec, (
        "no override was used — the receipt must not carry the field"
    )


def test_retire_refuses_when_live_body_row_names_ref(repo, tmp_path):
    """ outcome 1: reachable ref + live in_flight_bodies row -> REFUSE,
    naming the claiming goal and its age. This is the exact g-306-283 near-miss:
    both tips were merged (reachable) while their bodies were mid-goal."""
    work = _merge_and_push_a(repo)
    r = _consume(work, "--retire", "refs/workers/alpha/sid-aaaa",
                 env={"WORKER_REF_TEAM_STATE_READER": _stub_reader(tmp_path, LIVE_ROW)})
    assert r.returncode == 1, "a live body row must refuse the retire"
    assert "live in_flight_bodies row" in r.stderr
    assert "g-999-9" in r.stderr, "diagnostic must name the claiming goal"
    assert "2026-08-13T10:00:00" in r.stderr, "diagnostic must name the claim time"
    ls = _git(work, "ls-remote", "origin", "refs/workers/*")
    assert "sid-aaaa" in ls, "the ref must survive the refusal"
    receipts = work / "core" / "logs" / "worker-ref-retirements.jsonl"
    assert not receipts.exists(), "a refused retire must not write a receipt"


def test_retire_fail_closed_on_unreadable_liveness_source(repo, tmp_path):
    """ outcome 3: unreadable in_flight source -> REFUSAL, not
    permission. Keeping a ref costs nothing; deleting a live one is an
    unrecoverable handle loss."""
    work = _merge_and_push_a(repo)
    r = _consume(work, "--retire", "refs/workers/alpha/sid-aaaa",
                 env={"WORKER_REF_TEAM_STATE_READER": _stub_reader(tmp_path, "", rc=1)})
    assert r.returncode == 1, "an unreadable liveness source must refuse"
    assert "Fail-closed" in r.stderr
    ls = _git(work, "ls-remote", "origin", "refs/workers/*")
    assert "sid-aaaa" in ls, "the ref must survive the refusal"
    receipts = work / "core" / "logs" / "worker-ref-retirements.jsonl"
    assert not receipts.exists()


def test_force_retire_live_overrides_and_logs_to_receipt(repo, tmp_path):
    """The operator-knows-it-is-dead override retires past a live row AND the
    receipt records both the overridden row and the justification verbatim."""
    work = _merge_and_push_a(repo)
    r = _consume(work, "--retire", "refs/workers/alpha/sid-aaaa",
                 "--force-retire-live", "body killed manually during incident drill",
                 env={"WORKER_REF_TEAM_STATE_READER": _stub_reader(tmp_path, LIVE_ROW)})
    assert r.returncode == 0, r.stderr + r.stdout
    ls = _git(work, "ls-remote", "origin", "refs/workers/*")
    assert "sid-aaaa" not in ls
    receipts = work / "core" / "logs" / "worker-ref-retirements.jsonl"
    rec = json.loads(receipts.read_text().strip().splitlines()[-1])
    assert rec["liveness_override"] == "body killed manually during incident drill"
    assert rec["body_row"].startswith("LIVE-OVERRIDDEN goal=g-999-9")


# --- : "null" means BOTH "no live row" and "that path does not exist" ---
# team-state-read.sh returns rc=0 / stdout "null" / EMPTY stderr for a path that
# does not exist (measured on four controls, incl. a positive control proving the
# reader itself works). So the gate's fail-closed branch — written for an
# "unreadable liveness source" — could not fire on the failure mode that actually
# occurs, and path drift fell through to RETIRE. These four pin the schema probe
# that disambiguates. guard-3660 is the constraint they must not violate: an
# ABSENT row is a LEGITIMATE retire, so only UNTRUSTWORTHY absence may refuse.

def test_retire_fails_closed_when_agent_status_itself_drifts(repo, tmp_path):
    """Top-level key moved/renamed -> schema probe reads "null" -> REFUSE."""
    work = _merge_and_push_a(repo)
    r = _consume(work, "--retire", "refs/workers/alpha/sid-aaaa",
                 env={"WORKER_REF_TEAM_STATE_READER": _stub_reader(
                     tmp_path, "null", status_payload="null")})
    assert r.returncode == 1, "drifted schema must REFUSE, not retire"
    assert "sid-aaaa" in _git(work, "ls-remote", "origin", "refs/workers/*"), (
        "the ref must survive a refusal — deleting it is the unrecoverable direction"
    )


def test_retire_fails_closed_when_ref_agent_absent_from_team_state(repo, tmp_path):
    """The ref's agent segment is derived by string-munging the ref path. If it
    parses wrong (or names an agent team-state never heard of), the child query
    returns "null" for a nonexistent path and the old gate retired anyway."""
    work = _merge_and_push_a(repo)
    r = _consume(work, "--retire", "refs/workers/alpha/sid-aaaa",
                 env={"WORKER_REF_TEAM_STATE_READER": _stub_reader(
                     tmp_path, "null",
                     status_payload='{"bravo": {"in_flight_bodies": {}}}')})
    assert r.returncode == 1, "unknown ref agent must REFUSE"
    assert "sid-aaaa" in _git(work, "ls-remote", "origin", "refs/workers/*")


def test_retire_fails_closed_when_in_flight_bodies_key_gone_fleet_wide(repo, tmp_path):
    """The in_flight_bodies key renamed: every agent row exists, none carries the
    key. Indistinguishable from a fleet with zero live bodies, so this refuses in
    BOTH cases — the safe direction, with --force-retire-live as the escape."""
    work = _merge_and_push_a(repo)
    r = _consume(work, "--retire", "refs/workers/alpha/sid-aaaa",
                 env={"WORKER_REF_TEAM_STATE_READER": _stub_reader(
                     tmp_path, "null", status_payload='{"alpha": {}, "bravo": {}}')})
    assert r.returncode == 1, "fleet-wide missing key must REFUSE"
    assert "sid-aaaa" in _git(work, "ls-remote", "origin", "refs/workers/*")


def test_retire_proceeds_for_an_agent_that_simply_has_no_live_bodies(repo, tmp_path):
    """THE FALSE-REFUSE PIN, and the reason this fix does NOT probe the parent
    path the filing goal proposed. in_flight_bodies is created LAZILY on first
    claim: measured 2026-08-21, alpha/bravo carried it while echo/foxtrot/zeta
    existed in agent_status WITHOUT it. Probing agent_status.<agent>.in_flight_bodies
    would therefore read "null" for three real agents and refuse every ordinary
    retire for them — turning a guard into something operators route around with
    --force-retire-live. The probe sits one level up for exactly this reason, and
    guard-3660 requires it: an absent row is a legitimate retire."""
    work = _merge_and_push_a(repo)
    r = _consume(work, "--retire", "refs/workers/alpha/sid-aaaa",
                 env={"WORKER_REF_TEAM_STATE_READER": _stub_reader(
                     tmp_path, "null",
                     status_payload='{"alpha": {}, "bravo": {"in_flight_bodies": {}}}')})
    assert r.returncode == 0, (r.stderr + r.stdout)
    assert "sid-aaaa" not in _git(work, "ls-remote", "origin", "refs/workers/*")
    rec = json.loads((work / "core" / "logs" / "worker-ref-retirements.jsonl")
                     .read_text().strip().splitlines()[-1])
    assert rec["body_row"] == "absent"


def test_schema_drift_refusal_is_overridable(repo, tmp_path):
    """A refusal an operator cannot get past becomes a reason to stop using the
    tool. Same escape hatch the live-row branch already offers, recorded in the
    receipt so the override is auditable."""
    work = _merge_and_push_a(repo)
    r = _consume(work, "--retire", "refs/workers/alpha/sid-aaaa",
                 "--force-retire-live", "team-state schema migration in flight",
                 env={"WORKER_REF_TEAM_STATE_READER": _stub_reader(
                     tmp_path, "null", status_payload="null")})
    assert r.returncode == 0, (r.stderr + r.stdout)
    rec = json.loads((work / "core" / "logs" / "worker-ref-retirements.jsonl")
                     .read_text().strip().splitlines()[-1])
    assert rec["liveness_override"] == "team-state schema migration in flight"
    assert "DRIFT" in rec["body_row"].upper(), rec["body_row"]


def test_liveness_seam_default_is_the_real_sibling():
    """guard-920 mitigation for the WORKER_REF_TEAM_STATE_READER test seam: the
    production default must resolve to the REAL team-state-read.sh sibling, and
    that sibling must exist. Without this pin the stub-driven tests above could
    stay green while production reads through a renamed/moved script."""
    src = CONSUME.read_text(encoding="utf-8", errors="replace")
    assert 'TEAM_STATE_READER="${WORKER_REF_TEAM_STATE_READER:-$SCRIPT_DIR/team-state-read.sh}"' in src
    assert (SCRIPTS / "team-state-read.sh").exists()


def test_zero_refs_is_exit_zero_not_all_clear(tmp_path):
    origin = tmp_path / "o.git"
    work = tmp_path / "w"
    _run(["git", "init", "--bare", "--initial-branch=main", str(origin)])
    _run(["git", "clone", str(origin), str(work)])
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    _git(work, "checkout", "-b", "main")
    (work / "f.txt").write_text("x\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "base")
    _git(work, "push", "origin", "main")
    r = _consume(work)
    assert r.returncode == 0
    assert "normal state, not a verified-empty one" in r.stdout


def test_iteration_close_has_executable_caller():
    """Regression pin for the no-caller defect itself (hypothesis
    2026-08-08_worker-ref-consumer-landed-but-has-no-caller: the script landed
    with zero executable call sites and the queue went invisible). Anchor to
    NON-comment lines — a comment quoting the invocation must not count."""
    src = ITERATION_CLOSE.read_text(encoding="utf-8", errors="replace")
    live = [
        ln
        for ln in src.splitlines()
        if "worker-ref-consume.sh" in ln
        and "--check" in ln
        and not ln.lstrip().startswith("#")
    ]
    assert live, (
        "iteration-close.sh no longer invokes worker-ref-consume.sh --check "
        "on an executable line — the consumer has lost its wired caller and "
        "worker carrier refs are invisible again (g-306-283)"
    )


def _plant_unreadable_ref(work, sid="sid-bogus"):
    """Loose ref with a well-formed sha that exists in no object db — the
    'unfetched sid' shape g-306-287 F-002 names. Verified empirically:
    for-each-ref LISTS it (so it enters REF_LIST) and rev-list fails rc=128.
    Callers pair this with --no-fetch: the default fetch is --prune against
    origin, which deletes a local-only ref before enumeration ever sees it."""
    d = Path(work) / ".git" / "refs" / "workers" / "alpha"
    d.mkdir(parents=True, exist_ok=True)
    (d / sid).write_text("1111111111222222222233333333334444444444\n")


def test_unreadable_ref_lands_in_its_own_bucket_not_healthy_zero(repo):
    """F-002 (): a rev-list failure must NOT read as the healthy
    'fully contained' 0. Pre-fix, the error value WAS 0, so a ref with a bad
    object / corrupt ref / unfetched sid reported as fully consumed and
    silently dropped out of `outstanding` — a visibility instrument failing
    toward silence."""
    # Prime: a default-fetch run materializes sid-aaaa/sid-bbbb locally (pushed
    # worker refs live only on origin until the script's own fetch mirrors
    # them). THEN plant the broken ref and assert on a --no-fetch run, so the
    # --prune fetch cannot delete the local-only plant before enumeration.
    prime = _consume(repo["work"], "--json")
    assert prime.returncode == 0, prime.stderr
    _plant_unreadable_ref(repo["work"])
    r = _consume(repo["work"], "--json", "--no-fetch")
    assert r.returncode == 0, r.stderr
    by_sid, data = _refs_by_sid(r.stdout)
    bogus = by_sid["sid-bogus"]
    assert bogus["unreadable"] == 1, bogus
    assert bogus["commits_ahead"] == 0
    # the readable refs keep their contract untouched
    assert by_sid["sid-bbbb"]["unreadable"] == 0
    assert data["unreadable"] == 1, data
    assert data["outstanding"] == 1, (
        "an unreadable ref must be excluded from outstanding (its count is "
        "unknowable) without disturbing the readable tips' count"
    )


def test_unreadable_ref_fires_the_check_banner(repo):
    """The unreadable bucket must reach the --check banner: an unreadable ref
    is itself a visibility failure, and the banner is how iteration-close
    surfaces this instrument's findings between disposition runs."""
    _plant_unreadable_ref(repo["work"])
    r = _consume(repo["work"], "--check", "--no-fetch")
    assert r.returncode == 0, r.stderr
    assert "unreadable" in r.stdout, (
        "an unreadable ref produced no banner line — the error bucket is "
        "invisible exactly where g-306-287 F-002 required it visible"
    )


def test_iteration_close_failure_branch_emits_gate_log_row():
    """F-001 (): the cadence marker is touched BEFORE the check runs
    (deliberate thrash protection), so a failed check has already advanced its
    own cadence — the failure needs a DURABLE record, not a stderr WARN that
    guard-772 shows is invisible on backgrounded runs. Pin the gate-log
    emission on an executable line, same anchoring discipline as the
    wired-caller pin above (guard-1099: comments quoting the call must not
    count)."""
    src = ITERATION_CLOSE.read_text(encoding="utf-8", errors="replace")
    live = [
        ln
        for ln in src.splitlines()
        if "gate-log.sh" in ln
        and "worker-ref-report-check" in ln
        and not ln.lstrip().startswith("#")
    ]
    assert live, (
        "iteration-close.sh no longer emits the worker-ref-report-check "
        "fail_open gate-log row on a --check failure — the dark visibility "
        "path g-306-287 F-001 measured is un-instrumented again"
    )
    # and the registration that makes those rows visible to the evaluator
    gates = (SCRIPTS.parent / "config" / "gates.yaml").read_text(
        encoding="utf-8", errors="replace")
    assert "worker-ref-report-check" in gates, (
        "gate id not registered in gates.yaml — _gate_log.log docstring: an "
        "unregistered gate_id is invisible to gate-retirement-eval"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


# ---------------------------------------------------------------------------
# Content-not-commits ( drain, 2026-08-17). A live body syncs
# origin/main into its branch with plain merge commits; once main has consumed
# the body's real work those sync merges are the only commits main lacks and
# they carry nothing — yet the plain rev-list count reported them as
# commits_ahead>0 and the three-dot diff listed the framework files they had
# pulled FROM main, so the ref read as an outstanding TIP forever (measured on
# the live refs: commits_ahead=2 / framework_files=41 with zero unlanded
# content). A merge counts only when `git show --remerge-diff` shows it added
# hunks of its own (a conflict resolution / evil merge).
# ---------------------------------------------------------------------------

def _advance_main(work, text="main\n"):
    """One commit on main (f.txt) pushed to origin; HEAD stays on main."""
    _git(work, "checkout", "main")
    (work / "f.txt").write_text(text)
    _git(work, "add", ".")
    _git(work, "commit", "-m", "main advance")
    _git(work, "push", "origin", "main")
    return _git(work, "rev-parse", "HEAD")


def test_content_free_sync_merge_reads_contained_not_outstanding(repo):
    work = repo["work"]
    base = _git(work, "rev-parse", "HEAD")
    _advance_main(work)
    # A worker branch with NO commits of its own syncs main in with --no-ff:
    # the merge commit is the only commit main lacks, and it carries nothing.
    _git(work, "checkout", "-b", "wsync", base)
    _git(work, "merge", "--no-ff", "--no-edit", "main")
    sha = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", f"{sha}:refs/workers/alpha/sid-sync")
    _git(work, "checkout", "main")
    r = _consume(work, "--json")
    assert r.returncode == 0, r.stderr
    by_sid, data = _refs_by_sid(r.stdout)
    s = by_sid["sid-sync"]
    assert s["commits_ahead"] == 0, s
    assert s["sync_merges"] == 1, s
    assert s["framework_files"] == 0, s
    # sid-aaaa / sid-bbbb still carry real commits main lacks: bbbb is the one tip.
    assert by_sid["sid-bbbb"]["commits_ahead"] == 2
    assert data["outstanding"] == 1, (
        "a ref whose only unlanded commits are content-free sync merges is "
        "CONTAINED — it must not be counted as an outstanding tip")


def test_content_bearing_merge_still_counts_as_unlanded_work(repo):
    """Positive control for the discriminator: a merge that ADDS hunks beyond
    the automatic result (a hand resolution / evil merge) is real unlanded work
    and must keep commits_ahead > 0 even though it is a merge commit."""
    work = repo["work"]
    base = _git(work, "rev-parse", "HEAD")
    _advance_main(work)
    _git(work, "checkout", "-b", "wevil", base)
    r = _run(["git", "-C", str(work), "merge", "--no-ff", "--no-commit", "main"])
    assert r.returncode == 0, r.stderr
    (work / "f.txt").write_text("worker tweak inside the merge\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "merge main (with a resolution of its own)")
    sha = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", f"{sha}:refs/workers/alpha/sid-evil")
    _git(work, "checkout", "main")
    r = _consume(work, "--json")
    assert r.returncode == 0, r.stderr
    by_sid, data = _refs_by_sid(r.stdout)
    e = by_sid["sid-evil"]
    assert e["commits_ahead"] == 1, e
    assert e["sync_merges"] == 0, e
    assert data["outstanding"] == 2, "sid-evil and sid-bbbb are both real tips"


def test_content_not_commits_source_pin():
    src = CONSUME.read_text(encoding="utf-8")
    assert "rev-list --count --no-merges" in src
    assert "show --remerge-diff --format=" in src
    assert '"sync_merges":%s' in src


# ---------------------------------------------------------------------------
# : pin the TWO reachability-refusal diagnostics added by .
#
# WHY THIS EXISTS.  split the "not reachable from origin/main" refusal
# into two different remedies, and the whole suite passed 24/24 both BEFORE and
# AFTER that change — grep showed CARRY=0, rebase=0, orphan=0 in this file. So
# the split could have been reverted wholesale with the suite still green: the
# "fix can be deleted and nothing goes red" class.
#
# The refusal ITSELF was already pinned (test_retire_refuses_ref_not_reachable_
# from_origin_main above). What was NOT pinned is WHICH REMEDY it prints, and
# that is the entire content of  — the branch exists because the two
# causes need OPPOSITE actions: a live carrier must be CARRIED (re-merging is
# unsatisfiable, retiring destroys a running body's push target), while a dead
# one must be merge-and-pushed. Printing the wrong one sends the operator
# hunting a bookkeeping bug that is not there.
#
# Both pins assert rc=1 AND ref survival AND receipt-absence, not message text
# alone, so a loosened predicate cannot pass them (goal verification outcome 2).
# Both drive the declared WORKER_REF_TEAM_STATE_READER seam, which is what makes
# the live-vs-dead distinction hermetic: sid-bbbb is deliberately NOT merged, so
# the reachability check refuses first and the branch under test is reached.
# ---------------------------------------------------------------------------

def test_unreachable_refusal_names_CARRY_when_a_live_body_row_exists(repo, tmp_path):
    """ branch 1: unreachable ref + LIVE in_flight_bodies row -> the
    remedy is CARRY, explicitly NOT merge-and-push. guard-3660: reachability is
    about the CONTENT, the body row is about the HANDLE, and only the handle
    matters while a body is running."""
    r = _consume(repo["work"], "--retire", "refs/workers/alpha/sid-bbbb",
                 env={"WORKER_REF_TEAM_STATE_READER": _stub_reader(tmp_path, LIVE_ROW)})
    assert r.returncode == 1, "an unreachable ref must still refuse"
    assert "REFUSED" in r.stderr
    assert "CARRY" in r.stderr, "a live carrier's remedy is CARRY, not merge-and-push"
    assert "guard-3660" in r.stderr, "the CARRY branch must cite guard-3660"
    assert "g-999-9" in r.stderr, "the live row's claiming goal must be named"
    # The wrong remedy must NOT also be printed — the branch is exclusive, and a
    # refusal that prints both remedies is no better than the one-line original.
    assert "Merge it and push main first" not in r.stderr, (
        "the live branch must not also emit the dead-carrier remedy"
    )
    ls = _git(repo["work"], "ls-remote", "origin", "refs/workers/*")
    assert "sid-bbbb" in ls, "the ref must survive the refusal"
    receipts = repo["work"] / "core" / "logs" / "worker-ref-retirements.jsonl"
    assert not receipts.exists(), "a refused retire must not write a receipt"


def test_unreachable_refusal_names_rebase_orphan_when_no_live_body_row(repo, tmp_path):
    """ branch 2: unreachable ref + NO live row -> merge-and-push, plus
    the rebase-orphan cause for the 'I already merged and it still refuses'
    case. guard-1863: never `git pull --rebase` this repo."""
    r = _consume(repo["work"], "--retire", "refs/workers/alpha/sid-bbbb",
                 env={"WORKER_REF_TEAM_STATE_READER": _stub_reader(tmp_path, "null")})
    assert r.returncode == 1, "an unreachable ref must still refuse"
    assert "REFUSED" in r.stderr
    assert "Merge it and push main first" in r.stderr
    assert "rebase" in r.stderr, "the dead branch must name the rebase-orphan cause"
    assert "guard-1863" in r.stderr, "the rebase-orphan hint must cite guard-1863"
    # Exclusivity, mirroring the pin above.
    assert "CARRY" not in r.stderr, (
        "with no live row the CARRY remedy must not be emitted"
    )
    ls = _git(repo["work"], "ls-remote", "origin", "refs/workers/*")
    assert "sid-bbbb" in ls, "the ref must survive the refusal"
    receipts = repo["work"] / "core" / "logs" / "worker-ref-retirements.jsonl"
    assert not receipts.exists(), "a refused retire must not write a receipt"
