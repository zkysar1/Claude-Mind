"""test_completed_not_committed_backport_merge.py — regression for  defect C.

A BACKPORT MERGE (direction target->source, e.g. main->dev) can NEVER appear as a
new commit on the target branch: its content is already there, which is exactly
what made it mergeable in that direction. So `classify_stranded` asking "is this
commit on the default branch?" about one is asking a question with a single
possible answer, and the permanent NO scored it as stranded work.

Measured on g-115-9021: the flagged commit was a main->dev backport, the two files
it touched were byte-identical between origin/main and origin/dev, and the content
had already shipped via PR #412 (merged to main 2026-08-31T12:03:21Z). The sweep's
prescribed remedy for a stranded entry is to MERGE the named pull request — on a
push-to-main auto-deploying repo that ships to production against guard-5389 and
guard-5514, which is why this class is HIGH and not cosmetic.

SUPPRESSION DIRECTION, and why most of these cases assert a REFUSAL. Like the
squash-merge carve-out beside it, this lane SUPPRESSES, so its failure mode is
hiding a genuinely stranded deliverable — the very defect the sweep exists to
catch. Only a definite True may suppress; None and False must both keep flagging.
The true-positive path (outcome 3 of the goal: the g-115-9026 case, where the
default branch and the delivery branch coincide) is pinned here explicitly.
"""

from __future__ import annotations

import datetime
import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "cnc_backport", CORE_SCRIPTS / "completed-not-committed-sweep.py")
cnc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cnc)

NOW = datetime.datetime(2026, 9, 5, 12, 0, 0)
BACKPORT_SHA = "23a049d711112222333344445555666677778888"
REAL_SHA = "408a7a8999990000aaaabbbbccccddddeeee1111"


def _goal(shas=(BACKPORT_SHA,), gid="g-115-9021"):
    return {
        "id": gid,
        "_source": "world",
        "_aspiration_id": "asp-115",
        "status": "completed",
        "completed_at": "2026-09-05T06:00:00",
        "title": "backport the commons note",
        "verification": {"outcomes": [f"commit {s}" for s in shas]},
    }


def _open_pr(number=439):
    """An OPEN pull request old enough to clear min_pr_age_hours — the shape that
    produces the DANGEROUS stranded_open_pr remedy ('merge PR #N')."""
    return {"state": "OPEN", "number": number,
            "url": f"https://example.invalid/pull/{number}",
            "title": "unrelated work", "created_at": "2026-09-01T09:00:00",
            "merge_commit_sha": None, "draft": False, "body": ""}


def _classify(backport_status, shas=(BACKPORT_SHA,), pr=None):
    pr_record = _open_pr() if pr is None else pr
    return cnc.classify_stranded(
        _goal(shas), NOW,
        sha_status={s: True for s in shas},
        default_status={s: False for s in shas},
        pr_status={s: pr_record for s in shas},
        backport_status=backport_status)


# --- the fix itself --------------------------------------------------------

def test_backport_merge_is_not_stranded():
    """The regression. A True in the map means the sha is a merge whose SECOND
    parent already sits on the default branch — nothing to strand."""
    assert _classify({BACKPORT_SHA: True}) is None


def test_ungated_behaviour_still_flags_it():
    """POSITIVE CONTROL for the case above: without the map the SAME inputs must
    still produce a stranded entry. Without this, a classify_stranded that had
    stopped flagging entirely would pass the test above (guard-1220)."""
    entry = _classify(None)
    assert entry is not None
    assert entry["reason"] == "stranded_open_pr"


# --- the suppression must be narrow (outcome 3: keep the true positives) ----

def test_ordinary_off_default_commit_is_still_flagged():
    """A normal single-parent commit probes False and MUST keep flagging — this
    is the g-115-9026 true-positive path the goal requires be preserved."""
    entry = _classify({REAL_SHA: False}, shas=(REAL_SHA,))
    assert entry is not None
    assert entry["reason"] == "stranded_open_pr"


def test_undeterminable_probe_still_flags():
    """None means the probe could not judge. It must NOT suppress: a probe
    failure turning into a blessing is how a stranded deliverable goes silent."""
    entry = _classify({BACKPORT_SHA: None})
    assert entry is not None
    assert entry["reason"] == "stranded_open_pr"


def test_absent_map_is_byte_identical_to_the_old_caller():
    """Backward compatibility: a pre- caller passes nothing and must
    behave exactly as before (the merge_default_status precedent)."""
    without = _classify(None)
    empty = _classify({})
    assert without is not None and empty is not None
    assert without["reason"] == empty["reason"]


def test_one_backport_does_not_bless_a_genuinely_stranded_sibling():
    """The sibling case that matters most. A goal naming TWO off-default shas,
    one a backport and one real, is still stranded on the real one."""
    entry = _classify({BACKPORT_SHA: True, REAL_SHA: False},
                      shas=(BACKPORT_SHA, REAL_SHA))
    assert entry is not None
    assert entry["reason"] == "stranded_open_pr"


# --- the probe's own parsing contract --------------------------------------

class _FakeGit:
    """Records calls and replays scripted (rc, stdout) by git subcommand."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, repo, *args):
        self.calls.append(args)
        for key, val in self.responses.items():
            if key in args:
                return val
        return (0, "")


def _probe(monkeypatch, rev_list, contains=(0, "  origin/main")):
    fake = _FakeGit({"cat-file": (0, ""),
                     "rev-list": rev_list,
                     "branch": contains})
    monkeypatch.setattr(cnc, "_git", fake)
    return cnc.probe_sha_backport_merge(
        BACKPORT_SHA, [Path("/repo")], {"/repo": "origin/main"}), fake


def test_probe_reads_the_SECOND_parent_not_the_first(monkeypatch):
    """`rev-list --parents -n 1` prints '<sha> <p1> <p2>'. The first parent is the
    branch merged INTO; only the second can make this a backport. Testing 'any
    parent contained' would swallow ordinary feature merges once dev reached main."""
    verdict, fake = _probe(monkeypatch, (0, f"{BACKPORT_SHA} p1sha p2sha"))
    assert verdict is True
    contains_calls = [c for c in fake.calls if "branch" in c]
    assert contains_calls, "the containment probe never ran"
    assert "p2sha" in contains_calls[0], (
        "must test the SECOND parent; testing p1 would flag ordinary merges")


def test_probe_returns_false_for_a_normal_single_parent_commit(monkeypatch):
    verdict, _ = _probe(monkeypatch, (0, f"{BACKPORT_SHA} p1sha"))
    assert verdict is False


def test_probe_returns_false_when_second_parent_is_not_on_default(monkeypatch):
    """A merge bringing in genuinely NEW work is not a backport."""
    verdict, _ = _probe(monkeypatch, (0, f"{BACKPORT_SHA} p1sha p2sha"),
                        contains=(0, ""))
    assert verdict is False


def test_probe_returns_none_on_rev_list_error(monkeypatch):
    """rc!=0 is undeterminable, never a verdict — the reason this probe uses the
    `branch --contains` family rather than `merge-base --is-ancestor`, which
    reports 'not an ancestor' and an internal error with the same nonzero rc."""
    verdict, _ = _probe(monkeypatch, (128, ""))
    assert verdict is None


def test_probe_returns_none_on_containment_error(monkeypatch):
    verdict, _ = _probe(monkeypatch, (0, f"{BACKPORT_SHA} p1sha p2sha"),
                        contains=(129, ""))
    assert verdict is None
