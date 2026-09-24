"""Tests for worker-ref-consume.sh (): ancestor collapse, --check
thresholds, --retire receipt discipline, and the wired-caller regression pin.

The wired-caller pin is the load-bearing test: the script shipped 2026-08-06
(g-306-264) with ZERO executable call sites and sat invisible for a week while
worker refs accumulated 200+ stranded commits (g-115-5945). A consumer whose
caller regresses away reverts to exactly that state, silently — so the pin
greps iteration-close.sh for a NON-comment invocation (guard-1099: an
unanchored grep counts comments quoting the call as live code).

All git fixtures are self-contained tmp repos; no network and no live daemon.

THE "NO WORLD STORE" CLAUSE THIS DOCSTRING USED TO CARRY WAS FALSE, and its
falseness is worth keeping visible rather than quietly deleting (guard-5953,
g-115-8634). Three tests here invoke `--check`, whose DO_CHECK branch calls
pull-signal-set.sh — a SHARED WORLD-STORE writer. The tmp git repo isolated
what the FIXTURE built and said nothing about a second store the SUBJECT
reaches through its own path resolution, so `refs/workers/alpha/sid-bbbb`
(pushed at the fixture below) was found LIVE in g-306-284.pull_signal, where
its idempotent-skip-while-live contract (rb-662) then SUPPRESSED the real
signal from 3 genuine outstanding tips carrying 16 framework files.

The next sentence used to read "STORAGE_BACKEND is irrelevant here" — TRUE of
the git fixture and IRRELEVANT to the leaking channel, which is precisely why
it read as reasoned. A correct statement about the isolated resource is the
strongest camouflage for an un-isolated one.

The leak is now closed IN PRODUCTION, not by test opt-in: worker-ref-consume.sh
refuses to stamp when --repo is not the bound agent repo. Because _paths.sh
line 31 EXPORTS PROJECT_ROOT unconditionally, a test cannot inject a fake bound
repo to get around it (the guard-2484 clobber, working in our favour here), so
every tmp-repo invocation in this file is structurally incapable of writing.
"""
import json
import os
import shutil
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
    lines = [json.loads(l) for l in receipts.read_text().strip().splitlines()]
    # : a retirement now writes TWO append-only lines — the receipt
    # (outcome "attempted", written BEFORE the destructive push) and a terminal
    # outcome line. Select the receipt by its marker rather than by position;
    # this assertion block is about the receipt's recovery fields.
    rec = next(r for r in lines if r.get("outcome") == "attempted")
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
    # The terminal line is what makes the ledger readable without a second
    # source: the receipt alone can only ever say "attempted".
    term = [r for r in lines if r.get("outcome") in ("delete_succeeded", "delete_failed")]
    assert len(term) == 1, f"expected exactly one terminal outcome line, got {term}"
    assert term[0]["outcome"] == "delete_succeeded"
    assert term[0]["ref"] == "refs/workers/alpha/sid-aaaa"
    assert term[0]["tip_sha"] == repo["sha_a"], (
        "the terminal line must carry the tip SHA so it joins to its receipt"
    )


def test_retire_failed_remote_delete_marks_ledger(repo, tmp_path):
    """: when the remote delete FAILS the ledger must say so.

    Reachable, not theoretical: two agents seeing the same stale carrier both
    pass the reachability and liveness gates, the first deletes the ref, and the
    second's `git push origin :<ref>` fails 'remote ref does not exist' with its
    receipt ALREADY on disk. Before the fix that receipt was the only line, and
    it asserted a retired_at timestamp — one retirement, two completion records.

    --no-fetch is load-bearing here, not incidental: the default fetch is
    `--prune`, so once origin's ref is gone a fetching run prunes the LOCAL ref
    too and exits at the earlier 'no such ref locally' guard — a different path
    that never reaches the push.
    """
    work = _merge_and_push_a(repo)
    # Acquire the refs/workers/* name locally (the fixture clone has the commit
    # but not the ref) via a plain report run, which fetches.
    _consume(work)
    assert "sid-aaaa" in _git(work, "ls-remote", "origin", "refs/workers/*")
    # Make origin REFUSE the delete. The goal's proposed trigger -- another agent
    # already deleted the ref -- does NOT work: measured on git 2.43.0, `git push
    # origin :<ref>` exits 0 when the remote ref is already gone (and
    # receive.denyDeletes did not change that either). A pre-receive hook is the
    # deterministic way in, and it stands for the real-world class this path
    # guards: a ref-protection rule or a server-side refusal.
    hooks = repo["origin"] / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'deletes refused by test hook' >&2\nexit 1\n")
    hook.chmod(0o755)

    r = _consume(work, "--no-fetch", "--retire", "refs/workers/alpha/sid-aaaa",
                 env={"WORKER_REF_TEAM_STATE_READER": _stub_reader(tmp_path, "null")})
    assert r.returncode == 1, (
        "a failed remote delete must still exit non-zero: " + r.stderr + r.stdout
    )
    receipts = work / "core" / "logs" / "worker-ref-retirements.jsonl"
    assert receipts.exists(), "the receipt must be written BEFORE the push (fail-safe order)"
    lines = [json.loads(l) for l in receipts.read_text().strip().splitlines()]
    rec = next(r for r in lines if r.get("outcome") == "attempted")
    assert rec["ref"] == "refs/workers/alpha/sid-aaaa"
    term = [r for r in lines if r.get("outcome") in ("delete_succeeded", "delete_failed")]
    assert len(term) == 1, f"expected one terminal line, got {term}"
    assert term[0]["outcome"] == "delete_failed", (
        "the whole point of g-306-479: a reader must be able to tell this "
        "retirement did NOT complete, without consulting any other source"
    )
    assert term[0]["ref"] == "refs/workers/alpha/sid-aaaa"


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
    rec = next(r for r in (json.loads(l) for l in
               receipts.read_text().strip().splitlines())
               if r.get("outcome") == "attempted")  # : not [-1] any more
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
    rec = next(r for r in (json.loads(l) for l in
               (work / "core" / "logs" / "worker-ref-retirements.jsonl")
               .read_text().strip().splitlines())
               if r.get("outcome") == "attempted")  # : not [-1] any more
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
    rec = next(r for r in (json.loads(l) for l in
               (work / "core" / "logs" / "worker-ref-retirements.jsonl")
               .read_text().strip().splitlines())
               if r.get("outcome") == "attempted")  # : not [-1] any more
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


