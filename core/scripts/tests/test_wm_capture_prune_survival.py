"""Capture-lane survival through wm PRUNE ().

The four worker->reducer capture lanes (CAPTURE_SLOTS: spark_capture,
exp_capture, hyp_capture, encoding_capture) are written by a worker Body and
consumed by the reducer at generalize-down, so an UNCONSUMED lane must survive
every intervening WM maintenance operation. There are TWO such operations and
they are protected by TWO DIFFERENT CONSTANTS:

    reset  -> RESET_SURVIVING_SLOTS   (pinned by test_wm_reset_cadence.py)
    prune  -> ARRAY_SLOTS             (pinned HERE)

Only the reset half was tested. The prune half rested on membership assertions
alone, and membership does not prove wiring (guard-1943): a slot can sit in
ARRAY_SLOTS while the prune predicate stops consulting it. The predicate is

    slot_name not in ARRAY_SLOTS and ... and slot_val is not None

and a non-empty list is not None, so an unregistered capture lane is NULLED at
evict_threshold_minutes (120) while its Body is still waiting for the next
consolidation — silently, with the worker's whole spark/experience/hypothesis/
encoding payload in it.

Covers BOTH copies of the predicate, because the DAEMON is the live path and a
wm.py-only edit is inert at runtime (guard-742/547):

  * mind_api/src/endpoints/wm_write.py prune()  -- LIVE (wm-prune.sh is daemon-only)
  * core/scripts/wm.py _do_prune()              -- CLI mirror

Iterates wm.CAPTURE_SLOTS rather than naming the four lanes, so a fifth lane is
covered by being REGISTERED, not by someone remembering to extend this file
(the g-115-6054 lesson: a representative member is not coverage of a family).

Hermetic: temp WM via BODY_WM_PATH for the CLI half, in-process DaemonFixture in
a tmp project root for the daemon half. Never touches live working memory.

Run:
  py -3 -m pytest core/scripts/tests/test_wm_capture_prune_survival.py -q
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

CORE_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(CORE_ROOT / "scripts"))

import wm  # noqa: E402

# Well past evict_threshold_minutes (120) in core/config/memory-pipeline.yaml.
# Computed from now() rather than hardcoded so the test cannot rot into a
# vacuous pass by having its fixed timestamp drift far enough that EVERY slot
# ages out identically -- the canary below is what proves eviction still fires.
AGED_MINUTES = 200


def _aged_iso() -> str:
    return (datetime.now() - timedelta(minutes=AGED_MINUTES)).isoformat()


def _capture_payload(slot: str) -> list:
    """One unconsumed item per lane. Deliberately ONE item, not many: the
    array_limits half of prune must not be the thing under test here (caps are
    a separate, real defect -- see the cap/eviction hole in g-306-203's note).
    """
    return [{"goal_id": "g-000-01", "slot": slot, "note": "unconsumed worker payload"}]


def _seed(aged: str) -> tuple[dict, dict]:
    """slots + slot_meta seeding every capture lane plus an evictable canary."""
    slots = {s: _capture_payload(s) for s in wm.CAPTURE_SLOTS}
    # Negative control. A plain scalar in no registry -- prune MUST null it.
    # Without this, a prune that silently did nothing at all (wrong path, wrong
    # WM file, threshold misread) would pass every survival assertion below.
    slots["prune_capture_canary"] = "evict_me"
    meta = {
        name: {"updated_at": aged, "accessed_at": aged, "update_count": 1}
        for name in slots
    }
    return slots, meta


def _assert_lanes_survived(after_slots: dict, where: str) -> None:
    for slot in wm.CAPTURE_SLOTS:
        assert after_slots.get(slot) == _capture_payload(slot), (
            f"{where}: capture lane {slot} did not survive prune "
            f"(got {after_slots.get(slot)!r}). ARRAY_SLOTS membership is "
            f"load-bearing for SURVIVAL, not just clear-to-[] semantics -- an "
            f"unconsumed worker payload was just destroyed."
        )


def test_every_capture_slot_is_registered_in_array_slots():
    """The membership invariant the prune predicate depends on.

    CLI-side only by design: test_wm_reset_cadence.py's parity test already
    pins ARRAY_SLOTS and CAPTURE_SLOTS byte-equal between wm.py and the daemon,
    so asserting the subset on one copy establishes it on both. Re-extracting
    the daemon constants here would duplicate that check, not strengthen it.
    """
    missing = sorted(set(wm.CAPTURE_SLOTS) - set(wm.ARRAY_SLOTS))
    assert not missing, (
        f"capture lanes absent from ARRAY_SLOTS: {missing} -- prune's "
        f"scalar-eviction predicate will null them at evict_threshold_minutes"
    )


def test_cli_prune_preserves_aged_unconsumed_capture_lanes():
    """core/scripts/wm.py _do_prune() -- the CLI mirror of the live predicate."""
    with tempfile.TemporaryDirectory() as tmpdir:
        wm_file = Path(tmpdir) / "working-memory.yaml"
        original_body = os.environ.get("BODY_WM_PATH")
        os.environ["BODY_WM_PATH"] = str(wm_file)
        try:
            wm.cmd_init(SimpleNamespace())
            aged = _aged_iso()
            slots, meta = _seed(aged)

            data = wm.read_wm()
            data["slots"].update(slots)
            data.setdefault("slot_meta", {}).update(meta)
            wm.write_wm(data)

            wm._do_prune(SimpleNamespace(dry_run=False))

            after = wm.read_wm()["slots"]
            assert after.get("prune_capture_canary") is None, (
                "negative control survived: prune did not evict an aged "
                "unregistered scalar, so this run proves nothing about the "
                "capture lanes either"
            )
            _assert_lanes_survived(after, "CLI _do_prune")
        finally:
            if original_body is None:
                os.environ.pop("BODY_WM_PATH", None)
            else:
                os.environ["BODY_WM_PATH"] = original_body


def test_daemon_prune_endpoint_preserves_aged_unconsumed_capture_lanes():
    """POST /v1/wm/prune against an in-process daemon -- the LIVE path.

    wm-prune.sh is daemon-only (no-python-cli-fallback.md), so this is the
    copy that actually runs in production; the CLI test above cannot speak for
    it (guard-742/547).
    """
    import json
    import urllib.request

    import yaml

    from _daemon_fixture import DaemonFixture

    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd) / "world"
        world.mkdir()
        with DaemonFixture(world, agent="alpha") as df:
            wm_path = (df.project_root / "agents" / "alpha" / "session"
                       / "working-memory.yaml")
            aged = _aged_iso()
            slots, meta = _seed(aged)
            wm_path.write_text(
                yaml.safe_dump({
                    "session_start": datetime.now().isoformat(),
                    "slots": slots,
                    "slot_meta": meta,
                }),
                encoding="utf-8",
            )

            req = urllib.request.Request(
                f"http://127.0.0.1:{df.port}/v1/wm/prune", method="POST")
            req.add_header("X-Mind-Agent", "alpha")
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode("utf-8"))

            # The endpoint nests its report: {"ok", "dry_run", "report": {...}}.
            # Reading evicted_slots off the top level yields an empty set on
            # every run -- a vacuous pass for the survival checks and a silent
            # false FAIL for the canary (guard-2298: a zero from a parser
            # written this turn is not a measurement). Assert the envelope so a
            # future shape change fails loudly instead of quietly.
            assert "report" in body, f"unexpected prune response shape: {body!r}"
            report = body["report"]
            evicted = {e.get("slot") for e in (report.get("evicted_slots") or [])}
            assert "prune_capture_canary" in evicted, (
                f"negative control not evicted by the daemon: {body!r} -- the "
                f"survival assertions below would be vacuous"
            )
            assert not (set(wm.CAPTURE_SLOTS) & evicted), (
                f"daemon prune reported evicting capture lanes: "
                f"{sorted(set(wm.CAPTURE_SLOTS) & evicted)}"
            )

            after = yaml.safe_load(wm_path.read_text(encoding="utf-8"))["slots"]
            assert after.get("prune_capture_canary") is None, \
                "daemon reported the canary evicted but did not write it"
            _assert_lanes_survived(after, "daemon /v1/wm/prune")


if __name__ == "__main__":
    failures = []
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as exc:
            failures.append(name)
            print(f"FAIL {name}: {exc}")
    print(f"{'TEST FAIL' if failures else 'TEST PASS'} "
          f"({len(failures)} failed)")
    sys.exit(1 if failures else 0)
