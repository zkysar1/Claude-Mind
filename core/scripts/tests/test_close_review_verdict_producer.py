"""Producer half of the close-review gate ().

The CONSUMER (`close-review-gate.py`) has been pinned since g-357-40/42; what had
no writer was the verdict artifact itself, so the gate could only ship dormant.
These cases pin the writer, and above all pin the two asymmetries that make it
worth having rather than merely present:

  * the machine may VETO an approval on its own evidence and may never GRANT one
    (guard-2564 — a label must not assert more than its predicate supports), and
  * a verdict is never invented: with neither --approve nor --reject nothing is
    written, so "the reviewer did not say" stays distinguishable from "the
    reviewer said no".

The coach g-012-02 fixture is reproduced here rather than imported from
test_close_review_coach_fixture.py on purpose: that file pins the CONSUMER's
behaviour against the shape, this one pins the PRODUCER's, and a shared import
would let one file's edit silently change the other's subject.

Every run pins CLOSE_REVIEW_LEDGER_DIR to tmp_path, so no case can write into the
real world ledger, and STORAGE_BACKEND=local (guard-955) so a tmp write can never
collide on a production object-store key.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PRODUCER = REPO_ROOT / "core" / "scripts" / "close-review-verdict.py"
GATE = REPO_ROOT / "core" / "scripts" / "close-review-gate.py"

sys.path.insert(0, str(REPO_ROOT / "core" / "scripts"))
from goal_close_risk_tier import count_named_entities, named_entities  # noqa: E402

# The 16 entities the coach goal enumerated.
SOURCE_ENTITIES = [
    "g-012-02", "g-012-03", "g-012-04", "g-012-05",
    "guard-8801", "guard-8802", "guard-8803", "guard-8804",
    "rb-7701", "rb-7702", "rb-7703", "rb-7704",
    "asp-4401", "asp-4402", "sq-3301", "sig-2201",
]
# Same COUNT, last 6 identities displaced — the famous-name-prior substitution.
ARTIFACT_ENTITIES = SOURCE_ENTITIES[:10] + [
    "rb-7799", "rb-7798", "asp-4499", "asp-4498", "sq-3399", "sig-2299",
]

COACH_SOURCE = (
    "Catalogue each of the following and record its disposition: "
    + ", ".join(SOURCE_ENTITIES)
    + ". Deliverable is one row per entity."
)


def _fixture(tmp_path: Path, entities) -> Path:
    p = tmp_path / f"artifact-{len(entities)}-{abs(hash(tuple(entities))) % 9999}.txt"
    p.write_text("\n".join(f"- {e}: done" for e in entities), encoding="utf-8")
    return p


def _source(tmp_path: Path) -> Path:
    p = tmp_path / "source.txt"
    p.write_text(COACH_SOURCE, encoding="utf-8")
    return p


def _run(tmp_path: Path, *args, env_extra=None):
    env = dict(os.environ)
    env["STORAGE_BACKEND"] = "local"                  # guard-955
    env["CLOSE_REVIEW_LEDGER_DIR"] = str(tmp_path)    # never the real ledger
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(PRODUCER), *args],
                          capture_output=True, text=True, env=env, timeout=120)


def _run_gate(tmp_path: Path, goal: dict, closer: str):
    gj = tmp_path / "goal.json"
    gj.write_text(json.dumps(goal), encoding="utf-8")
    env = dict(os.environ)
    env["STORAGE_BACKEND"] = "local"
    env["CLOSE_REVIEW_LEDGER_DIR"] = str(tmp_path)
    env["CLOSE_REVIEW_GATE_ENABLED"] = "1"
    env["MIND_AGENT"] = closer
    return subprocess.run(
        [sys.executable, str(GATE), "--goal", goal["goal_id"], "--goal-json", str(gj)],
        capture_output=True, text=True, env=env, timeout=120)


def _coach_goal(goal_id="producer-coach-shape") -> dict:
    return {
        "goal_id": goal_id,
        "title": "Catalogue 16 entities and record dispositions",
        "description": COACH_SOURCE,
        "priority": "MEDIUM",
        "participants": ["agent"],
        "verification": {"outcomes": ["all 16 entries present in the artifact"]},
    }


# ─── the SSOT refactor: one regex, two views ──────────────────────────────────

def test_named_entities_and_count_share_one_definition():
    """count_named_entities must be exactly len(named_entities).

    The producer diffs entity SETS while the tier classifier counts them. A
    second regex in the producer would let the classifier route a goal to tier 2
    for entities the reviewer then could not see, with nothing failing when the
    two drifted.
    """
    assert count_named_entities(COACH_SOURCE) == 16
    assert len(named_entities(COACH_SOURCE)) == count_named_entities(COACH_SOURCE)
    assert named_entities(None) == set() and count_named_entities(None) == 0


def test_the_count_is_green_while_the_identities_are_wrong():
    """The founding incident, restated at the producer's own inputs."""
    src, art = named_entities(COACH_SOURCE), named_entities(" ".join(ARTIFACT_ENTITIES))
    assert len(src) == len(art) == 16          # the shipped criterion: GREEN
    assert len(src - art) == 6                 # what it never looked at
    assert len(art - src) == 6


