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
