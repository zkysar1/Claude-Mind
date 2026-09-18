"""Stop-time runner-lease renewal — graceful-stop D3.5 ().

THE DEFECT THIS PINS. D1 of the graceful stop sets agent-state=IDLE, and
`heartbeat-tick.sh` REFUSES to tick in IDLE (its `exit 2` desync gate,
guard-543). So from D1 onward nothing renews the cross-machine runner lease:
a D4 consolidation running past `runner_heartbeat` stale_seconds (3900 s) —
or a `--resume` started more than ~65 min after the last loop tick — loses
the claim, and every fenced `agents/<agent>/**` write then fails `no_claim`.
Measured 2026-09-15 on cc-05: a stop that produced NO handoff at all.

THE REMEDY, shipped as D3.5 (8bca757848): one standalone fail-open
`runner-claim.sh heartbeat` between D3 and D4, so D4 starts with a full
lease window. `runner-claim.sh heartbeat` is NOT `heartbeat-tick.sh
--bypass-state`: it is not agent-state-gated, and it is token-conditional,
so it can only refresh a claim this box already holds (rb-10942).

WHAT THESE TESTS PIN, and why each one can fail independently:
  1. D3.5 exists and sits strictly BETWEEN D3 and D4. Order is the whole
     point — a renewal after D4 renews nothing that D4 needed.
  2. D3.5 calls `runner-claim.sh heartbeat`, on its OWN Bash line with no
     precondition chained into the same invocation (guard-6424/guard-409),
     and NOT `heartbeat-tick.sh` — which is the call that cannot work here.
  3. `runner-claim.sh` has NO agent-state gate. This is the property D3.5
     RESTS on, and it lives in a different file, so nothing else would fail
     if someone added one: D3.5 would simply stop renewing under IDLE and
     the 2026-09-15 defect would return silently.
  4. `runner-claim.sh heartbeat` is token-conditional (it resolves the
     framework-owned token from agents/<agent>/session/runner-token and
     refuses without one). That is what makes the stop-time renewal safe to
     ship at all — it cannot steal a peer's claim.

WHY STRUCTURAL AND NOT BEHAVIOURAL. The remedy is a SKILL.md step, i.e.
pseudocode executed by the model, so there is no function to call. Test 3
below is deliberately source-level too, because the invariant it pins is an
ABSENCE, and an absence is not observable from a single successful run.
Stated rather than implied: these tests prove the remedy is PRESENT and its
precondition HOLDS. They do NOT prove a real long stop keeps its claim —
that is verification outcome 1 of g-115-10040 and needs a live /stop, which
the loop may not self-initiate.

COMMENT-STRIPPING IS LOAD-BEARING in test 3. `runner-claim.sh` mentions IDLE
seven times in its own comments (it documents the IDLE->RUNNING CAS), so a
naive grep for a state gate matches prose and the test would be red on a
healthy file. The strip is what makes the assertion about CODE.

Companion: core/config/rationale/graceful-stop-lease-renewal.md.
The behavioural half — heartbeat-tick.sh's IDLE refusal must NOT become
stop-scoped — is pinned in test_body_heartbeat_writer.py, which already owns
the relocated-PROJECT_ROOT fixture for that script (guard-598: one sandbox
cp list, not two).

Run: py -3 core/scripts/tests/test_graceful_stop_lease_renewal.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SKILL = REPO / ".claude" / "skills" / "aspirations-graceful-stop" / "SKILL.md"
RUNNER_CLAIM = REPO / "core" / "scripts" / "runner-claim.sh"

_COMMENT_LINE = re.compile(r"^[ \t]*#.*$", re.MULTILINE)


def _strip_comments(src: str) -> str:
    """Blank out whole-line shell comments, keeping line numbering intact."""
    return _COMMENT_LINE.sub("", src)


def _step_line(lines: list[str], marker: str) -> int:
    """Index of the line opening step `marker` (e.g. '# D3:'). Fails loudly."""
    for i, ln in enumerate(lines):
        if ln.strip().startswith(marker):
            return i
    raise AssertionError(
        f"graceful-stop SKILL.md has no step opening with {marker!r} — the "
        "D1-D7 sequence this test reasons about is not present in the shape "
        "expected. Re-read the file before loosening this test.")


def test_d35_sits_strictly_between_d3_and_d4():
    lines = SKILL.read_text(encoding="utf-8").splitlines()
    d3 = _step_line(lines, "# D3:")
    d35 = _step_line(lines, "# D3.5:")
    d4 = _step_line(lines, "# D4:")
    assert d3 < d35 < d4, (
        "D3.5 (stop-time lease renewal) must sit strictly between D3 and D4. "
        f"Got D3={d3 + 1}, D3.5={d35 + 1}, D4={d4 + 1}. A renewal that runs "
        "after D4 renews nothing D4 needed — D4 is the long phase whose "
        "agents/<agent>/** writes get fenced no_claim when the lease lapses.")


def test_d35_calls_runner_claim_heartbeat_on_its_own_line():
    lines = SKILL.read_text(encoding="utf-8").splitlines()
    d35, d4 = _step_line(lines, "# D3.5:"), _step_line(lines, "# D4:")
    block = [ln for ln in lines[d35:d4] if not ln.strip().startswith("#")]
    calls = [ln for ln in block if "runner-claim.sh heartbeat" in ln]
    assert len(calls) == 1, (
        "the D3.5 block must carry exactly one `runner-claim.sh heartbeat` "
        f"call; found {len(calls)} in {block!r}")
    call = calls[0]
    assert "heartbeat-tick.sh" not in call, (
        "D3.5 must NOT use heartbeat-tick.sh — that is the script which "
        "refuses in IDLE, which is precisely why the lease lapsed "
        "(and --bypass-state is reserved for /start).")
    # guard-6424 / guard-409: a lease acquisition must not share a Bash
    # invocation with the precondition that gates it. One call, no `&&`, no
    # chained status read, no command substitution feeding it.
    assert "&&" not in call and "$(" not in call, (
        "the D3.5 renewal must be its OWN call with nothing chained into the "
        f"same invocation (guard-6424/guard-409). Got: {call!r}")


def test_runner_claim_has_no_agent_state_gate():
    """The ABSENCE D3.5 rests on: runner-claim.sh must stay state-blind.

    If a future edit adds an agent-state read here, D3.5 silently stops
    renewing (D1 has already set IDLE) and the 2026-09-15 no-handoff stop
    returns with nothing else going red.
    """
    src = RUNNER_CLAIM.read_text(encoding="utf-8")
    code = _strip_comments(src)
    # Positive control: stripping must not have emptied the file (guard-2421).
    assert len([ln for ln in code.splitlines() if ln.strip()]) > 50, (
        "comment-stripping left almost no code — the strip is broken, so the "
        "absence assertion below would pass vacuously")
    # Control that the strip is doing real work: IDLE IS mentioned, in prose.
    assert "IDLE" in src, "expected runner-claim.sh to document IDLE at all"
    hits = [ln.strip() for ln in code.splitlines()
            if "session-state-get" in ln or "session/agent-state" in ln]
    assert hits == [], (
        "runner-claim.sh must not gate on agent-state — graceful-stop D3.5 "
        "calls it AFTER D1 has set IDLE, so a state gate here makes the "
        f"stop-time lease renewal a no-op. Offending code line(s): {hits}")


def test_runner_claim_heartbeat_is_token_conditional():
    """Token-conditional is what makes a stop-time renewal safe to ship.

    It can only refresh a claim this box already holds, so it can never
    steal a peer's — the reason candidates (a) and (c) (weaken the IDLE
    gate / teach the no_claim fence to accept a stale self-holder) were
    rejected in favour of this primitive.
    """
    code = _strip_comments(RUNNER_CLAIM.read_text(encoding="utf-8"))
    assert "session/runner-token" in code, (
        "runner-claim.sh must resolve the framework-owned token from "
        "agents/<agent>/session/runner-token")
    assert "no runner token" in code, (
        "runner-claim.sh must REFUSE when no token resolves — a renewal that "
        "proceeds token-less is no longer token-conditional")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  [PASS] {name}")
            except AssertionError as exc:
                failures += 1
                print(f"  [FAIL] {name}: {exc}")
    sys.exit(1 if failures else 0)