# ─── outcome 2: the reviewer REJECTS the coach fixture citing check 2 ──────────

def test_coach_fixture_is_REJECTED_citing_source_fidelity(tmp_path):
    r = _run(tmp_path, "--goal", "coach", "--reviewer", "peer",
             "--source-file", str(_source(tmp_path)),
             "--artifact-file", str(_fixture(tmp_path, ARTIFACT_ENTITIES)),
             "--reject")
    assert r.returncode == 3, r.stdout + r.stderr
    payload = json.loads(r.stdout)
    assert payload["verdict"] == "REJECT"

    blob = " ".join(payload["findings"])
    assert "source-fidelity" in blob, payload["findings"]
    # The ids VERBATIM, not a count. The whole lesson of the founding incident is
    # that a number concealed the defect, so a finding reporting only "6" would
    # repeat the mistake it exists to report.
    for missing in ("asp-4401", "asp-4402", "rb-7703", "rb-7704", "sq-3301", "sig-2201"):
        assert missing in blob, f"{missing} not named in findings: {payload['findings']}"
    assert payload["fidelity"]["counts_match"] is True, (
        "the record must preserve that the count-based criterion was GREEN — that "
        "is what shows a future reader why counting was not enough")
    assert payload["fidelity"]["substitution_signature"] is True


def test_the_record_reproduces_its_own_verdict(tmp_path):
    """guard-3743: a decision record carries the inputs its verdict follows from.

    A reader with only the artifact must be able to recompute REJECT without the
    session that wrote it.
    """
    r = _run(tmp_path, "--goal", "coach", "--reviewer", "peer",
             "--source-file", str(_source(tmp_path)),
             "--artifact-file", str(_fixture(tmp_path, ARTIFACT_ENTITIES)),
             "--reject")
    fid = json.loads(r.stdout)["fidelity"]
    recomputed = "APPROVE" if not fid["missing"] else "REJECT"
    assert recomputed == json.loads(r.stdout)["verdict"]
    assert set(fid["source_entities"]) - set(fid["artifact_entities"]) == set(fid["missing"])


# ─── the asymmetry: veto yes, grant no (guard-2564) ───────────────────────────

def test_approve_is_REFUSED_when_the_fidelity_diff_fails(tmp_path):
    """The load-bearing case. A reviewer asserting the judgment checks passed
    cannot override the mechanical one, so the APPROVE label can never outrun the
    evidence. Without this the producer would happily write the very verdict the
    founding incident produced."""
    r = _run(tmp_path, "--goal", "coach", "--reviewer", "peer",
             "--source-file", str(_source(tmp_path)),
             "--artifact-file", str(_fixture(tmp_path, ARTIFACT_ENTITIES)),
             "--approve", "--write")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "REFUSING to write APPROVE" in r.stderr
    # and nothing was written
    assert not (tmp_path / "audit-reports" / "close-reviews" / "coach.json").exists()