def _land_same_content_on_main_by_another_route(work):
    """Put sid-bbbb's framework blob on main under a DIFFERENT commit.

    This is the hunk-carry shape: the reducer copied the hunks onto main
    instead of merging the ref, so the CONTENT is landed while the ref's own
    commits stay unreachable. Same blob, different commit — which is exactly
    what `commits_ahead` and the three-dot file list cannot see.
    """
    _git(work, "checkout", "main")
    (work / "core").mkdir(exist_ok=True)
    (work / "core" / "x.sh").write_text("echo x\n")   # byte-identical to sid-bbbb's
    _git(work, "add", ".")
    _git(work, "commit", "-m", "carry sid-bbbb's framework hunks onto main directly")
    _git(work, "push", "origin", "main")
    return _git(work, "rev-parse", "HEAD")


def test_phantom_framework_payload_is_not_a_demand_signal(repo):
    """ occ188. A ref whose framework blobs are ALREADY at HEAD by
    another route must not raise the pull signal, however loudly the three-dot
    file list still names those paths.

    The three-dot count is RIGHT for what it reports ("what did these commits
    touch" — guard-3094 / guard-4573 forbid switching it to two-dot). It is
    the wrong question for the DEMAND signal, which asks "would merging bring
    a framework path HEAD lacks". `framework_files_real` answers that one.

    Note the shape deliberately kept here: sid-bbbb ALSO carries a.txt, a
    non-framework path that is genuinely outstanding. So the ref is a PARTIAL
    phantom, and the whole-ref merge-tree discriminator (occ165) correctly
    returns not-phantom while the framework half is entirely phantom — which
    is why the test is per-path.
    """
    work = repo["work"]
    _land_same_content_on_main_by_another_route(work)
    r = _consume(work, "--json")
    assert r.returncode == 0, r.stderr
    by_sid, _ = _refs_by_sid(r.stdout)
    b = by_sid["sid-bbbb"]
    assert b["framework_files"] == 1, (
        "the three-dot list must STILL name the path — it reports what the "
        "ref's commits touched, and that is unchanged", b)
    assert b["framework_files_real"] == 0, (
        "merging contributes no framework content: the blob is already at "
        "HEAD under a different commit", b)

    assert b["merge_paths_real"] == 1, (
        "occ189: the framework half is phantom but a.txt is genuinely "
        "outstanding, so the WHOLE-ref merge is not empty", b)

    txt = _consume(work, "--check")
    assert txt.returncode == 0, txt.stderr
    assert "PHANTOM framework payload" in txt.stdout, txt.stdout
    assert "merge with:" not in txt.stdout, (
        "recommending a merge whose FRAMEWORK content is already at HEAD is "
        "the redundant no-op merge the drain protocol exists to prevent",
        txt.stdout)
    # occ189: this fixture is the MIXED case (phantom framework half + live
    # agent-store half), so --check must make the NARROW claim only. Until
    # 2026-09-12 it said "merging yields a no-op merge commit" here, which
    # would have told an operator to discard a.txt on the strength of a
    # framework-only measurement (guard-5122).
    assert "This is NOT a no-op merge" in txt.stdout, txt.stdout
    assert "merging would still change 1 path(s)" in txt.stdout, txt.stdout
    assert "no-op merge commit" not in txt.stdout, (
        "the whole-ref no-op claim is not licensed by fw_real alone",
        txt.stdout)


def test_real_framework_payload_still_raises_the_demand_signal(repo):
    """Positive control for the test above — WITHOUT it a fix that hard-coded
    framework_files_real=0 would pass. Same ref, same instrument, no main-side
    carry: the payload is genuinely outstanding and must read that way."""
    work = repo["work"]
    r = _consume(work, "--json")
    assert r.returncode == 0, r.stderr
    by_sid, _ = _refs_by_sid(r.stdout)
    b = by_sid["sid-bbbb"]
    assert b["framework_files"] == 1, b
    assert b["framework_files_real"] == 1, (
        "nothing landed this blob on main, so merging DOES bring it", b)

    txt = _consume(work, "--check")
    assert txt.returncode == 0, txt.stderr
    assert "merge with:" in txt.stdout, txt.stdout
    assert "PHANTOM framework payload" not in txt.stdout, txt.stdout
    # occ190 NEGATIVE CONTROL for the test below. The merge-tree read SUCCEEDED
    # here, so the payload is MEASURED and the fail-open note must be absent.
    # Without this assertion a fix that printed the note unconditionally would
    # pass the positive test while restoring the very indistinguishability
    # guard-2586 forbids.
    assert "FAIL-OPEN, not a measurement" not in txt.stdout, txt.stdout


def _git_merge_tree_shim(tmp_path, blob_oid):
    """PATH shim whose `git merge-tree` prints a BLOB oid instead of a tree.

    This is the "unresolvable tree" cause worker-ref-consume.sh names for
    mt_total=-1: `rev-parse --verify <blob>^{tree}` cannot peel a blob, so the
    phantom content test is skipped and fw_real/mt_total keep their fail-open
    values. No repository state can produce it — real merge-tree emits a tree
    or nothing — so shimming the binary is the only way to exercise the branch,
    and leaving it unexercised is how the branch's own comment stayed wrong for
    an hour (occ190). Every other subcommand execs the real git unchanged.
    """
    real_git = shutil.which("git")
    assert real_git, "no git on PATH to delegate to"
    d = tmp_path / "gitshim"
    d.mkdir(exist_ok=True)
    p = d / "git"
    p.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do\n'
        '  if [ "$a" = "merge-tree" ]; then printf \'%s\\n\' '
        f'"{blob_oid}"; exit 0; fi\n'
        "done\n"
        f'exec "{real_git}" "$@"\n'
    )
    p.chmod(0o755)
    return d


