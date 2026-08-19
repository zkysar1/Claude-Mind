#!/usr/bin/env python3
"""Pins the DECISION that merge_aspirations is deliberately NOT status-monotonic
(g-115-6486).

WHY A TEST AND NOT JUST A COMMENT. The goal that produced this file arrived with
a recommended fix: adopt `_PIPELINE_STAGE_RANK`'s shape — "a monotonic
aspiration status rank where retired/archived dominates active" — so that
archival survives a peer's concurrent bump. The reasoning is sound for
pipeline.jsonl and WRONG here, because the two lifecycles differ in exactly the
property the rank encodes:

  * a hypothesis lifecycle is monotonic BY DESIGN (pipeline-move only ever goes
    forward), so ranking archived above active can never lose information;
  * an aspiration may legitimately REOPEN. `aspirations.py::_check_not_archived`
    explicitly permits modifying a live copy whose id is ALSO in the archive,
    and `_aspirations_resurrection.classify` exempts that case by name as
    ``post_archive_work`` — "a legitimate reopen (asp-328 shape) that keeps the
    aspiration live and is never touched".

So a rank would force every reopened aspiration back to terminal on the next
sync, on every box, silently — destroying live non-terminal work in the fleet's
primary queue. Measured 2026-08-17 (alpha worker Body, hostname cc-08, uname -r
6.8.0-137-generic, own-cloud): asp-328 is live=active / archive=completed with
non-terminal goals inside, and is the ONLY remaining live/archive overlap.

A prose comment cannot stop that change from being made; these assertions can.
Each one goes RED against a rank implementation and stays green under the
current union, which is the whole point — this file exists to fail LOUDLY the
next time someone applies the pipeline precedent here.
"""

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

from coordination_merge import merge_aspirations  # noqa: E402


def blob(*recs):
    return ("".join(json.dumps(r, ensure_ascii=True) + "\n" for r in recs)).encode()


def parse(raw):
    return [json.loads(line) for line in raw.decode().splitlines() if line.strip()]


REOPENED = {
    "id": "asp-328",
    "status": "active",
    "last_selected": "2026-08-17T10:00:00",
    "goals": [{"id": "g-328-48", "status": "in-progress"}],
}
STALE_TERMINAL = {
    "id": "asp-328",
    "status": "completed",
    "last_selected": "2026-07-12T10:00:00",
    "goals": [],
}


def test_reopened_aspiration_survives_a_stale_terminal_peer_copy():
    """THE LOAD-BEARING ASSERTION. A rank making completed dominate active
    inverts this, and it is the single change this file exists to refuse."""
    for left, right in ((REOPENED, STALE_TERMINAL), (STALE_TERMINAL, REOPENED)):
        out = parse(merge_aspirations(blob(left), blob(right)))
        assert len(out) == 1, out
        assert out[0]["status"] == "active", (
            "a stale terminal peer copy overrode a live reopen — this is the "
            "pipeline-monotonic-rank transfer the docstring refuses; see "
            "_aspirations_resurrection.classify post_archive_work"
        )


def test_reopened_aspirations_live_goals_are_not_dropped():
    """DOES NOT DISCRIMINATE against the rank — measured, and recorded because
    the first version of this file claimed it did.

    The reasoning that produced it was: the rank makes the terminal side the
    merge BASE, that side's `goals` is empty, so the reopen's in-flight work
    vanishes with its status. Plausible and WRONG. `_merge_aspiration_record`
    computes goals as `_merge_goals(a.get("goals"), b.get("goals"), ...)` — a
    union over BOTH arguments, independent of which side won the base pick — so
    g-328-48 survives the mutation and this test stayed GREEN while the pin
    above went red (mutation run 2026-08-17: `F....`).

    Kept anyway, because it pins a real invariant that a future goals-touching
    change could break, and because a test that is honest about what it does NOT
    catch is worth more than one silently assumed to catch everything: exactly
    ONE assertion in this file discriminates against the rank, and it is the one
    above. Do not read a green run here as evidence the rank is absent."""
    out = parse(merge_aspirations(blob(REOPENED), blob(STALE_TERMINAL)))
    ids = {g["id"] for g in out[0].get("goals", [])}
    assert "g-328-48" in ids, f"live non-terminal goal lost in merge: {out[0]}"


def test_merge_is_commutative_and_idempotent_on_the_reopen_pair():
    """Any replacement rule must keep these. They are the properties the fenced
    PUT relies on, so a fix that breaks them breaks sync itself — not just this
    lane."""
    ab = merge_aspirations(blob(REOPENED), blob(STALE_TERMINAL))
    ba = merge_aspirations(blob(STALE_TERMINAL), blob(REOPENED))
    assert ab == ba, "merge_aspirations is not commutative on the reopen pair"
    assert merge_aspirations(ab, ab) == ab, "not idempotent"
    assert merge_aspirations(ab, blob(STALE_TERMINAL)) == ab, (
        "re-merging a stale terminal copy into a converged result changed it — "
        "the resurrection would recur on every subsequent sync"
    )


def test_union_still_keeps_a_one_sided_aspiration():
    """The union behaviour itself is NOT what this file objects to — it is
    correct for every non-removal case, and the resurrection remedy lives
    outside this function by design (archive_sweep -> _reconcile_resurrected).
    Pinned so a future 'fix' does not narrow the union while chasing the delete
    case."""
    other = {"id": "asp-999", "status": "active", "goals": []}
    out = parse(merge_aspirations(blob(REOPENED), blob(other)))
    assert {r["id"] for r in out} == {"asp-328", "asp-999"}


def test_docstring_still_carries_the_refusal():
    """A behavioural pin cannot explain WHY, and the next reader arrives at this
    function through the goal that recommends the rank. If the rationale is
    deleted the assertions above look like arbitrary fixtures, so the refusal
    and its reason are pinned together."""
    doc = merge_aspirations.__doc__ or ""
    assert "DO NOT TRANSFER THE PIPELINE PRECEDENT HERE" in doc
    assert "post_archive_work" in doc
    assert "_reconcile_resurrected" in doc, (
        "the docstring must keep naming the component that DOES own the "
        "resurrection remedy — otherwise this reads as 'known bug, unfixed'"
    )
