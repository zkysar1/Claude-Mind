"""`backend-cat.sh head --exit-on-drift` — the verdict becomes the exit code ().

WHY THE FLAG EXISTS. `head` already printed a drift verdict and always exited 0,
so the only way for a cadence to gate on it was to grep `[match]` out of prose —
a hand-written parser over a framework script's output, which is the guard-2298
hazard verbatim. Nothing gated, so nothing did: the own-cloud tree-node merge
REFUSES a same-heading divergence by design (guard-4778) and that refusal is
SILENT TO THE WRITER, so alpha's directive-lane series shard sat wedged ~4 days
across FIVE cadence passes with five readings live only on one box's disk.

WHAT THESE TESTS CAN AND CANNOT COVER, stated plainly because a checker that
reports what it declined to look at as a pass is the defect this flag exists to
break (guard-1760). Under `STORAGE_BACKEND=local` the file IS the store, so
drift is unrepresentable and rc 3 / rc 4 are structurally unreachable — a suite
that runs local can never exercise them. Rather than skip and call it covered,
the mapping is pinned at SOURCE level (same pattern as
test_completed_not_committed_gate_language's `_norm carries body` assertions,
which exist because the WIRING is what rots while the logic tests stay green).

rc 3 and rc 4 were verified LIVE on own-cloud (alpha, cc-07, 2026-08-25) by
perturbing and then restoring a frozen archive shard byte-identically:
  rc 3  local mirror perturbed  -> "LOCAL MIRROR DIVERGED FROM STORE"
  rc 4  local mirror moved away -> "INDETERMINATE ... nothing was verified"
Both restored to the original sha256 and re-probed rc 0.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_helpers import BASH  # noqa: E402  (needs the path insert above)

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "core" / "scripts" / "backend-cat.sh"


def _run(*args, backend="local"):
    # BASH, never a bare "bash" argv[0] (guard-580: resolves to System32 WSL on
    # win32 and can hang forever). .as_posix(), never str(Path) — bash silently
    # strips the backslashes of a str(WindowsPath) (guard-581).
    return subprocess.run(
        [BASH, SCRIPT.as_posix(), *args],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        env={**os.environ, "STORAGE_BACKEND": backend},
    )


# ── flag plumbing ───────────────────────────────────────────────────────────

def test_flag_is_refused_on_cat_not_silently_ignored():
    """A caller that believes it is gating on drift while nothing gates is worse
    than a caller told it asked for the wrong thing (guard-3130: a guard whose
    predicate nothing satisfies is dead and looks exactly like a live one)."""
    r = _run("cat", "CLAUDE.md", "--exit-on-drift")
    assert r.returncode == 2
    assert "applies to 'head' only" in r.stderr


def test_flag_is_refused_on_list_too():
    r = _run("list", "core/scripts", "--exit-on-drift")
    assert r.returncode == 2
    assert "applies to 'head' only" in r.stderr


def test_unknown_flag_still_refused():
    """Scope control: adding a flag must not turn the arg loop permissive."""
    assert _run("head", "CLAUDE.md", "--nonsense").returncode == 2


# ── backward compatibility (the half most likely to break silently) ─────────

def test_head_without_the_flag_is_unchanged():
    """Every pre-existing caller of `head` must be byte-for-byte unaffected —
    the flag is opt-in, and a default-on drift exit would break them all."""
    r = _run("head", "CLAUDE.md")
    assert r.returncode == 0
    assert "backend:" in r.stdout


def test_local_backend_exits_zero_under_the_flag():
    """On the local backend the file IS the store, so drift is not merely
    absent — it is unrepresentable. Exiting 0 here is a real answer, not a
    decline, which is why it is 0 and not 4."""
    r = _run("head", "CLAUDE.md", "--exit-on-drift")
    assert r.returncode == 0
    assert "the file IS the store" in r.stdout


def test_absent_path_keeps_its_own_exit_code_ahead_of_the_drift_check():
    """not-found is rc 1 and must stay rc 1: `st is None` returns before the
    mirror probe, so a missing object can never be reported as drift. Measured
    while writing this flag — an early test asserted 4 here and was wrong."""
    assert _run("head", "core/scripts/NO-SUCH-FILE.xyz",
                "--exit-on-drift").returncode == 1


def test_usage_names_the_exit_codes():
    r = _run("bogus-subcommand", "x")
    assert r.returncode == 2
    assert "--exit-on-drift" in r.stderr
    assert "3 DRIFT" in r.stderr and "4 indeterminate" in r.stderr


# ── the verdict -> rc mapping (source-level; see module docstring) ──────────

@pytest.fixture(scope="module")
def src() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_drift_maps_to_three_and_match_to_zero(src):
    drift = src.index('verdict = "DRIFT')
    assert "drift_rc = 3" in src[drift:drift + 200]
    match = src.index('verdict = "match"')
    assert "drift_rc = 0" in src[match:match + 120]


@pytest.mark.parametrize("marker", [
    'verdict = "multipart ETag',          # md5 compare not possible
    'print("local:    (no local mirror file)")',
    'print(f"local:    (probe failed:',
])
def test_every_indeterminate_branch_maps_to_four_never_zero(src, marker):
    """THE LOAD-BEARING ASSERTION. Each of these is "I declined to look", not
    "I looked and it matched". Mapping any of them to 0 would rebuild the exact
    silence the flag exists to break, and would do it invisibly — the command
    would keep printing an honest 'local:' line while the exit code lied."""
    i = src.index(marker)
    assert "drift_rc = 4" in src[i:i + 260], f"{marker!r} does not map to rc 4"


def test_all_three_indeterminate_branches_are_still_present(src):
    """Positive control for the parametrized test above: if a branch is renamed
    or removed, `src.index` raises there and the test fails loudly — but if the
    whole block were deleted, the parametrize list would still need updating,
    so pin the count here too."""
    assert src.count("drift_rc = 4") == 3


def test_the_remedy_is_named_at_the_point_of_failure(src):
    """A caller that only ever sees "rc=3" has to go find guard-4778. The wedge
    this flag catches stayed unfixed for 4 days precisely because nothing said
    what to do next, so the message must carry the remedy and must warn against
    the whole-object push that would destroy a peer's content."""
    assert "guard-4778" in src
    assert "do NOT file an infra blocker" in src
    assert "mirror_put" in src
    assert "DESTROY the peer's content" in src
