#!/usr/bin/env python3
"""test_known_blockers_key_drift.py — regression test ().

The known_blockers slot had a three-way key drift between its documented
schema, its two writers, and its six readers:

    documented (handoff-working-memory.md)  blocker_id  detected_at   reason
    infra-health.py      (writer)           blocker_id  <ABSENT>      reason
    create-blocker.py    (writer)           id          created_at    failure_reason
    every reader                            blocker_id  detected_at   reason

create-blocker.py is the canonical CREATE_BLOCKER path, so blockers born
there were invisible to EVERY reader simultaneously. `_age_hours` returned
None for all of them, and blocker-recheck.py's loop does
`if age is None ... continue` — so the aged-blocker capability recheck and
the proactive user escalation were BOTH structurally unreachable at ANY age.
Not "not yet aged": permanently skipped. Both alarm clocks on a user-routed
blocker were disconnected at once.

infra-health.py had the same disease in a different form: it emitted
`detected_session` but no `detected_at` at all, so its streak blockers were
ageless too — and because it REBUILDS every `streak-*` entry on each sync, a
naive `detected_at = now` would have reset the age on every run (permanently
young rather than permanently ageless, equally unreachable).

Fix direction matters and is counter-intuitive: the writers were corrected to
the documented schema, NOT the readers to the legacy names. Flipping readers
to id/created_at would have broken infra-health.py's entries, which were
already schema-correct.
"""

import ast
import datetime as dt
import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

# blocker-recheck.py is hyphenated -> load by path
_spec = importlib.util.spec_from_file_location(
    "blocker_recheck", SCRIPT_DIR / "blocker-recheck.py"
)
br = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(br)


def _stamp(hours_ago):
    return (dt.datetime.now() - dt.timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )


def _age_of(blocker):
    """Replicate the reader's age expression (blocker-recheck.py sweep loop)."""
    return br._age_hours(blocker.get("detected_at") or blocker.get("created_at"))


# ---------------------------------------------------------------- readers ---

def test_reader_ages_canonical_shape():
    """A blocker carrying the documented detected_at ages normally."""
    age = _age_of({"blocker_id": "infra-x-2026-07-26", "detected_at": _stamp(30)})
    assert age is not None
    assert 29.5 < age < 30.5


def test_reader_ages_legacy_shape():
    """THE regression: a blocker in create-blocker.py's pre-fix shape ages.

    Before g-115-3348 this returned None, and the caller's
    `if age is None or age < max_age: continue` skipped it at every age —
    so the recheck sweep reported rechecked=0 forever on real blockers.
    """
    legacy = {"id": "infra-aws-exec-2026-07-26", "created_at": _stamp(72)}
    age = _age_of(legacy)
    assert age is not None, "legacy created_at-only blocker must still age"
    assert 71.5 < age < 72.5


def test_reader_prefers_detected_at_when_both_present():
    """Canonical key wins; the alias is a fallback, not an override."""
    both = {"detected_at": _stamp(10), "created_at": _stamp(99)}
    age = _age_of(both)
    assert 9.5 < age < 10.5


def test_reader_returns_none_when_both_absent():
    """Refuse-to-guess is preserved — the shim widens tolerance, not silence."""
    assert _age_of({"blocker_id": "streak-thing"}) is None
    assert _age_of({"detected_at": None, "created_at": None}) is None
    assert _age_of({"detected_at": {"not": "a timestamp"}}) is None


def test_blocker_id_tolerates_both_spellings():
    assert br._blocker_id({"blocker_id": "streak-svc"}) == "streak-svc"
    assert br._blocker_id({"id": "infra-legacy-2026-07-26"}) == "infra-legacy-2026-07-26"
    # canonical wins when both are present
    assert br._blocker_id({"blocker_id": "canon", "id": "legacy"}) == "canon"
    assert br._blocker_id({}) is None


# ---------------------------------------------------------------- writers ---

def _dict_keys_of_assignment(path, var_name):
    """Collect the literal string keys of `var_name = {...}` in a source file."""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == var_name:
                    return {
                        k.value
                        for k in node.value.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)
                    }
    raise AssertionError(f"{var_name} dict-literal assignment not found in {path}")


