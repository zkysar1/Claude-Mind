"""The 409 write_conflict message must not assert what it never observed.

g-115-4314. `mind_api/src/server.py` returns this string on every
optimistic-concurrency failure. The handler does NOT read remote state, so it
cannot know WHY the version precondition failed — and the two candidate causes
need OPPOSITE responses:

  * a live remote change      -> retry is correct
  * a STALE LOCAL MIRROR      -> retry conflicts identically, forever

The original text picked one and prescribed a remedy from it: "remote changed
between the in-lock read and the write ... safe to retry". Both halves were
derived from the exception TYPE, neither measured.

Measured counter-case (g-115-3782, 2026-07-29): the remote was STATIC —
double-HEAD returned the same version 15s apart — while local sat 11,057 bytes
behind. 8 attempts over ~2.5 minutes of escalating backoff all conflicted. The
message actively steered toward the retry playbook guard-908 exists to prevent,
and because it asserted the OPPOSITE of persistence, guard-908's trigger could
not fire.

This test pins the CONTRACT, not the prose: the message may be reworded freely,
but it must never re-acquire an unhedged causal claim or an unconditional
retry-safety promise, and it must keep handing over the discriminator.

A note on why the forbidden-substring style is right here rather than lazy: the
defect class is a CONFIDENT ASSERTION creeping back in. Pinning the exact new
string would fail on every harmless rewording and get "fixed" by updating the
expected value — which is precisely how the old assertion would return.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SERVER = REPO / "mind_api" / "src" / "server.py"


def _conflict_message():
    """Extract the write_conflict Response.error(...) message literal."""
    src = SERVER.read_text(encoding="utf-8")
    i = src.find('"write_conflict"')
    assert i > 0, "write_conflict branch not found in server.py"
    # Collect the adjacent string literals that follow, up to the closing paren.
    tail = src[i:i + 2000]
    end = tail.find(")\n")
    body = tail[:end if end > 0 else len(tail)]
    parts = re.findall(r'"([^"]*)"', body)
    # Join with "" — Python concatenates ADJACENT string literals with no
    # separator, and each literal already carries its own trailing space. A
    # " ".join here injects doubles mid-phrase ("STALE LOCAL  MIRROR"), which
    # then fails a substring assertion about a message that is perfectly fine.
    # Caught by this suite on first run; the tempting fix was to loosen the
    # assertion, which would have been fixing the correct side of the compare.
    joined = "".join(p for p in parts if p != "write_conflict")
    return re.sub(r"\s+", " ", joined).strip()


def test_does_not_promise_retry_safety():
    msg = _conflict_message().lower()
    assert "safe to retry" not in msg, (
        "the handler cannot know retry is safe — it never reads remote state. "
        "A stale local mirror raises this same error and retries forever "
        "(g-115-3782)."
    )


def test_does_not_assert_an_unobserved_cause():
    msg = _conflict_message().lower()
    # An unhedged "remote changed" is the specific claim that was never measured.
    unhedged = re.search(r'remote changed between', msg)
    assert not unhedged, (
        "'remote changed' states a cause this handler never observed. Name it "
        "as a CANDIDATE alongside the stale-mirror candidate, or omit it."
    )


def test_states_what_is_actually_known():
    msg = _conflict_message().lower()
    assert "did not land" in msg, (
        "persistence IS knowable from the exception and callers depend on it — "
        "guard-908's trigger keys on the write's fate, so this must be explicit"
    )


def test_names_both_candidates_and_the_discriminator():
    msg = _conflict_message().lower()
    assert "stale local mirror" in msg, "the non-obvious candidate must be named"
    assert "head the remote twice" in msg, (
        "the message must hand over the discriminator, not just hedge. Hedging "
        "alone would replace confident-and-wrong with useless."
    )


def test_message_is_reachable_from_the_conflict_branch_only():
    """Guard against the string drifting to a branch with different semantics."""
    src = SERVER.read_text(encoding="utf-8")
    assert src.count('"write_conflict"') == 1, (
        "more than one write_conflict emitter in server.py — a second one would "
        "need this same contract, and this test only covers the first"
    )