def test_unreadable_merge_tree_is_named_on_the_merge_recommendation(repo, tmp_path):
    """ occ190. `merge with:` is reached by TWO conditions and until
    2026-09-12 printed one sentence for both: (a) the content test RAN and
    found real framework payload, and (b) the content test could NOT run, so
    fw_real stayed at the three-dot touch count and the payload is ASSUMED.

    An operator cannot act differently on two readings that are byte-identical
    (guard-2586 — a fallback path and a failure path must never emit the same
    message; guard-4719 — compute the cause or the message lies on every other
    path). The fail-open BEHAVIOUR is correct and deliberately unchanged; what
    was missing is that it says so.
    """
    work = repo["work"]
    src = tmp_path / "notatree.txt"
    src.write_text("not a tree\n")
    blob = _git(work, "hash-object", "-w", str(src))
    shim = _git_merge_tree_shim(tmp_path, blob)

    txt = _consume(
        work, "--check",
        env={"PATH": f"{shim}{os.pathsep}{os.environ['PATH']}"},
    )
    assert txt.returncode == 0, txt.stderr
    assert "merge with:" in txt.stdout, (
        "fail-open is the intended behaviour — under-signalling strands "
        "framework work silently (guard-3660 asymmetry)", txt.stdout)
    assert "FAIL-OPEN, not a measurement" in txt.stdout, (
        "an unreadable merge-tree must SAY the payload was assumed from the "
        "three-dot touch list, not measured", txt.stdout)


def _land_the_WHOLE_ref_on_main_by_another_route(work):
    """Put BOTH of sid-bbbb's blobs on main under a different commit.

    Sibling of `_land_same_content_on_main_by_another_route`, which lands only
    the framework blob and therefore produces the MIXED case. This one lands
    the agent-store blob too, so merging sid-bbbb really would change nothing
    — the only state in which --check may say "no-op merge commit".
    """
    _git(work, "checkout", "main")
    (work / "core").mkdir(exist_ok=True)
    (work / "core" / "x.sh").write_text("echo x\n")   # byte-identical to sid-bbbb's
    (work / "a.txt").write_text("a\n")                # byte-identical to sid-aaaa's
    _git(work, "add", ".")
    _git(work, "commit", "-m", "carry ALL of sid-bbbb's hunks onto main directly")
    _git(work, "push", "origin", "main")
    return _git(work, "rev-parse", "HEAD")


def test_whole_ref_no_op_is_the_only_state_that_earns_the_no_op_claim(repo):
    """ occ189. The DISCRIMINATOR between the two phantom messages.

    occ188 gated the demand signal on a per-path content test (`fw_real`) and
    then used that same number to assert "merging yields a no-op merge
    commit". Those are different claims: `fw_real` looks only at framework
    paths, so it says nothing about an agent-store delta riding the same ref.
    The sibling test above pins the MIXED case; this one pins the case where
    the wide claim is actually earned, and the pair is what makes either
    assertion meaningful — without this one a fix that simply deleted the
    "no-op" sentence would pass.

    Not hypothetical: ddcd6db0 was in the mixed state at 02:29 on the very
    firing that shipped `fw_real` — four phantom framework blobs beside a
    genuinely outstanding experience.jsonl.
    """
    work = repo["work"]
    _land_the_WHOLE_ref_on_main_by_another_route(work)
    r = _consume(work, "--json")
    assert r.returncode == 0, r.stderr
    by_sid, _ = _refs_by_sid(r.stdout)
    b = by_sid["sid-bbbb"]
    assert b["framework_files"] == 1, (
        "the three-dot touch list is unchanged by any of this", b)
    assert b["framework_files_real"] == 0, b
    assert b["merge_paths_real"] == 0, (
        "both blobs are at HEAD, so the whole-ref merge-tree equals HEAD's "
        "tree", b)

    txt = _consume(work, "--check")
    assert txt.returncode == 0, txt.stderr
    assert "PHANTOM framework payload" in txt.stdout, txt.stdout
    assert "do NOT merge" in txt.stdout, txt.stdout
    assert "genuine no-op merge commit" in txt.stdout, txt.stdout
    assert "merge with:" not in txt.stdout, txt.stdout
    assert "This is NOT a no-op merge" not in txt.stdout, (
        "the wide claim IS licensed here — mt_total is 0", txt.stdout)


def test_no_op_claim_is_gated_on_the_whole_ref_count_in_source():
    """Source pin for the occ189 discriminator (sibling of the fw_real pin
    below). The two counts must stay SEPARATE: `fw_real` gates the pull
    signal, `mt_total` gates the no-op wording. Folding either into the other
    re-creates one of the two defects — a framework-only signal that fires on
    agent-store churn, or a no-op claim made from a framework-only reading.
    """
    src = CONSUME.read_text(encoding="utf-8")
    assert 'if [ "$mt_total" = 0 ] && [ "$mt_conf" = 0 ]; then' in src, (
        "the no-op sentence must be gated on the UNFILTERED merge-tree count "
        "(occ189) AND on a measured-clean merge (occ192) — a non-zero "
        "merge-tree rc must not be able to ride under a 'genuine no-op' claim")
    assert "This is NOT a no-op merge" in src, (
        "the mixed case needs its own message, not silence")
    assert 'mt_total=-1' in src, (
        "-1 is the UNMEASURED sentinel; a 0 default would make an unreadable "
        "merge-tree assert the strongest claim available")
    assert '"merge_paths_real":%s' in src, (
        "machine consumers need the same discrimination the text has")
    # The occ188 gate must be untouched by the occ189 fix.
    assert 'if [ "$fw_real" -gt 0 ]; then' in src
    assert "pull_fw_total=$((pull_fw_total+fw_real))" in src


def test_demand_signal_gate_reads_the_content_count_in_source():
    """Source pin: pull_tip_count must be gated on the CONTENT count, not the
    touch count. A future edit reverting the gate to fw_count restores the
    false out-of-cadence promotion measured at occ188 (echo/cc-03 raised a
    pull_signal naming 4 framework files that were all already on main)."""
    src = CONSUME.read_text(encoding="utf-8")
    assert 'if [ "$fw_real" -gt 0 ]; then' in src, (
        "the pull-signal gate must key on fw_real (content), not fw_count")
    assert "pull_fw_total=$((pull_fw_total+fw_real))" in src


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