def test_a_faithful_artifact_CAN_be_approved(tmp_path):
    """The positive control for the case above: the refusal must be caused by the
    failing diff, not by --approve being unreachable in general."""
    r = _run(tmp_path, "--goal", "ok", "--reviewer", "peer", "--closer", "alpha",
             "--source-file", str(_source(tmp_path)),
             "--artifact-file", str(_fixture(tmp_path, SOURCE_ENTITIES)),
             "--approve")
    assert r.returncode == 0, r.stdout + r.stderr
    payload = json.loads(r.stdout)
    assert payload["verdict"] == "APPROVE"
    assert payload["fidelity"]["passed"] is True
    assert payload["findings"] == []


def test_self_review_is_REFUSED_at_production_time(tmp_path):
    r = _run(tmp_path, "--goal", "ok", "--reviewer", "alpha", "--closer", "alpha",
             "--source-file", str(_source(tmp_path)),
             "--artifact-file", str(_fixture(tmp_path, SOURCE_ENTITIES)),
             "--approve", "--write")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "self-review" in r.stderr
    assert not (tmp_path / "audit-reports" / "close-reviews" / "ok.json").exists()


def test_no_verdict_is_invented(tmp_path):
    """Neither flag => nothing written, and a distinct rc. A default verdict in
    either direction would make silence indistinguishable from a decision."""
    r = _run(tmp_path, "--goal", "ok", "--reviewer", "peer",
             "--source-file", str(_source(tmp_path)),
             "--artifact-file", str(_fixture(tmp_path, SOURCE_ENTITIES)))
    assert r.returncode == 2, r.stdout + r.stderr
    assert json.loads(r.stdout)["verdict"] is None
    assert not (tmp_path / "audit-reports" / "close-reviews" / "ok.json").exists()


# ─── outcome 1, end to end: the artifact the GATE actually accepts ────────────

def test_producer_APPROVE_satisfies_the_gate_and_REJECT_does_not(tmp_path):
    """The contract that matters: schema-valid means the consumer accepts it.

    Both halves in one case on purpose — an APPROVE that releases proves the
    producer speaks the gate's schema, and a REJECT that does NOT release proves
    the release came from the verdict rather than from the artifact merely
    existing at the path.
    """
    goal = _coach_goal()
    src, good = _source(tmp_path), _fixture(tmp_path, SOURCE_ENTITIES)
    bad = _fixture(tmp_path, ARTIFACT_ENTITIES)

    assert _run_gate(tmp_path, goal, "alpha").returncode == 1, "no verdict must refuse"

    r = _run(tmp_path, "--goal", goal["goal_id"], "--reviewer", "peer-bravo",
             "--closer", "alpha", "--source-file", str(src),
             "--artifact-file", str(bad), "--reject", "--write")
    assert r.returncode == 3, r.stdout + r.stderr
    assert _run_gate(tmp_path, goal, "alpha").returncode == 1, (
        "a REJECT on file must not release the close")

    r = _run(tmp_path, "--goal", goal["goal_id"], "--reviewer", "peer-bravo",
             "--closer", "alpha", "--source-file", str(src),
             "--artifact-file", str(good), "--approve", "--write")
    assert r.returncode == 0, r.stdout + r.stderr
    g = _run_gate(tmp_path, goal, "alpha")
    assert g.returncode == 0, g.stdout + g.stderr
    assert "peer-bravo" in g.stdout


def test_a_produced_APPROVE_by_the_CLOSER_still_cannot_release(tmp_path):
    """Independence end to end. --closer is optional on the producer, so a caller
    can omit it and write a self-approval; the GATE must still refuse. This pins
    that the producer's refusal is a convenience, never the only defence."""
    goal = _coach_goal("producer-selfapprove")
    r = _run(tmp_path, "--goal", goal["goal_id"], "--reviewer", "alpha",
             "--source-file", str(_source(tmp_path)),
             "--artifact-file", str(_fixture(tmp_path, SOURCE_ENTITIES)),
             "--approve", "--write")   # no --closer: producer cannot know
    assert r.returncode == 0, r.stdout + r.stderr
    assert _run_gate(tmp_path, goal, "alpha").returncode == 1, (
        "the gate must refuse a verdict whose reviewer is the closing agent even "
        "when the producer was not told who the closer is")


# ─── outcome 3, the routing half: a REJECT reaches the GOAL, not just the ledger ──

