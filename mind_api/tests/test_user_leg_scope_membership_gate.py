""" / guard-6104 — `update-goal` must refuse a NON-CANONICAL user_leg_scope.

THE DEFECT, and it is an ASYMMETRY rather than a missing check. `user_leg_scope` is
an exact-membership key: every consumer tests whole-value membership
(`audit-user-to-agent.py::_assess_user_leg` does `if scope not in
VALID_USER_LEG_SCOPES`), so a canonical head followed by prose classifies as
`undeclared` — IDENTICALLY to an empty field. `aspirations.py::_validate_goal`
already RAISES on such a value at ADD time; the generic field-update path
validated nothing at all, so the same value was refused at add and accepted in
silence at update. guard-330 names exactly this class: "Add-path validation alone
is insufficient — update-field paths are backdoors."

WHY A WRITE-TIME REFUSAL RATHER THAN ANOTHER ADVISORY. The field gives no signal
at write time, so the defect is invisible until an audit runs — and the goals most
likely to carry it are the ones whose author took the trouble to EXPLAIN the human
leg. An advisory has already been tried on this very field (the missing-scope WARN
that fires on `participants` writes) and does not reach this case at all.
MEASURED RECURRENCE, which is the actual finding: guard-6104 was written 2026-09-06
against a live population of 2 undeclared of 13 `[agent, user]` goals; on
2026-09-15 the same audit read 3 undeclared of 16 — the guardrail was active the
whole nine days and the population grew. Backfilling the named records was never
the fix, because the writer is live and the residue regenerates.

WHY THE DAEMON IS THE PRIMARY TARGET. This framework is daemon-only
(`.claude/rules/no-python-cli-fallback.md`): the wrapper reaches
`rt_call POST /v1/aspirations/update-goal`, so `aspirations.py::cmd_update_goal` is
not on the production path. The CLI twin carries the same refusal (guard-2323), and
the last test here pins that BOTH read the one SSOT set rather than a hand-typed
copy — a refusal added only to the CLI copy would have been inert on the exact path
that admits the defect (guard-742).
"""
import json
import pathlib
import subprocess
import sys

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(_SRC.parent) not in sys.path:
    sys.path.insert(0, str(_SRC.parent))
_CORE_SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / "core" / "scripts"
if str(_CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_CORE_SCRIPTS))

from src.endpoints.aspirations_write import update_goal  # noqa: E402
from gates.user_leg_scope import VALID_USER_LEG_SCOPES  # noqa: E402

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]


class _StubPaths:
    project_root = pathlib.Path(".")
    # Same poison value as test_goal_field_allowlist.py, for the same 
    # reason: a cwd-relative world made an admitted write drop a real audit
    # ledger at PROJECT_ROOT. The autouse fixture replaces it per test.
    world = pathlib.Path("/nonexistent/g-306-284-stub-world-not-patched")
    agent_name = "test-agent"


@pytest.fixture(autouse=True)
def _stub_world(tmp_path, monkeypatch):
    monkeypatch.setattr(_StubPaths, "world", tmp_path)
    return tmp_path


class _StubCtx:
    """Minimal ctx. The membership gate fires in `_run_update_goal_gates`, which
    is a PRE-LOCK gate — so a bad value costs no store I/O, which is exactly what
    lets a stub reach it with no live fleet state."""

    def __init__(self, value, field="user_leg_scope", headers=None):
        self.query = {"id": "g-306-284", "field": field}
        self.body = json.dumps(value).encode("utf-8")
        self.headers = headers or {}
        self.paths = _StubPaths()


def _body(resp):
    return str(getattr(resp, "body", "")) if resp is not None else ""