# --- : the pull_signal producer gate -------------------------------
# Regression pins for the two ways --check wrote a phantom into a SHARED field.
# Both members are real measurements, not hypotheticals: guard-5953 (this test
# file's own fixture leaked into .pull_signal) and guard-5797 (a behind
# local branch manufactured outstanding tips out of already-landed commits).

def test_check_refuses_to_stamp_pull_signal_from_a_foreign_repo(repo):
    """THE regression pin for guard-5953. Invoked in the LITERAL production arg
    shape the rest of this file uses (`--repo <tmp>`), not a contract-ideal one
    (guard-920) — that shape IS the defect, because every test here passes it.

    Before the gate this exact call wrote a live pull_signal onto g-306-284
    naming refs/workers/alpha/sid-bbbb, a ref that exists in no repo on the
    fleet, and the producer's skip-while-live idempotence then blocked the real
    signal."""
    r = _consume(repo["work"], "--check", "--max-depth", "0", "--max-age-h", "9999")
    assert r.returncode == 0, "advisory mode must exit 0 even on breach"

    # NON-VACUITY CONTROL FIRST (guard-2421): a skip proves nothing if the
    # producer had nothing to stamp anyway. This fixture's sid-bbbb touches
    # core/x.sh, so the DO_CHECK branch reaches the producer with a real
    # outstanding framework tip — the refusal is the GATE, not an empty set.
    assert "sid-bbbb" in r.stdout and "framework_files=1" in r.stdout, (
        "control failed: the fixture must present an outstanding framework tip, "
        "or this test would pass even with the gate removed"
    )

    assert "SKIP-foreign-repo" in r.stdout, (
        "--check from a tmp repo must refuse to stamp the shared pull_signal"
    )
    assert "SET (" not in r.stdout, (
        "the producer must not have written: a SET verdict here means a test "
        "process just wrote into the live world store (guard-5953)"
    )


def test_foreign_repo_gate_cannot_be_bypassed_by_injecting_project_root(repo):
    """The gate compares against PROJECT_ROOT, and _paths.sh line 31 EXPORTS it
    unconditionally — so an injected value is clobbered before the comparison.
    That is the guard-2484 clobber working FOR us: it means no test, present or
    future, can talk itself past this gate into the live store."""
    r = _consume(repo["work"], "--check", "--max-depth", "0", "--max-age-h", "9999",
                 env={"PROJECT_ROOT": str(repo["work"])})
    assert r.returncode == 0
    assert "SKIP-foreign-repo" in r.stdout, (
        "injecting PROJECT_ROOT must NOT let a tmp repo pose as the bound repo"
    )
    assert "SET (" not in r.stdout


