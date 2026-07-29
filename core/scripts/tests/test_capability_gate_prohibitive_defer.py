"""test_capability_gate_prohibitive_defer.py — regression tests for g-115-3405.

Two independent correctness gaps in the defer enforcement chain, both fixed in
core/scripts/gates/capability.py. This file pins BOTH, plus the recall controls
that guard-958 makes mandatory.

GAP 1 (the dangerous one) — prohibitive-narrative verb inversion.
    A defer whose failure_reason PROHIBITS an action used to have that very
    action lifted out as the Unblock's title verb, producing e.g.
    "Unblock: delete for g-115-2050" from "Do not delete the archive ...".
    The emitted goal then instructed exactly the act the defer existed to
    prevent — a data-loss shape, not a cosmetic one.
    Fix: _is_prohibited_use() disqualifies a verb occurrence sitting under a
    prohibitive. When EVERY candidate verb is prohibited, action_verb stays
    None and the PRE-EXISTING g-115-1872 verbless-suppression withholds the
    Unblock. No new suppression path was added.

GAP 2 — the gate recommended a bypass flag the defer path ignores.
    The human-readable `reason` always said --override-agent-match. On the
    defer path that flag is not consulted at all (aspirations.py cmd_update_goal
    honours --force-defer), so an operator following the gate's own advice
    failed silently. Fix: caller_context, defaulting to "create-blocker" so
    every pre-existing caller is byte-identical.

GAP 2b (g-115-3813) — the caller_context fix reached only the DEAD call site.
    g-115-3405 passed --caller-context defer from aspirations.py, and the
    CLI-shaped tests for GAP 2 went green. But under daemon-only architecture
    the CLI defer path never executes: aspirations-update-goal.sh reaches the
    daemon, whose _capability_eval call in aspirations_write.py kept the
    "create-blocker" default. Every LIVE refusal therefore still named the
    inert flag. The GAP-2 tests could not see it because they never crossed the
    HTTP boundary where the wiring lives — so GAP 2b's test does.


WHY THE RECALL CONTROLS BELOW ARE NOT OPTIONAL (guard-958). When a
keyword-matching safety gate is tightened, the failure mode is losing RECALL on
genuine positives, and multi-keyword happy paths MASK it — the other keywords
still carry the match, so the regression is invisible. Each control here is
shaped so a SINGLE surviving verb is the sole thing under test, adjacent to the
new disqualifier. This is not theoretical: the first _PROHIBITIVE_PRE draft
failed 4 of 9 cases on exactly these, via a too-wide backward window that
swallowed the genuine action in "cannot reach the host, so restart the bridge".

CORPUS DEPENDENCY (deliberate, mirrors test_capability_gate_suggest_unblock.py):
the GAP-1 cases need a capability match to fire before there is any Unblock to
suppress, so they assert would_block is True with an explanatory message. If the
capability corpus stops matching these fixtures the assertion fails loudly and
the fixture text needs updating — which is the intended signal, not a flake.
A silent pass here would mean the test had stopped testing anything.

Written pytest-collectable ON PURPOSE. Many sibling capability-gate tests are
main()-style files that pytest collects ZERO tests from, so they never run in
the mandated `pytest core/scripts/tests` sweep (the g-115-2349 invisible-suite
class, which left 9 silent reds). These functions run in the ordinary suite.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
GATE_PY = CORE_SCRIPTS / "capability-gate.py"

sys.path.insert(0, str(CORE_SCRIPTS))

from _daemon_fixture import DaemonFixture  # noqa: E402


def _run_gate(failure_reason: str, *, caller_context: str | None = None,
              suggest_unblock: bool = True,
              for_goal_id: str = "g-TEST") -> dict:
    """Invoke capability-gate.py via subprocess and return the parsed JSON."""
    cmd = [
        sys.executable, str(GATE_PY),
        "--failure-reason", failure_reason,
        "--intended-participants", "user",
        "--output", "json",
    ]
    if caller_context is not None:
        cmd.extend(["--caller-context", caller_context])
    if suggest_unblock:
        cmd.extend(["--suggest-unblock", "--for-goal-id", for_goal_id])
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:  # pragma: no cover - diagnostic path
        pytest.fail(
            f"gate did not emit JSON.\nstdout={proc.stdout!r}\nstderr={proc.stderr!r}"
        )


def _require_match(payload: dict, label: str) -> None:
    """The GAP-1 cases are vacuous unless a capability actually matched."""
    assert payload.get("would_block") is True, (
        f"{label}: expected would_block=True — this case can only test verb "
        f"SUPPRESSION if a capability match fires first, otherwise there is no "
        f"Unblock to suppress and the assertion below would pass vacuously. "
        f"got would_block={payload.get('would_block')!r} "
        f"match_count={payload.get('match_count')!r}. If the capability corpus "
        f"changed, update the fixture text rather than deleting this assert."
    )


# ---------------------------------------------------------------- GAP 1

def test_prohibitive_defer_emits_no_unblock_verb():
    """The canonical g-115-2050 / g-115-3404 shape must suppress the Unblock.

    Previously produced "Unblock: delete for <goal>" — instructing the deletion
    the defer exists to prevent.
    """
    d = _run_gate("Do not delete the archive before the retention config is verified.")
    _require_match(d, "canonical prohibitive defer")
    assert d.get("unblock_suggested") is not True, (
        f"prohibitive defer must not suggest an Unblock; got "
        f"unblock_suggested={d.get('unblock_suggested')!r} "
        f"title={d.get('unblock_title')!r}"
    )
    title = d.get("unblock_title") or ""
    for prohibited in ("delete", "purge", "destroy", "wipe"):
        assert prohibited not in title.lower(), (
            f"emitted title names the PROHIBITED action {prohibited!r}: {title!r} "
            f"— this is the g-115-3405 data-loss inversion regressing."
        )


@pytest.mark.parametrize("reason,expected_verb", [
    ("Do not delete the archive. Push the fix.", "push"),
    ("Never purge the store. Deploy it.", "deploy"),
])
def test_recall_preserved_for_genuine_action_beside_a_prohibition(reason, expected_verb):
    """guard-958 recall control — a SINGLE genuine verb beside a prohibition.

    The prohibition must disqualify only its own verb, never the neighbouring
    legitimate action. These are deliberately single-verb so no other keyword
    can mask a recall loss.
    """
    d = _run_gate(reason)
    _require_match(d, f"recall control ({expected_verb})")
    assert d.get("unblock_suggested") is True, (
        f"recall loss: genuine action {expected_verb!r} was suppressed by the "
        f"prohibitive disqualifier. reason={reason!r} payload_title="
        f"{d.get('unblock_title')!r}"
    )
    title = (d.get("unblock_title") or "").lower()
    assert expected_verb in title, (
        f"expected the genuine action {expected_verb!r} in the title, got "
        f"{d.get('unblock_title')!r}"
    )


# ---------------------------------------------------------------- GAP 2

def _reason_of(payload: dict) -> str:
    return payload.get("reason") or ""


def test_defer_context_recommends_force_defer():
    """On the defer path the only flag that works is --force-defer."""
    d = _run_gate("Do not delete the archive. Push the fix.", caller_context="defer")
    reason = _reason_of(d)
    assert "--force-defer" in reason, (
        f"defer path must recommend --force-defer; got reason={reason!r}"
    )
    assert "--override-agent-match" not in reason, (
        f"defer path must NOT recommend --override-agent-match — that flag is "
        f"not consulted there, so following it fails silently. reason={reason!r}"
    )


@pytest.mark.parametrize("ctx", [None, "create-blocker"])
def test_create_blocker_context_is_byte_compatible(ctx):
    """Default and explicit create-blocker keep the pre-g-115-3405 wording.

    Passing None exercises the argparse default, which is what every existing
    caller hits — that is the backwards-compatibility guarantee.
    """
    d = _run_gate("Do not delete the archive. Push the fix.", caller_context=ctx)
    reason = _reason_of(d)
    assert "--override-agent-match" in reason, (
        f"create-blocker path (ctx={ctx!r}) must keep recommending "
        f"--override-agent-match; got reason={reason!r}"
    )
    assert "--force-defer" not in reason, (
        f"create-blocker path (ctx={ctx!r}) must not recommend --force-defer; "
        f"got reason={reason!r}"
    )


# ------------------------------------------- GAP 2 — DAEMON WIRING (g-115-3813)
# The two tests above reach the gate through its CLI entry point, and they PASS
# whether or not the daemon passes caller_context. That is precisely how the gap
# below survived g-115-3405 undetected: the fix was applied to
# aspirations.py::_run_capability_gate_for_defer, verified green by CLI-shaped
# tests, and never reached the only call site that actually executes.
#
# Under daemon-only architecture (35 wrappers, no Python CLI fallback —
# .claude/rules/no-python-cli-fallback.md) `aspirations-update-goal.sh` reaches
# the daemon endpoint, never the CLI function. So every LIVE defer refusal kept
# rendering `--override-agent-match`, a flag that wrapper plumbs ONLY so argparse
# can redirect and explicitly does NOT honour on this path (g-115-2814).
# Measured 2026-07-29 on g-326-63: following the gate's own message verbatim
# returned override_applied=null plus a second refusal naming no working escape.
#
# This test therefore goes through the HTTP endpoint, because the defect was in
# the WIRING and only a test that crosses the same boundary can see it.

def _seed_defer_world(tmp: Path) -> Path:
    """Temp world holding one pending goal to attempt a narrative defer on."""
    world = tmp / "world"
    world.mkdir()
    asp = {
        "id": "asp-100",
        "title": "daemon defer caller-context wiring",
        "motivation": "Pin that the daemon defer path identifies itself",
        "scope": "project",
        "priority": "MEDIUM",
        "status": "active",
        "created": "2026-07-29T00:00:00",
        "goals": [{
            "id": "g-100-01",
            "title": "Seed goal",
            "description": "Target for the defer attempt.",
            "status": "pending",
            "priority": "MEDIUM",
            "blocked_by": [],
            "verification": {"outcomes": ["x"], "checks": [],
                             "preconditions": []},
            "origin_signal": "user_directive",
            "participants": ["agent"],
        }],
    }
    (world / "aspirations.jsonl").write_text(
        json.dumps(asp, ensure_ascii=False) + "\n", encoding="utf-8")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")

    agent_dir = tmp / "alpha"
    (agent_dir / "session").mkdir(parents=True)
    (agent_dir / "aspirations.jsonl").write_text("", encoding="utf-8")
    (agent_dir / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world


def _daemon_defer(port: int, goal_id: str, reason: str) -> tuple[int, dict]:
    url = (f"http://127.0.0.1:{port}/v1/aspirations/update-goal"
           f"?id={goal_id}&field=defer_reason&source=world")
    req = urllib.request.Request(
        url, data=json.dumps(reason).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Ayoai-Agent", "alpha")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:  # pragma: no cover - diagnostic path
            pytest.fail(f"daemon returned non-JSON {e.code}: {raw!r}")


def test_daemon_defer_path_recommends_the_flag_it_honours():
    """A live daemon defer refusal must name --force-defer, not the inert flag.

    Corpus dependency is the same deliberate one the GAP-1 cases carry: the
    refusal only exists if a capability matches first, so a 200 here means the
    fixture stopped matching and needs updating — not that the wiring is fine.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = _seed_defer_world(Path(tmpd))
        with DaemonFixture(world) as df:
            status, body = _daemon_defer(
                df.port, "g-100-01",
                "Do not delete the archive. Push the fix.")

    assert status == 400 and body.get("error") == "capability_blocked", (
        f"expected the capability gate to refuse this defer — without a "
        f"refusal there is no remediation text to inspect and the assertions "
        f"below would pass vacuously. got status={status} body={body!r}. If "
        f"the capability corpus changed, update the fixture text."
    )
    reason = _reason_of(body.get("gate_output") or {})
    assert "--force-defer" in reason, (
        f"the daemon defer path must recommend --force-defer — the flag it "
        f"actually reads (X-Ayoai-Force-Defer, per aspirations_write.py). "
        f"Missing it means the _capability_eval call lost caller_context='defer' "
        f"and fell back to the create-blocker default. reason={reason!r}"
    )
    assert "--override-agent-match" not in reason, (
        f"the daemon defer path must NOT recommend --override-agent-match: it "
        f"is the CREATE_BLOCKER bypass and is deliberately not honoured here "
        f"(g-115-2814), so an operator following it gets override_applied=null "
        f"and a silent second refusal. reason={reason!r}"
    )
