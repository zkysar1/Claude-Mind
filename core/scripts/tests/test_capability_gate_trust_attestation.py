"""test_capability_gate_trust_attestation.py — regression test for .

THE FP. `test-capability-gate.sh` case `c_user_trust_confirmation` went red on
2026-07-28, when 767d1c4d7 forged the skill `least-privilege-credential-cutover`.
The CASE itself dates to 2026-04-18, so nothing about the gate changed — a new
skill NAME did. "need user trust confirmation for credential rotation" shares
exactly ONE token with that skill, `credential`, and that token qualifies as
distinctive only because it sits in the skill's name (`_identifier_parts`,
g-248-105).

WHY IT MATTERS beyond a red suite: `would_block` means "refuse the
participants:[user] routing", so the gate was refusing to route to a human one
of the shapes that most requires one — the same inversion `_load_human_only`
documents for guard-12/guard-29.

WHY NOT `_GENERIC_NAME_PARTS`, the adjacent and obvious fix — measured, and it
CANNOT separate these. The FP and the genuine sole-token positives produce the
byte-identical overlap `['credential']`:

    "need user trust confirmation for credential rotation"  -> ['credential']
    "this credential is over-provisioned"                   -> ['credential']
    "the credential is too broad"                           -> ['credential']

Demoting the token drops all four together, losing recall on the skill's actual
purpose. That is exactly the loss guard-958 demands an adversarial
SINGLE-surviving-keyword control for, and the multi-token invocations
("cut over to a scoped credential" -> 4 hits) MASK it precisely as guard-958
warns. The discriminator is not the token; it is the prose.

WHY THE WINDOW IS LOAD-BEARING (`test_trust_elsewhere_*`). The first
implementation searched the WHOLE text and was measured WIDER than the defect:
it flipped "cannot access EFS, awaiting user trust confirmation" from block to
no-block, because a trust phrase anywhere in a long mixed defer disqualified a
token it had nothing to do with. On a safety gate that is the fail-OPEN
direction, so the attestation must GOVERN the token rather than merely co-occur
with it — matching the convention the sibling disqualifiers state for
themselves ("scoped to the immediate window around the keyword"). Those two
tests are the ones that go red if a future edit drops the windowing.

guard-958 compliance: this change DROPS matches, which LOOSENS the gate (the
g-115-792 anti-pattern), so the adversarial sole-keyword recall controls are
mandatory and live here, adjacent to the change.

Subprocess + fixture shape mirrors test_capability_gate_generic_name_parts.py.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
GATE_PY = CORE_SCRIPTS / "capability-gate.py"

# The measured FP, verbatim from test-capability-gate.sh case c_user_trust_confirmation.
_TRUST_FP = "need user trust confirmation for credential rotation"

# guard-958 adversarial recall controls — each a SOLE-surviving-keyword case
# whose only overlap with the forged skill is 'credential', and each a genuine
# statement of that skill's own purpose.
_SOLE_OVERBROAD = "this credential is over-provisioned"
_SOLE_TOO_BROAD = "the credential is too broad"

# The canonical defer this gate MUST keep blocking (probe-before-defer.md
# anti-patterns). Protected twice over: carries no 'trust', and commit/push
# qualify on the earlier _IMPERATIVE_VERBS branch the trust check never reaches.
_MUST_BLOCK_APPROVAL = "awaiting user approval to commit and push"

# Windowing controls — a trust attestation present but NOT governing the token.
_TRUST_UNGOVERNED = ("the credential is too broad. separately, user trust "
                     "confirmation is pending")
_TRUST_AFTER_OTHER_TOKEN = "cannot access EFS, awaiting user trust confirmation"


def _load_module():
    sys.path.insert(0, str(CORE_SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "cap_trust", CORE_SCRIPTS / "gates" / "capability.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


m = _load_module()

# Shape mirrors the live world/forged-skills.yaml entry.
_CRED_ENTRY = {
    "skill": "least-privilege-credential-cutover",
    "triggers": ["cut over to a scoped credential", "least privilege",
                 "rotate off the root key", "narrow an IAM policy"],
    "source": "world",
}


def _run_gate(failure_reason: str,
              intended_participants: str = "user") -> tuple[int, dict]:
    """Invoke capability-gate.py via subprocess. Returns (exit_code, parsed)."""
    cmd = [
        sys.executable, str(GATE_PY),
        "--failure-reason", failure_reason,
        "--intended-participants", intended_participants,
        "--output", "json",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = {"_raw_stdout": proc.stdout, "_raw_stderr": proc.stderr}
    return proc.returncode, payload


# --- the measured false positive ---------------------------------------------

def test_trust_attestation_does_not_falsely_block():
    _, d = _run_gate(_TRUST_FP)
    assert not d.get("would_block"), (
        f"a human trust-attestation defer was refused routing to a human; "
        f"matches={d.get('matches')} keywords={d.get('keywords_extracted')}"
    )


# --- guard-958 recall controls (sole-surviving-keyword, not happy paths) ------

def test_sole_credential_token_still_matches_over_provisioned():
    """'credential' is the ONLY overlap and names the skill's own purpose.
    This is what demoting the token via _GENERIC_NAME_PARTS would have broken."""
    _, d = _run_gate(_SOLE_OVERBROAD)
    assert d.get("would_block"), (
        f"sole-token recall lost; keywords={d.get('keywords_extracted')}")


def test_sole_credential_token_still_matches_too_broad():
    _, d = _run_gate(_SOLE_TOO_BROAD)
    assert d.get("would_block"), (
        f"sole-token recall lost; keywords={d.get('keywords_extracted')}")


def test_user_approval_to_commit_and_push_still_blocks():
    """The canonical wrongly-deferred shape. If this ever goes green-to-red the
    fix has widened from 'trust attestation' into approval language generally,
    which reopens g-115-792 in the fail-OPEN direction."""
    _, d = _run_gate(_MUST_BLOCK_APPROVAL)
    assert d.get("would_block"), (
        f"canonical agent-provisionable defer stopped blocking; "
        f"matches={d.get('matches')} keywords={d.get('keywords_extracted')}")


# --- windowing: the attestation must GOVERN the token, not co-occur ----------

def test_trust_elsewhere_in_the_text_does_not_disqualify():
    _, d = _run_gate(_TRUST_UNGOVERNED)
    assert d.get("would_block"), (
        "an ungoverned trust phrase elsewhere in the defer disqualified a token "
        "it does not govern — the whole-text search this fix rejected")


def test_trust_after_an_unrelated_token_does_not_disqualify():
    """Measured regression of the first implementation: whole-text search
    flipped this from block to no-block."""
    _, d = _run_gate(_TRUST_AFTER_OTHER_TOKEN)
    assert d.get("would_block"), (
        "a trust phrase trailing an unrelated capability token disqualified it")


# --- the predicate itself -----------------------------------------------------

def test_governs_predicate_positive_and_negative():
    assert m._trust_attestation_governs("credential", _TRUST_FP)
    assert not m._trust_attestation_governs("credential", _SOLE_TOO_BROAD)
    assert not m._trust_attestation_governs("efs", _TRUST_AFTER_OTHER_TOKEN)


def test_single_token_qualifies_keeps_two_arg_call_shape():
    """The default text="" is what keeps the human-only veto path and the
    existing positional tests byte-identical. Pinned so a future signature
    change cannot silently alter a path it was never measured against."""
    assert m._single_token_qualifies("credential", _CRED_ENTRY)
    assert m._single_token_qualifies("credential", _CRED_ENTRY, "")
    assert not m._single_token_qualifies("credential", _CRED_ENTRY, _TRUST_FP)


def test_find_matches_keeps_two_arg_call_shape():
    kws = {"credential"}
    assert m._find_matches(kws, [_CRED_ENTRY])
    assert m._find_matches(kws, [_CRED_ENTRY], "")
    assert not m._find_matches(kws, [_CRED_ENTRY], _TRUST_FP)


def test_multi_token_overlap_is_never_disqualified():
    """>=2 hits bypass _single_token_qualifies entirely by design, so even a
    governing trust phrase cannot strip a genuine multi-token reference."""
    kws = {"credential", "scoped", "rotate"}
    assert m._find_matches(kws, [_CRED_ENTRY], _TRUST_FP)