def _verdict_file(tmp_path: Path, goal_id: str) -> Path:
    """Where CLOSE_REVIEW_LEDGER_DIR=tmp_path actually puts the artifact.

    Spelled out rather than guessed. The gate nests it under
    audit-reports/close-reviews/ (guard-599 — place under an EXISTING
    top-level dir), so a bare tmp_path/<goal>.json misses it — and the
    `assert not ...exists()` cases would then pass VACUOUSLY, which is the
    reason this is a helper instead of a literal repeated six times.
    """
    return tmp_path / "audit-reports" / "close-reviews" / f"{goal_id}.json"


def _producer_module():
    """close-review-verdict.py by path — its filename is hyphenated."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("close_review_verdict", PRODUCER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_route_command_calls_the_shared_writer_with_the_findings():
    """The argv, not a second append implementation.

    `goal-field-append.sh` owns the CAS read-modify-write for goal text fields;
    a hand-rolled append here would be a copy that drifts silently when that
    writer changes. So what is pinned is the CALL.
    """
    m = _producer_module()
    cmd = m.route_command("g-1-1", "world", "peer-bravo",
                          ["source-fidelity: asp-4401 absent"])
    assert cmd[0].endswith("bash") and cmd[0].startswith("/"), (
        "bash must be an absolute path, never a bare argv[0] (guard-580)")
    assert cmd[1].endswith("goal-field-append.sh")
    assert cmd[2:5] == ["--source", "world", "g-1-1"]
    assert cmd[5] == "progress_note"
    assert cmd[6].startswith("close-review-reject:")
    assert "asp-4401" in cmd[7] and "peer-bravo" in cmd[7]
    assert "blocked until" in cmd[7], "the note must say the close is blocked"


def test_the_marker_lets_a_RE_review_through_but_not_a_repeat():
    """Idempotency keyed on the FINDINGS, which is the load-bearing choice.

    A goal-keyed marker would make the first review's note permanent and swallow
    every later one — so rework that fixes defect A and exposes defect B would
    leave B unrecorded, which is precisely the case this routing serves.
    """
    m = _producer_module()
    first = ["source-fidelity: asp-4401 absent"]
    assert m.route_marker(first) == m.route_marker(list(first))     # repeat: same
    assert m.route_marker(first) != m.route_marker(["traceability: outcome 2 unmet"])


def test_routing_fires_on_a_written_REJECT_and_on_nothing_else(tmp_path, monkeypatch):
    """Three conditions in one case because it is their CONJUNCTION that matters:
    an APPROVE has nothing to rework, and a dry run must leave no trace anywhere.
    """
    m = _producer_module()
    calls = []
    monkeypatch.setattr(m, "route_findings", lambda *a, **k: calls.append(a) or True)
    monkeypatch.setenv("CLOSE_REVIEW_LEDGER_DIR", str(tmp_path))
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    src, good, bad = _source(tmp_path), _fixture(tmp_path, SOURCE_ENTITIES), \
        _fixture(tmp_path, ARTIFACT_ENTITIES)
    base = ["--goal", "g-9-9", "--reviewer", "peer", "--source-file", str(src)]

    assert m.main(base + ["--artifact-file", str(bad), "--reject"]) == 3
    assert calls == [], "a DRY RUN must route nothing"

    assert m.main(base + ["--artifact-file", str(good), "--approve", "--write",
                          "--route-to-goal", "world"]) == 0
    assert calls == [], "an APPROVE has nothing to rework"

    assert m.main(base + ["--artifact-file", str(bad), "--reject", "--write",
                          "--route-to-goal", "world"]) == 3
    assert len(calls) == 1, "a WRITTEN REJECT must route"
    goal_id, source, reviewer, findings = calls[0]
    assert (goal_id, source, reviewer) == ("g-9-9", "world", "peer")
    assert any("asp-4401" in f for f in findings)


def test_a_routing_failure_is_LOUD_and_never_crashes_the_verdict(monkeypatch, capsys):
    """Both failure branches, with the writer STUBBED — never invoked for real.

    An unrouted REJECT looks exactly like a goal nobody found defects in, so the
    failure must be audible; and the verdict artifact is already on disk by this
    point, so it must not raise.

    The stub is not fastidiousness: `goal-field-append.sh` writes the live goal
    store, and a test that calls it for its non-zero exit is one existing goal id
    away from appending a fabricated review note to real work.
    """
    m = _producer_module()
    finding = ["source-fidelity: asp-4401 absent"]

    def boom(*a, **k):
        raise OSError("no such file")
    monkeypatch.setattr(m.subprocess, "run", boom)
    assert m.route_findings("g-9-9", "world", "peer", finding) is False
    err = capsys.readouterr().err
    assert "ROUTING FAILED" in err and "OSError" in err and "NOT annotated" in err

    monkeypatch.setattr(m.subprocess, "run",
                        lambda *a, **k: m.subprocess.CompletedProcess(
                            a[0], 2, "", "goal g-9-9 not found"))
    assert m.route_findings("g-9-9", "world", "peer", finding) is False
    err = capsys.readouterr().err
    assert "ROUTING FAILED (rc=2)" in err and "goal g-9-9 not found" in err

    # PIN FLIPPED 2026-09-03 ( independent re-review, finding F4).
    # This asserted SILENCE on the empty case, on the reading that "nothing to
    # route is a no-op, not a failure report". The function's own docstring
    # says the opposite two lines up — "It must also never be silent: an
    # unrouted REJECT looks exactly like a goal nobody found defects in" — and
    # the docstring is right, because the ONLY call site is behind
    # `if args.route_to_goal and payload["verdict"] != "APPROVE"`: reaching
    # here means a caller explicitly asked to route rework from a
    # close-blocking verdict and there was none. That is the stall the module
    # docstring warns about ("blocks the close without routing the rework"),
    # not a benign no-op.
    #
    # The RETURN contract is unchanged (still False, still no raise) — only the
    # silence is gone. Flipping a pin is worth this much comment because the
    # move is indistinguishable from covering up a regression when it isn't
    # explained.
    monkeypatch.setattr(m.subprocess, "run", boom)
    assert m.route_findings("g-9-9", "world", "peer", []) is False
    err = capsys.readouterr().err
    assert "NOTHING ROUTED" in err and "no findings" in err


# ---------------------  re-review: F2, F3, F5 ---------------------

def test_reviewed_at_is_stamped_on_every_written_verdict(tmp_path):
    """F2. WHEN a review happened was unrecoverable from the artifact — absent
    from the producer, the gate and the skill alike. Nothing gates on it; it is
    the audit field that makes a ledger row datable at all."""
    r = _run(tmp_path, "--goal", "g-9-9", "--reviewer", "peer",
             "--source-file", str(_source(tmp_path)),
             "--artifact-file", str(_fixture(tmp_path, SOURCE_ENTITIES)),
             "--approve", "--write")
    assert r.returncode == 0, r.stdout + r.stderr
    rec = json.loads(_verdict_file(tmp_path, "g-9-9").read_text(encoding="utf-8"))
    # naive ISO-8601 to the second, the repo-wide stamp shape
    assert len(rec["reviewed_at"]) == 19 and rec["reviewed_at"][10] == "T"


def test_approve_with_notes_releases_the_close_AND_routes_its_notes(tmp_path, monkeypatch):
    """F3. The binary forced a reviewer with non-blocking observations to either
    REJECT a sound close or drop the observations.

    Both halves are asserted together because either alone is a different (and
    broken) feature: a third state the GATE does not recognise silently behaves
    as REJECT, and one that does not ROUTE leaves the notes in a ledger nobody
    reads — which is the same "reaches the ledger and nobody else" defect the
    REJECT routing already exists to fix.
    """
    m = _producer_module()
    calls = []
    monkeypatch.setattr(m, "route_findings", lambda *a, **k: calls.append((a, k)) or True)
    monkeypatch.setenv("CLOSE_REVIEW_LEDGER_DIR", str(tmp_path))
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    rc = m.main(["--goal", "g-9-9", "--reviewer", "peer",
                 "--source-file", str(_source(tmp_path)),
                 "--artifact-file", str(_fixture(tmp_path, SOURCE_ENTITIES)),
                 "--approve-with-notes", "--finding", "naming is inconsistent",
                 "--write", "--route-to-goal", "world"])
    assert rc == 0, "an approval is an approval — it must not exit like a REJECT"
    rec = json.loads(_verdict_file(tmp_path, "g-9-9").read_text(encoding="utf-8"))
    assert rec["verdict"] == "APPROVE_WITH_NOTES"
    assert len(calls) == 1, "notes that reach only the ledger reach nobody"
    assert calls[0][1]["verdict"] == "APPROVE_WITH_NOTES"


def test_the_gate_RELEASES_on_approve_with_notes(tmp_path):
    """The consumer half of F3, pinned separately. A third state the producer can
    write but the gate does not recognise would read as 'not APPROVE' and behave
    as a REJECT while looking like a third state — the exact trap the producer's
    own VERDICTS comment predicted."""
    # via the producer's own loader, so this pins the SAME module object the
    # producer consults — not a second copy that could diverge from it.
    g = _producer_module()._gate()
    assert g.releases_close("APPROVE_WITH_NOTES")
    assert g.releases_close("  approve_with_notes  "), "written by one hand, read by another"
    assert g.releases_close("APPROVE")
    assert not g.releases_close("REJECT")
    assert not g.releases_close(None)


def test_approve_with_notes_carrying_no_notes_is_REFUSED(tmp_path):
    """Same rule as the fidelity veto (guard-2564): the label may not assert more
    than the record carries. 'With notes' and no notes is a lie in the direction
    that matters, because it releases a close."""
    r = _run(tmp_path, "--goal", "g-9-9", "--reviewer", "peer",
             "--source-file", str(_source(tmp_path)),
             "--artifact-file", str(_fixture(tmp_path, SOURCE_ENTITIES)),
             "--approve-with-notes", "--write")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "no notes" in r.stderr
    assert not _verdict_file(tmp_path, "g-9-9").exists()


def test_approve_with_notes_is_still_subject_to_the_fidelity_VETO(tmp_path):
    """It is an APPROVAL, so the machine veto applies unchanged. Adding a third
    state must not open a lane around the one check that is mechanised."""
    r = _run(tmp_path, "--goal", "g-9-9", "--reviewer", "peer",
             "--source-file", str(_source(tmp_path)),
             "--artifact-file", str(_fixture(tmp_path, ARTIFACT_ENTITIES)),
             "--approve-with-notes", "--finding", "x", "--write")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "REFUSING to write APPROVE_WITH_NOTES" in r.stderr
    assert not _verdict_file(tmp_path, "g-9-9").exists()


def test_a_reviewer_may_REJECT_their_own_close(tmp_path):
    """F5. The independence guard used to fire on any --closer match regardless
    of verdict, so recording a REJECT on your own work was refused.

    Finding fault in your own close is the one direction that needs no
    independence — refusing it suppressed the RECORD, not the conflict. The gate
    itself has always scoped the same function to approvals only
    (`independence_defect(v, agent) if approved else None`), and the function's
    docstring opens 'Why this APPROVE verdict is not an INDEPENDENT review'.
    """
    r = _run(tmp_path, "--goal", "g-9-9", "--reviewer", "same", "--closer", "same",
             "--source-file", str(_source(tmp_path)),
             "--artifact-file", str(_fixture(tmp_path, ARTIFACT_ENTITIES)),
             "--reject", "--write")
    assert r.returncode == 3, r.stdout + r.stderr
    rec = json.loads(_verdict_file(tmp_path, "g-9-9").read_text(encoding="utf-8"))
    assert rec["verdict"] == "REJECT"


def test_self_APPROVAL_is_still_refused_in_both_approving_forms(tmp_path):
    """The other half of F5, and the reason the scoping is by RELEASING verdict
    rather than by the --approve flag: APPROVE_WITH_NOTES releases a close too,
    so it must not become a self-review bypass."""
    for extra in (["--approve"], ["--approve-with-notes", "--finding", "x"]):
        r = _run(tmp_path, "--goal", "g-9-9", "--reviewer", "same", "--closer", "SAME",
                 "--source-file", str(_source(tmp_path)),
                 "--artifact-file", str(_fixture(tmp_path, SOURCE_ENTITIES)),
                 *extra, "--write")
        assert r.returncode == 1, (extra, r.stdout + r.stderr)
        assert "self-review" in r.stderr
        assert not _verdict_file(tmp_path, "g-9-9").exists()