def test_behind_base_is_measured_from_a_genuinely_behind_repo(tmp_path):
    """guard-5797 / outcome 3: a GENUINELY behind base, never a mocked count.

    Builds a clone whose local main is really two commits behind origin/main and
    asserts the gate's own predicate — `git rev-list --count HEAD..refs/remotes/
    origin/main` — reports it. This is the condition under which already-landed
    commits read as outstanding, which is what manufactured the phantom tip.

    Asserted as the predicate rather than end-to-end because the foreign-repo
    gate (correctly) fires first on any tmp repo, so the behind-base branch is
    unreachable from a fixture — stated plainly rather than papered over with a
    seam that would reopen the leak the test above pins shut."""
    origin = tmp_path / "o.git"
    work = tmp_path / "w"
    _run(["git", "init", "--bare", "--initial-branch=main", str(origin)])
    _run(["git", "clone", str(origin), str(work)])
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    _git(work, "checkout", "-b", "main")
    (work / "f.txt").write_text("base\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "base")
    _git(work, "push", "origin", "main")

    # advance origin by two real commits, then park local main behind them
    for n in ("one", "two"):
        (work / f"{n}.txt").write_text(f"{n}\n")
        _git(work, "add", ".")
        _git(work, "commit", "-m", n)
    _git(work, "push", "origin", "main")
    _git(work, "fetch", "origin")
    _git(work, "reset", "--hard", "HEAD~2")

    behind = _git(work, "rev-list", "--count", "HEAD..refs/remotes/origin/main")
    assert behind == "2", (
        f"expected a genuinely behind base of 2 commits, got {behind!r} — the "
        "fixture, not the gate, is wrong if this fails"
    )
    # Positive control on the predicate's other direction: a synced base is 0.
    _git(work, "reset", "--hard", "refs/remotes/origin/main")
    assert _git(work, "rev-list", "--count", "HEAD..refs/remotes/origin/main") == "0"


def test_pull_producer_gate_is_pinned_in_source(repo):
    """guard-920 mitigation, mirroring test_liveness_seam_default_is_the_real_
    sibling above: the behaviour tests can only exercise the branch they reach,
    so pin that the producer call is INSIDE the gated else-branch and that both
    refusal reasons still exist. Without this, someone could delete the
    behind-base arm and every test here would stay green."""
    src = CONSUME.read_text(encoding="utf-8", errors="replace")
    assert "SKIP-foreign-repo" in src
    assert "SKIP-behind-base" in src
    assert "SKIP-unreadable-base" in src
    # the base must be handed to the producer (outcome 1) — a stamp with no
    # recorded base is the unauditable number guard-5797 forbids
    assert '--base "head=$pull_head_sha origin=$pull_origin_sha"' in src
    # and the producer call must sit under the gate, not beside it
    gate_at = src.index("pull_skip=\"\"")
    call_at = src.index("pull-signal-set.sh")
    assert gate_at < call_at, (
        "the pull-signal-set.sh invocation must come AFTER the gate is computed"
    )
    assert (SCRIPTS / "pull-signal-set.sh").exists()


# ---------------------------------------------------------------------------
# goal_ids — WHAT work a carrier ref strands, not just how much ()
#
# The incident: a reducer selecting  weighed carrier-ref disposition
# against it and deprioritised the ref as "0-1h old, not urgent". That ref held
# , a decomposition child of , and its commit messages named
# BOTH ids. `commits_ahead` and `framework_files` could not express that, so the
# ordering was wrong on information the repository already had.
#
# CHURN EXCLUSION IS THE LOAD-BEARING HALF. iteration-push.sh's self-heal commit
# (iteration-push.sh:1083) names  in EVERY such commit on EVERY ref, so
# without the --invert-grep filter every ref in the fleet reports the same
# constant as stranded work — a field that always says the same thing tells a
# reader nothing, which is the failure mode this whole change exists to remove.
# ---------------------------------------------------------------------------

CHURN_SUBJECT = (
    "chore(alpha): pre-merge self-namespace churn (iteration-push self-heal, g-115-2249)"
)


@pytest.fixture()
def goalid_repo(tmp_path):
    """origin + work clone carrying three worker refs:

      sid-named  — one commit whose message names g-369-119 (subject) and
                   g-369-30 (body): the incident's exact shape.
      sid-churn  — one commit carrying ONLY the self-heal churn subject.
      sid-plain  — one commit whose message names no goal at all.

    HEAD is parked at base so every ref is ahead of it. The churn commit touches
    a non-framework path so its fw_count is 0, exactly like the real one.
    """
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
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

    def _branch_commit(fname, body, message):
        _git(work, "reset", "--hard", base)
        (work / fname).parent.mkdir(parents=True, exist_ok=True)
        (work / fname).write_text(body)
        _git(work, "add", ".")
        _git(work, "commit", "-m", message)
        return _git(work, "rev-parse", "HEAD")

    sha_named = _branch_commit(
        "core/handle.py",
        "handle\n",
        "feat(knowledge-projection): opaque per-goal handle (g-369-119)\n\n"
        "Precondition for g-369-30.\n",
    )
    _git(work, "push", "origin", f"{sha_named}:refs/workers/alpha/sid-named")

    sha_churn = _branch_commit("notes/churn.txt", "churn\n", CHURN_SUBJECT)
    _git(work, "push", "origin", f"{sha_churn}:refs/workers/alpha/sid-churn")

    sha_plain = _branch_commit("core/plain.sh", "echo plain\n", "chore: tidy up")
    _git(work, "push", "origin", f"{sha_plain}:refs/workers/alpha/sid-plain")

    _git(work, "reset", "--hard", base)
    return {"origin": origin, "work": work, "base": base}


def test_goal_ids_names_the_goals_the_unlanded_commits_carry(goalid_repo):
    """The incident's ref shape: subject names the child, body names the parent.
    BOTH must surface — the parent id is the one that would have changed the
    reducer's ordering, and it lives only in the body."""
    r = _consume(goalid_repo["work"], "--json")
    assert r.returncode == 0, r.stderr
    by_sid, _ = _refs_by_sid(r.stdout)
    named = by_sid["sid-named"]
    assert named["commits_ahead"] == 1, named
    ids = named["goal_ids"].split()
    assert ids == ["g-369-119", "g-369-30"], named["goal_ids"]


def test_goal_ids_excludes_the_self_heal_churn_constant(goalid_repo):
    """The churn commit names  in every such commit on every ref.

    ANTI-VACUITY: the final assertion would also pass if goal_ids were broken
    and always empty, so this test first PROVES the churn commit is present in
    the ref's log and that its id is extractable from the raw messages — i.e.
    that the exclusion is doing work rather than the extractor being dead."""
    work = goalid_repo["work"]
    ref = "refs/workers/alpha/sid-churn"
    # --json runs FIRST because the script's own fetch is what materialises the
    # worker refs in this clone (the fixture pushes them to origin only). The
    # positive controls below would die rc=128 "ambiguous argument" if they ran
    # against a ref this repo has never fetched — which reads like a defect in
    # the feature rather than a test ordering bug.
    r = _consume(work, "--json")
    assert r.returncode == 0, r.stderr
    # positive control 1: the commit really is on the ref, ahead of HEAD
    assert _git(work, "rev-list", "--count", f"HEAD..{ref}") == "1"
    # positive control 2: unfiltered, its id IS in the message stream
    raw = _git(work, "log", "--format=%B", f"HEAD..{ref}")
    assert "g-115-2249" in raw

    by_sid, _ = _refs_by_sid(r.stdout)
    churn = by_sid["sid-churn"]
    assert churn["commits_ahead"] == 1, churn
    assert churn["goal_ids"] == "", churn


def test_goal_ids_is_empty_when_no_commit_names_a_goal(goalid_repo):
    """Empty means 'no commit here NAMED a goal', never 'nothing is stranded' —
    commits_ahead stays the count of record and must remain non-zero here."""
    r = _consume(goalid_repo["work"], "--json")
    assert r.returncode == 0, r.stderr
    by_sid, _ = _refs_by_sid(r.stdout)
    plain = by_sid["sid-plain"]
    assert plain["commits_ahead"] == 1, plain
    assert plain["goal_ids"] == "", plain
    # ...and the extractor is not simply dead in this run:
    assert by_sid["sid-named"]["goal_ids"], "extractor produced nothing anywhere"


def test_human_report_prints_carried_goals_only_when_there_are_some(goalid_repo):
    """--check reaches the human report, and the drain decision is made there.
    The line must appear for the named ref and NOT for the two without ids."""
    r = _consume(goalid_repo["work"], "--check")
    assert r.returncode == 0, r.stderr
    out = r.stdout + r.stderr
    assert "carries work naming: g-369-119 g-369-30" in out, out
    assert out.count("carries work naming:") == 1, out


def test_every_json_row_carries_the_goal_ids_key(goalid_repo):
    """A consumer reading .goal_ids must never hit a KeyError on some rows —
    the field is emitted for every ref, empty string included."""
    r = _consume(goalid_repo["work"], "--json")
    assert r.returncode == 0, r.stderr
    _, data = _refs_by_sid(r.stdout)
    assert data["refs"], "no refs emitted"
    assert all("goal_ids" in row for row in data["refs"]), data["refs"]


def test_daemon_only_ref_counts_as_framework_and_is_pullable(repo):
    """A carrier ref whose commits touch ONLY mind_api/src must count as
    framework work (g-306-436). While the :408 predicate read
    '^(core/|\\.claude/|CLAUDE\\.md)' such a ref reported framework_files=0, so
    it never incremented pull_tip_count and raised NO pull signal — daemon-only
    work waited out the ordinary interval timer while core/-touching work was
    promoted out of cadence (pull_boost=4.00). The ref was never invisible
    (n_outstanding increments unconditionally); what it lost was the DEMAND
    signal, and with it the ':506' merge suggestion naming the changed files.

    promotion-preflight.sh already counts mind_api/{src,tests} as framework
    (.claude/rules/promotion-cycle.md, Pre-Overwrite Drift Gate); this pins the
    two framework definitions together so they cannot drift apart again.

    Measured when this landed (30d window, 6,718 non-merge commits): 273 touched
    mind_api/ and 100 of those touched NO core/|.claude/|CLAUDE.md — 36.6% of
    all daemon work, i.e. the population this predicate was dropping is common,
    not hypothetical."""
    work = repo["work"]
    (work / "mind_api" / "src").mkdir(parents=True, exist_ok=True)
    (work / "mind_api" / "src" / "d.py").write_text("X = 1\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "worker D daemon-only commit")
    sha_d = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", f"{sha_d}:refs/workers/alpha/sid-dddd")
    _git(work, "reset", "--hard", "HEAD~1")

    r = _consume(work, "--json")
    assert r.returncode == 0, r.stderr
    by_sid, _ = _refs_by_sid(r.stdout)
    d = by_sid["sid-dddd"]
    assert d["commits_ahead"] == 1, d
    assert d["framework_files"] == 1, (
        "daemon-only ref reported framework_files=%s — the :408 predicate is "
        "excluding mind_api/, so this ref raises no pull signal (g-306-436)"
        % d["framework_files"]
    )

    # The actionable half: the human report must name the file and offer the
    # merge, which is the ':506' branch gated on the same fw_count.
    h = _consume(work, "--check")
    assert h.returncode == 0, h.stderr
    out = h.stdout + h.stderr
    assert "mind_api/src/d.py" in out, out


# ── occ191: agent-store-only carrier refs (fw_count=0, commits_ahead>0) ──────
# The class `mt_total` exists for was the one class it never measured. The
# merge-tree block was gated on `fw_count > 0`, and so was the whole per-ref
# detail report — so a PURE agent-store ref reported two numbers and nothing
# else, the safe-looking one being framework_files=0. guard-6539 was written
# for exactly that misreading ("means nothing under core/ — NOT safe to
# merge") and a guardrail cannot outvote the instrument it guards
# (guard-1984). Measured live on ref faec5e55: merging would have dropped 99
# append-only changelog records and added none, with the report silent.


def _agentstore_repo(tmp_path):
    """main carrying a 5-line append-only agent store, plus two SIBLING
    carrier refs branched from it — one STALE (rewrote the store shorter, so
    merging deletes) and one APPENDING (added lines, so merging adds).

    Siblings, not a chain: neither is an ancestor of the other, so both are
    TIPs and both reach the new report branch. Every path is under agents/,
    so framework_files is 0 for both by construction — that is the point.
    """
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    r = _run(["git", "init", "--bare", "--initial-branch=main", str(origin)])
    assert r.returncode == 0, r.stderr
    r = _run(["git", "clone", str(origin), str(work)])
    assert r.returncode == 0, r.stderr
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    _git(work, "checkout", "-b", "main")
    store = work / "agents" / "alpha"
    store.mkdir(parents=True)
    (store / "log.jsonl").write_text("".join(f'{{"n": {i}}}\n' for i in range(1, 6)))
    _git(work, "add", ".")
    _git(work, "commit", "-m", "base: 5-line append-only agent store")
    _git(work, "push", "origin", "main")
    base = _git(work, "rev-parse", "HEAD")

    # STALE carrier: a Body whose snapshot predates the last 3 appends and
    # which committed that shorter file back. Merging it DELETES 3 records.
    (store / "log.jsonl").write_text("".join(f'{{"n": {i}}}\n' for i in range(1, 3)))
    _git(work, "add", ".")
    _git(work, "commit", "-m", "stale body snapshot of the agent store")
    sha_stale = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", f"{sha_stale}:refs/workers/alpha/sid-stale")
    _git(work, "reset", "--hard", base)

    # APPENDING carrier: the ordinary healthy shape. Merging it ADDS 2.
    (store / "log.jsonl").write_text("".join(f'{{"n": {i}}}\n' for i in range(1, 8)))
    _git(work, "add", ".")
    _git(work, "commit", "-m", "body appended two records")
    sha_append = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", f"{sha_append}:refs/workers/alpha/sid-append")
    _git(work, "reset", "--hard", base)
    return {"origin": origin, "work": work}


@pytest.fixture()
def agentstore_repo(tmp_path):
    return _agentstore_repo(tmp_path)


def test_agent_store_only_ref_is_measured_not_left_unmeasured(agentstore_repo):
    """occ191. fw_count=0 must no longer imply merge_paths_real=-1.

    Before the fix both refs below reported merge_paths_real=-1 (UNMEASURED)
    forever, because the merge-tree block only ran when fw_count>0 — i.e. the
    sentinel that exists to stop a framework-only reading being generalised
    was itself unavailable for every ref that has NO framework half.
    """
    work = agentstore_repo["work"]
    r = _consume(work, "--json")
    assert r.returncode == 0, r.stderr
    by_sid, _ = _refs_by_sid(r.stdout)
    for sid in ("sid-stale", "sid-append"):
        row = by_sid[sid]
        assert row["framework_files"] == 0, row
        assert row["commits_ahead"] == 1, row
        assert row["merge_paths_real"] == 1, (
            "the content test must RUN for an agent-store-only ref; -1 here "
            "is the pre-occ191 blind spot", row)


def test_deletion_only_merge_is_named_as_a_stale_carrier_not_as_content(agentstore_repo):
    """The measured faec5e55 case: merging removes records and adds none.

    A path COUNT cannot distinguish this from content arriving, and the count
    is what the operator sees. Direction is what makes the line actionable.
    """
    work = agentstore_repo["work"]
    by_sid, _ = _refs_by_sid(_consume(work, "--json").stdout)
    row = by_sid["sid-stale"]
    assert row["merge_added_real"] == 0, row
    assert row["merge_deleted_real"] == 3, row

    txt = _consume(work, "--check")
    assert txt.returncode == 0, txt.stderr
    assert "DELETION-ONLY" in txt.stdout, txt.stdout
    assert "Default disposition = CARRY" in txt.stdout, txt.stdout
    assert "guard-6539" in txt.stdout, (
        "the line must name the guardrail whose trap it closes", txt.stdout)
    assert "merge with:" not in txt.stdout, (
        "a deletion-only carrier must never carry a merge recommendation",
        txt.stdout)


def test_appending_agent_store_ref_is_the_positive_control(agentstore_repo):
    """Both directions in one fixture, or an always-warns bug reads as a find.

    Same code path, same store, opposite delta: this ref must NOT get the
    deletion warning, and must be named as the safe append shape.
    """
    work = agentstore_repo["work"]
    by_sid, _ = _refs_by_sid(_consume(work, "--json").stdout)
    row = by_sid["sid-append"]
    assert row["merge_added_real"] == 2, row
    assert row["merge_deleted_real"] == 0, row

    txt = _consume(work, "--check")
    assert "append-only (+2 / -0)" in txt.stdout, txt.stdout


def test_widening_the_content_test_cannot_raise_the_demand_signal(agentstore_repo):
    """The safety argument for widening, asserted rather than reasoned.

    `fw_real` filters the merge-tree result to framework prefixes and the
    merge result vs HEAD is a SUBSET of the three-dot touch list, so
    fw_real <= fw_count always. At fw_count=0 the newly-reachable branch must
    therefore leave fw_real at 0 and contribute nothing to the pull signal —
    the widening adds a measurement, never a signal (guard-2499).
    """
    work = agentstore_repo["work"]
    by_sid, _ = _refs_by_sid(_consume(work, "--json").stdout)
    for sid in ("sid-stale", "sid-append"):
        assert by_sid[sid]["framework_files_real"] == 0, by_sid[sid]


def test_content_test_entry_condition_covers_agent_store_refs_in_source():
    """Source pin. Narrowing the guard back to `fw_count > 0` restores the
    blind spot silently — every agent-store-only ref would go back to
    reporting -1 with no detail line, and no test that reads only framework
    refs would notice."""
    src = CONSUME.read_text(encoding="utf-8")
    assert 'if [ "$fw_count" -gt 0 ] || [ "$ahead" -gt 0 ]; then' in src, (
        "the merge-tree content test must run for refs with commits but no "
        "framework payload (occ191)")
    assert 'elif [ "$ahead" -gt 0 ] && [ "$is_self" = 0 ]; then' in src, (
        "the report needs its own branch for agent-store-only refs; without "
        "it the measurement is taken and never shown")
    assert '"merge_added_real":%s' in src and '"merge_deleted_real":%s' in src, (
        "machine consumers need direction, not only a path count")
    assert "mt_add=-1" in src and "mt_del=-1" in src, (
        "-1 stays the UNMEASURED sentinel for the direction fields too")


# ── occ192: the CONFLICTING merge-tree, the one shape the suite never had ────
# Everything above pins the merge-tree read for CLEAN results — phantom
# framework payload (occ188), the whole-ref no-op (occ165), direction on an
# agent store (occ191), and an UNREADABLE tree via a PATH shim (occ190, a state
# no repository can produce). The reachable conflicted state had no fixture at
# all, and it was live on cc-04 while all 59 of those tests were green: ref
# faec5e55 merged with `CONFLICT (add/add)` on an experience file while --check
# printed "append-only (+171 / -0): safe shape. Merge only if the content is
# wanted". New-mode `git merge-tree` still WRITES a tree for a conflicted merge
# (markers embedded in the conflicting blob) and signals the conflict two ways
# the script was discarding: a non-zero rc, destroyed by the `| head -1`
# pipeline (guard-1473), and a `CONFLICT ...` report on lines 2..N, dropped by
# the same `head`. Both are free and already in that output.


def _conflict_repo(tmp_path):
    """main and a carrier ref that ADD THE SAME PATH with different content.

    add/add is the shape measured live, and it is the one a snapshot carrier
    reaches naturally: two Bodies writing the same experience file from a
    common base neither of them had it in.

    A CLEAN sibling ref is built in the same fixture deliberately. An
    always-warns regression and a working discriminator are textually
    identical when only the conflicting ref is asserted — the negative control
    is what makes a green run mean something (self.md: run a positive control
    before believing a zero, and its converse here).
    """
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    r = _run(["git", "init", "--bare", "--initial-branch=main", str(origin)])
    assert r.returncode == 0, r.stderr
    r = _run(["git", "clone", str(origin), str(work)])
    assert r.returncode == 0, r.stderr
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    _git(work, "checkout", "-b", "main")
    store = work / "agents" / "alpha"
    (store / "experience").mkdir(parents=True)
    (store / "log.jsonl").write_text('{"n": 1}\n')
    _git(work, "add", ".")
    _git(work, "commit", "-m", "base: agent store, no experience file yet")
    _git(work, "push", "origin", "main")
    base = _git(work, "rev-parse", "HEAD")

    # CONFLICTING carrier: adds exp-x.md with the Body's content.
    (store / "experience" / "exp-x.md").write_text("written by the body\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "body wrote its experience file")
    sha_conf = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", f"{sha_conf}:refs/workers/alpha/sid-conflict")
    _git(work, "reset", "--hard", base)

    # CLEAN carrier: appends to a file HEAD does not touch. Merges cleanly.
    (store / "log.jsonl").write_text('{"n": 1}\n{"n": 2}\n')
    _git(work, "add", ".")
    _git(work, "commit", "-m", "body appended one record")
    sha_clean = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", f"{sha_clean}:refs/workers/alpha/sid-clean")
    _git(work, "reset", "--hard", base)

    # HEAD adds the SAME path with DIFFERENT content -> add/add against the
    # conflicting carrier, and no interaction at all with the clean one.
    # The mkdir is load-bearing: `reset --hard base` above removed exp-x.md and
    # with it the now-empty experience/ dir (git tracks no empty directory), so
    # the write below lands in a path that no longer exists.
    (store / "experience").mkdir(parents=True, exist_ok=True)
    (store / "experience" / "exp-x.md").write_text("written at head\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "head wrote the same experience file")
    _git(work, "push", "origin", "main")
    return {"origin": origin, "work": work}


@pytest.fixture()
def conflict_repo(tmp_path):
    return _conflict_repo(tmp_path)


def _ref_block(stdout, sid):
    """The --check lines belonging to ONE ref.

    Scoping matters here for the same reason it mattered in the subject: the
    fixture deliberately reports a CLEAN sibling in the same run, which
    legitimately prints "safe shape", so a whole-stdout negative assertion
    measures the wrong population and fails against correct output. (Caught by
    this very test on its first run — the defect class the subject had.)
    """
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


def test_conflicting_merge_is_named_and_the_shape_verdict_is_withheld(conflict_repo):
    """The live faec5e55 case. --check must SAY the merge conflicts, name the
    path, and NOT emit a reassuring shape verdict over a conflicted tree."""
    work = conflict_repo["work"]
    txt = _consume(work, "--check")
    assert txt.returncode == 0, txt.stderr
    block = _ref_block(txt.stdout, "sid-conflict")
    assert block, ("the conflicting ref must appear in the report", txt.stdout)
    assert "CONFLICT" in block, (
        "a conflicting merge must be disclosed; merge-tree signals it by rc "
        "AND by a CONFLICT line, and the script reads that output already",
        block)
    assert "agents/alpha/experience/exp-x.md" in block, (
        "naming the count without the path leaves the operator nowhere to "
        "start", block)
    assert "safe shape" not in block, (
        "the +/- counts are taken over the CONFLICTED tree (markers "
        "included), so no shape verdict may be asserted from them", block)
    assert "Merge only if the content is wanted" not in block, (
        "that sentence invites a merge that will stop mid-way", block)


def test_clean_sibling_block_still_gets_its_shape_verdict(conflict_repo):
    """The suppression must be SCOPED to the conflicting ref. A regression that
    withholds the verdict everywhere would pass the test above."""
    work = conflict_repo["work"]
    block = _ref_block(_consume(work, "--check").stdout, "sid-clean")
    assert "append-only (+1 / -0): safe shape" in block, block
    assert "CONFLICT" not in block, (
        "a clean merge must not be warned about", block)


def test_clean_sibling_ref_is_the_negative_control(conflict_repo):
    """Same fixture, same run, no conflict: an always-warns bug must not read
    as a working discriminator."""
    work = conflict_repo["work"]
    by_sid, _ = _refs_by_sid(_consume(work, "--json").stdout)
    assert by_sid["sid-clean"]["merge_conflicts_real"] == 0, (
        "0 is MEASURED clean and is only set inside the resolved-tree branch",
        by_sid["sid-clean"])
    assert by_sid["sid-conflict"]["merge_conflicts_real"] == 1, (
        by_sid["sid-conflict"])


def test_merge_conflicts_real_keeps_the_unmeasured_sentinel(conflict_repo):
    """-1 means UNMEASURED, never 'clean'. A consumer reading -1 as 0 makes
    exactly the claim the human line refuses to make — the same inversion
    merge_paths_real's -1 convention exists to prevent (occ189)."""
    src = CONSUME.read_text(encoding="utf-8")
    assert "mt_conf=-1" in src, "-1 is the UNMEASURED initial value"
    assert '"merge_conflicts_real":%s' in src, (
        "machine consumers need the conflict signal too, not only the report")
    # A ref with no commits ahead never enters the merge-tree block, so its
    # conflict state is genuinely unmeasured and must report as such.
    work = conflict_repo["work"]
    _git(work, "push", "origin", "HEAD:refs/workers/alpha/sid-behind")
    by_sid, _ = _refs_by_sid(_consume(work, "--json").stdout)
    assert by_sid["sid-behind"]["merge_conflicts_real"] == -1, (
        "no commits ahead => the content test never ran => UNMEASURED",
        by_sid["sid-behind"])


def test_conflict_disclosure_survives_a_reworded_conflict_line(conflict_repo):
    """Source pin on the belt-and-braces half. The CONFLICT line is parsed by
    text, so a git wording change would silently downgrade the disclosure to
    'clean'; the non-zero rc is the independent signal that must keep it."""
    src = CONSUME.read_text(encoding="utf-8")
    assert '_mtrc=$?' in src, (
        "merge-tree's own rc must be captured off the UNPIPED command "
        "(guard-1473) — a trailing `| head -1` reports head's 0")
    assert '[ "$_mtrc" -ne 0 ] && [ "$mt_conf" = 0 ] && mt_conf=1' in src, (
        "a non-zero rc with no parsed CONFLICT line is still a conflict; "
        "trust the status over the parse")


def _has_merge_head(work):
    return _run(["git", "-C", str(work), "rev-parse", "-q", "--verify",
                 "MERGE_HEAD"]).returncode == 0


def test_merge_refused_by_dirty_tree_is_not_called_a_conflict(repo):
    """ (2026-09-24, cc-07): git refused the merge because the live
    session had re-dirtied a store file the merge would overwrite, and --merge
    printed "MERGE CONFLICT — resolve by hand". Nothing was in conflict: there
    was no MERGE_HEAD, and the only "resolution" a reader could find was to
    discard the dirty file, which was live session state."""
    work = repo["work"]
    (work / "a.txt").write_text("local\n")  # untracked; ref sid-aaaa adds a.txt
    r = _consume(work, "--merge", "refs/workers/alpha/sid-aaaa")
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "MERGE REFUSED" in r.stderr and "MERGE CONFLICT" not in r.stderr, r.stderr
    assert not _has_merge_head(work), "a refused merge starts no merge"
    assert (work / "a.txt").read_text() == "local\n", "the dirty file is untouched"


def test_real_merge_conflict_is_still_called_a_conflict(conflict_repo):
    """Positive control for the test above (guard-4166): the refused-merge
    branch must not swallow a genuine conflict, which DOES leave a MERGE_HEAD."""
    work = conflict_repo["work"]
    r = _consume(work, "--merge", "refs/workers/alpha/sid-conflict")
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "MERGE CONFLICT" in r.stderr and "MERGE REFUSED" not in r.stderr, r.stderr
    assert _has_merge_head(work), "a real conflict leaves the merge in progress"
