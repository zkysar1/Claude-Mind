"""An UNCONFIGURED directive must not drain as a healthy empty spool ().

MEASURED DEFECT, 2026-09-07 (alpha worker Body, cc-07). Through the audited
entry point `core/scripts/inbound-drain-run.py`, a spool holding one directive
that could not be filed -- because no target aspiration is configured
(`INBOUND_DIRECTIVE_ASP_ID`) -- printed

    [inbound-drain] status=ok drained=0 failed=0 apply=True

which is BYTE-IDENTICAL to the same run against an empty spool. The record sat
unclaimed in `inbound/` and nothing anywhere said so.

Why the signal was lost, both halves:
  * `mind-inbound-drain.py` counts it and exits 2 ("an undrained spool must
    never report success"), but the domain slot ends `|| true; exit 0`, so that
    rc never survives the hop.
  * The runner's result dict summed processed/claimed/rejected/quarantined/
    skipped_tmp and `failed` -- but never `unconfigured`, and `failed` is the
    battery's UNIVERSAL finding key. So the one surviving channel dropped it.

That is the exact class `inbound-drain-run.py`'s own docstring exists to
prevent: a healthy output concealing a dead hook. A member's free text would
wait forever while the always-run lane reported clean.

ISOLATION (guard-2484): every test here drives a STUB slot via `slot_override`.
None touches the real slot, WORLD_PATH, an agent dir, team-state, or a live
spool.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parents[1]


def _load_runner():
    """`inbound-drain-run.py` is hyphenated, so it needs the importlib shape."""
    path = CORE_SCRIPTS / "inbound-drain-run.py"
    spec = importlib.util.spec_from_file_location("inbound_drain_run_under_test", path)
    assert spec and spec.loader, f"cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stub_slot(tmp_path: Path, payload: dict) -> Path:
    """A slot that ignores its args and prints one fixed JSON payload."""
    slot = tmp_path / "stub-slot.sh"
    slot.write_text(
        "#!/usr/bin/env bash\ncat <<'JSON'\n"
        + json.dumps(payload)
        + "\nJSON\n",
        encoding="utf-8",
    )
    slot.chmod(0o755)
    return slot


def _env(**counts) -> dict:
    base = {
        "environment": "env-under-test",
        "processed": 0,
        "claimed": 0,
        "rejected": 0,
        "failed": 0,
        "quarantined": 0,
        "skipped_tmp": 0,
        "unconfigured": 0,
    }
    base.update(counts)
    return base


def _findings(res: dict) -> str:
    return " | ".join(f.get("reason", "") for f in res.get("failed", []))


def test_unconfigured_directive_is_surfaced_as_a_finding(tmp_path):
    """The regression: one unconfigured directive must reach `failed`."""
    mod = _load_runner()
    slot = _stub_slot(tmp_path, {"environments": [_env(unconfigured=1)]})

    res = mod.run(apply=True, slot_override=slot)

    assert res["status"] == "ok"
    assert res["unconfigured"] == 1, "the counter must be summed onto the result"
    assert len(res["failed"]) == 1, (
        "an unconfigured directive must become a finding -- `failed` is the "
        "battery's universal finding key, so anything not in it is invisible"
    )
    assert "INBOUND_DIRECTIVE_ASP_ID" in _findings(res), (
        "the finding must name the missing config, or a reader cannot act on it"
    )
    assert "env-under-test" in res["failed"][0]["file"]


def test_empty_spool_stays_clean(tmp_path):
    """False-positive control: the fix must not fire on a healthy empty spool.

    This is the half that makes the assertion above meaningful -- without it a
    runner that flagged EVERY run would pass the regression test.
    """
    mod = _load_runner()
    slot = _stub_slot(tmp_path, {"environments": [_env()]})

    res = mod.run(apply=True, slot_override=slot)

    assert res["status"] == "ok"
    assert res["unconfigured"] == 0
    assert res["failed"] == [], "an empty spool must produce no findings"


def test_unconfigured_is_distinguishable_from_empty(tmp_path):
    """The defect stated directly: the two runs must NOT be identical.

    Pre-fix both produced `failed=0`; that indistinguishability WAS the bug, so
    it is asserted against explicitly rather than left implied by the two tests
    above.
    """
    mod = _load_runner()
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()

    stuck = mod.run(
        apply=True,
        slot_override=_stub_slot(dir_a, {"environments": [_env(unconfigured=2)]}),
    )
    clean = mod.run(
        apply=True,
        slot_override=_stub_slot(dir_b, {"environments": [_env()]}),
    )

    assert (stuck["unconfigured"], len(stuck["failed"])) != (
        clean["unconfigured"], len(clean["failed"])
    ), "a stuck directive must be distinguishable from an empty spool"
    assert stuck["unconfigured"] == 2


def test_verbs_do_not_trip_the_unconfigured_finding(tmp_path):
    """A verb names its own target and needs no aspiration.

    Measured live on cc-07: a verb-only spool reported `failed=0`. If this ever
    fires, the finding has become noise on the one record kind that is
    structurally exempt.
    """
    mod = _load_runner()
    slot = _stub_slot(tmp_path, {"environments": [_env(rejected=1)]})

    res = mod.run(apply=True, slot_override=slot)

    assert res["unconfigured"] == 0
    assert res["failed"] == []
    assert res["rejected"] == 1


def test_unconfigured_sums_across_environments(tmp_path):
    """One finding per affected environment, and the counter is a total."""
    mod = _load_runner()
    slot = _stub_slot(tmp_path, {"environments": [
        _env(environment="env-a", unconfigured=1),
        _env(environment="env-b"),
        _env(environment="env-c", unconfigured=3),
    ]})

    res = mod.run(apply=True, slot_override=slot)

    assert res["unconfigured"] == 4
    assert len(res["failed"]) == 2, "only the affected environments produce findings"
    assert {f["file"] for f in res["failed"]} == {"env-a", "env-c"}


def test_genuine_failure_and_unconfigured_both_reported(tmp_path):
    """The pre-existing `failed` finding must survive alongside the new one.

    Guards the ordering/short-circuit mistake: the new loop is additive, so a
    record that genuinely failed AND a directive left queued are two findings,
    not one replacing the other.
    """
    mod = _load_runner()
    slot = _stub_slot(tmp_path, {"environments": [_env(failed=1, unconfigured=1)]})

    res = mod.run(apply=True, slot_override=slot)

    reasons = _findings(res)
    assert len(res["failed"]) == 2, reasons
    assert "failed and stayed claimed" in reasons
    assert "INBOUND_DIRECTIVE_ASP_ID" in reasons


def test_missing_unconfigured_key_is_treated_as_zero(tmp_path):
    """Back-compat: a slot predating the counter must not crash the runner.

    The domain slot is external and versions independently, so an older one
    emits no `unconfigured` key at all.
    """
    mod = _load_runner()
    payload_env = _env()
    del payload_env["unconfigured"]
    slot = _stub_slot(tmp_path, {"environments": [payload_env]})

    res = mod.run(apply=True, slot_override=slot)

    assert res["unconfigured"] == 0
    assert res["failed"] == []


def test_a_busy_environment_is_counted_and_is_not_a_finding(tmp_path):
    """: a drain that found the environment held by another drain left its records
    queued for the next pass. That is the other drain working, not a stuck record, so it is
    summed onto the result and raises no finding: one here would report every overlap of two
    runs as a fault. The contrast is the two tests around it, whose counts are records no
    drain will take until someone fixes the box."""
    mod = _load_runner()
    slot = _stub_slot(tmp_path, {"environments": [_env(busy=1)]})

    res = mod.run(apply=True, slot_override=slot)

    assert res["busy"] == 1, "the counter must be summed onto the result"
    assert res["failed"] == [], "a busy environment is no finding"


def test_unprovisioned_records_are_surfaced_as_a_finding(tmp_path):
    """: a verb or knowledge edit the drain left queued because this box cannot
    resolve handles. The finding names both variables, either of which is enough."""
    mod = _load_runner()
    slot = _stub_slot(tmp_path, {"environments": [_env(unprovisioned=2)]})

    res = mod.run(apply=True, slot_override=slot)

    assert res["unprovisioned"] == 2
    assert len(res["failed"]) == 1, "an unprovisioned record must become a finding"
    assert "KNOWLEDGE_HANDLE_SECRET" in _findings(res)
    assert "ENVIRONMENT_ID" in _findings(res)


def test_not_a_vessel_is_untouched(tmp_path):
    """The decline branch returns before the counters and must stay that way."""
    mod = _load_runner()
    slot = _stub_slot(tmp_path, {"not_a_vessel": True, "reason": "no token"})

    res = mod.run(apply=True, slot_override=slot)

    assert res["status"] == "not-a-vessel"
    assert res["failed"] == []
    assert "unconfigured" not in res, (
        "the not-a-vessel branch returns its own dict -- adding counters there "
        "would imply a drain ran when none did"
    )


def test_a_failed_erase_is_surfaced_as_a_finding(tmp_path):
    """ u4: an erase that did not finish after the undo window. The slot ends
    `|| true; exit 0`, so this dict is the one channel that survives; a count with no finding
    would read as a healthy `drained=0`."""
    mod = _load_runner()
    slot = _stub_slot(tmp_path, {"environments": [_env(erase_failed=2)]})

    res = mod.run(apply=True, slot_override=slot)

    assert res["erase_failed"] == 2
    assert len(res["failed"]) == 1, "a failed erase must become a finding"
    assert "2 erase(s)" in _findings(res) and "tries again" in _findings(res)
    assert "still on disk" not in _findings(res), "a count cannot say which copy of the text is left"


def test_an_erase_that_is_due_and_cannot_be_done_is_surfaced_as_a_finding(tmp_path):
    mod = _load_runner()
    slot = _stub_slot(tmp_path, {"environments": [_env(erase_pending=1)]})

    res = mod.run(apply=True, slot_override=slot)

    assert res["erase_pending"] == 1
    assert len(res["failed"]) == 1
    assert "cannot be completed" in _findings(res)


def test_a_completed_erase_is_counted_and_raises_no_finding(tmp_path):
    """The contrast: erasing is the job working, so it is summed and silent. A finding here would
    report every pass that did its work as a fault."""
    mod = _load_runner()
    slot = _stub_slot(tmp_path, {"environments": [_env(erased=3), _env(erased=1)]})

    res = mod.run(apply=True, slot_override=slot)

    assert (res["erased"], res["erase_pending"], res["erase_failed"]) == (4, 0, 0)
    assert res["failed"] == []