# Every shape here was measured on a live record, one per failure mode.
@pytest.mark.parametrize("bad", [
    # The three that were live-undeclared on 2026-09-15, verbatim heads.
    "product-value judgment: whether 10 real player accounts+people are worth buying",
    "marketing-positioning-decision: whether the framework's welcome email should lead with a CTA",
    "decision: DR retention posture for the basement store; owner-only per runbook",
    # guard-6104's own canonical example: a VALID token followed by prose. This is
    # the most important row — it is the shape an author writes when being helpful,
    # and it classifies identically to an empty field.
    "principal-identity: a third-party free-tier API key. Creating the account requires",
    "human-window: owner-run-and-reply: outcome 4 needs the OWNER to start a run",
    # Near-misses of real tokens.
    "architecture decision",     # space, not hyphen
    "decision",                  # the generic word, not a vocabulary member
    "gui-only",                  # a plausible-sounding invention, live on a record
    "browser-oauth-consent",     # another live invention
])
def test_non_canonical_scope_is_refused_by_the_daemon(bad):
    resp = update_goal(_StubCtx(bad))
    assert resp is not None, f"{bad!r} was admitted — the gate did not fire"
    assert resp.status == 400
    body = _body(resp)
    assert "user_leg_scope_not_canonical" in body, body[:400]
    # The refusal must be repairable from the message alone: it has to say WHERE
    # the prose belongs and WHICH tokens are legal, or the author's only move is
    # to invent a different string.
    assert "progress_note" in body, body[:400]
    for tok in ("architecture-decision", "principal-identity"):
        assert tok in body, body[:400]


@pytest.mark.parametrize("good", sorted(VALID_USER_LEG_SCOPES))
def test_every_canonical_token_passes(good):
    """The control, and the more important half of this file.

    A false refusal on a shared write path breaks live work for the WHOLE fleet,
    which is strictly worse than the drift being fixed. Parametrized over the SSOT
    set itself, so adding a token to the vocabulary cannot leave it refused here.
    """
    body = _body(update_goal(_StubCtx(good)))
    assert "user_leg_scope_not_canonical" not in body, (
        f"{good!r} is canonical but the membership gate refused it: {body[:400]}"
    )


@pytest.mark.parametrize("clearing", [None, ""])
def test_clearing_stays_open(clearing):
    """Retiring a user leg must not require a vocabulary token.

    This is the carve-out the blocker_ref shape refusal makes for the same reason:
    refusing the clear would break the legitimate path by which a resolved routing
    is retired.
    """
    body = _body(update_goal(_StubCtx(clearing)))
    assert "user_leg_scope_not_canonical" not in body, body[:400]


def test_other_fields_are_untouched():
    """The gate must key on THIS field only — a membership test leaking onto a
    neighbouring field would refuse every ordinary note write."""
    body = _body(update_goal(_StubCtx("some free prose", field="progress_note")))
    assert "user_leg_scope_not_canonical" not in body, body[:400]


def test_cli_twin_refuses_without_touching_the_store():
    """The CLI path carries the same refusal, and refuses BEFORE the lock.

    `g-000-00` does not exist; if the gate ran after the read, the error would be
    'not found' instead. Asserting on the gate's own message pins the ORDER, not
    merely the refusal.
    """
    proc = subprocess.run(
        [sys.executable, "core/scripts/aspirations.py",
         "update-goal", "g-000-00", "user_leg_scope", "decision: prose head"],
        cwd=str(_PROJECT_ROOT), capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode != 0, "CLI admitted a non-canonical user_leg_scope"
    assert "exact-membership key" in proc.stderr, proc.stderr[-500:]


def test_both_halves_read_the_one_ssot_set():
    """Neither refusal may carry a hand-typed copy of the vocabulary.

    This is the half that keeps the two write paths from drifting: the identical
    defect class (guard-742/guard-2323) is a fix landed on one call site while the
    other keeps an independent list that slowly disagrees.
    """
    import src.endpoints.aspirations_write as daemon_writer
    import aspirations as cli_writer
    import gates.user_leg_scope as ssot

    assert daemon_writer._VALID_USER_LEG_SCOPES is ssot.VALID_USER_LEG_SCOPES, (
        "the daemon refusal is not reading the SSOT object"
    )
    # aspirations.py keeps a module-level mirror whose EQUALITY to the SSOT is
    # already pinned by tests/test_allowlist_parity_batch3.py; assert it here too
    # so this file fails loudly if the CLI refusal is ever repointed at a literal.
    assert cli_writer.VALID_USER_LEG_SCOPES == ssot.VALID_USER_LEG_SCOPES