def test_create_blocker_emits_documented_schema_keys():
    """The sole deviant writer now conforms to the documented schema."""
    keys = _dict_keys_of_assignment(SCRIPT_DIR / "create-blocker.py", "new_blocker")
    assert "blocker_id" in keys, "create-blocker.py must emit the documented blocker_id"
    assert "detected_at" in keys, "create-blocker.py must emit the documented detected_at"
    # Legacy aliases retained deliberately: live blockers across the fleet carry
    # them, and create-blocker.py reads its own `id` on the dedup path. Removing
    # them is a separate migration once fleet blockers have cycled.
    assert "id" in keys and "created_at" in keys


def test_infra_health_emits_detected_at():
    """The other writer had NO age key at all — only detected_session."""
    src = (SCRIPT_DIR / "infra-health.py").read_text(encoding="utf-8")
    assert '"detected_at"' in src, "infra-health.py streak entries need detected_at"


def test_infra_health_carries_detected_at_forward():
    """Age must survive re-derivation, or streak blockers can never age.

    infra-health.py drops and rebuilds every `streak-*` entry on each sync.
    Stamping `now` unconditionally would reset the clock every run, so the
    blocker would sit permanently below any max-age threshold. Pins both the
    carry-forward map and its use at the construction site.
    """
    src = (SCRIPT_DIR / "infra-health.py").read_text(encoding="utf-8")
    assert "prior_detected" in src, "carry-forward map missing"
    assert 'prior_detected.get(f"streak-{component}") or _now_iso' in src, (
        "detected_at must reuse the prior stamp, falling back to now only for "
        "a genuinely new streak"
    )
    # Behavioral proof of the carry-forward semantics the source implements.
    original = _stamp(50)
    existing = [{"blocker_id": "streak-svc", "detected_at": original}]
    prior = {
        b["blocker_id"]: b.get("detected_at")
        for b in existing
        if b.get("blocker_id", "").startswith("streak-") and b.get("detected_at")
    }
    assert prior.get("streak-svc") == original
    assert prior.get("streak-brand-new") is None  # new streak -> falls back to now


# ------------------------------------------------------------ drift guards ---

def test_reader_age_read_keeps_both_spellings():
    """Guard the fix direction: do NOT flip readers to the legacy names only."""
    src = (SCRIPT_DIR / "blocker-recheck.py").read_text(encoding="utf-8")
    assert 'b.get("detected_at") or b.get("created_at")' in src, (
        "age read must tolerate both; flipping to created_at alone would break "
        "infra-health.py streak blockers, which are schema-correct"
    )


def test_failure_reason_chain_reads_top_level_failure_reason():
    """The 4th drift: create-blocker.py stores the narrative at TOP level.

    diagnostic_context is caller-supplied JSON and carries failure_reason only
    by luck, so without this rung the chain fell through to the blocker ID and
    fed *that* to the capability gate — a verdict on an identifier rather than
    a re-probe of the failure. Latent until the age filter above was fixed.
    """
    src = (SCRIPT_DIR / "blocker-recheck.py").read_text(encoding="utf-8")
    chain_start = src.index("failure_reason = (")
    chain = src[chain_start : chain_start + 1600]
    assert 'b.get("failure_reason")' in chain
    # id fallback must remain LAST — it is a last-resort label, not a reason
    assert chain.index('b.get("failure_reason")') < chain.index("_blocker_id(b)")


def test_skill_pseudocode_tolerates_legacy_shape():
    """Both LLM-side readers normalize before use (precheck + all-blocked)."""
    repo = SCRIPT_DIR.parent.parent
    precheck = (repo / ".claude/skills/aspirations-precheck/SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "blocker.blocker_id or blocker.id" in precheck
    assert "blocker.detected_at or blocker.created_at" in precheck
    # cooldown lookup and the log write must key on the SAME normalized value,
    # or an escalation can never match what /notify-user Step 3 wrote
    assert "where blocker_id == bid" in precheck
    assert '{"blocker_id":"{bid}"' in precheck

    all_blocked = (repo / ".claude/skills/aspirations-all-blocked/SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "blocker.detected_at or blocker.created_at" in all_blocked
    assert "blocker.reason or blocker.failure_reason" in all_blocked
