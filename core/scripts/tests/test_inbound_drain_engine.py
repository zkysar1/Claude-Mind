"""Engine mechanics for core/scripts/inbound_drain.py.

MOVED WITH THE ENGINE 2026-09-07 (g-369-150). These tests were written against
the domain copy in world/scripts; the engine moved to core so it could reach
seeded environment hosts (world/ is excluded from the seed and is not in git),
and coverage of its mechanics has to live where the engine lives or the in-use
engine ships untested. Slot resolution and the destination fence are covered
separately in test_inbound_drain_default_slot.py.

Synthetic fixtures only: no shared filesystem, no cloud calls, no daemon. The
two appliers are the seams — the verb applier is replaced with a stub whose
return code each test chooses, and the directive path gets a fake ``_rt``
injected into ``sys.modules`` so the REAL ``_apply_directive`` logic (including
the goal record it builds) is exercised rather than stubbed past.

The load-bearing test is ``test_nothing_is_ever_deleted``: archive-before-delete
binds on this spool because a queued record is a member's instruction and cannot
be regenerated, and the cheapest way to satisfy it is a drain that never calls
unlink at all. That test asserts the property directly instead of trusting the
implementation to keep honouring it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import tempfile
import types
import unittest
from pathlib import Path

DRAIN_PATH = Path(__file__).resolve().parents[1] / "inbound_drain.py"


def _load_drain():
    spec = importlib.util.spec_from_file_location("inbound_drain_engine_under_test", DRAIN_PATH)
    assert spec and spec.loader, f"cannot load {DRAIN_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


drain = _load_drain()


_ORIG_FENCE = drain._destination_fence


def setUpModule():
    """Neutralize the destination fence for the MECHANICS suite only.

    The fence (added when the engine moved to core) refuses to file when this
    box's world root is not under the spool being drained. Every fixture here is
    a synthetic tmp spool, so that is ALWAYS true and the fence would refuse every
    directive — this suite would then measure the fence instead of the dispositions,
    claim race and conservation properties it exists to pin. Neutralized here;
    covered in BOTH directions in test_inbound_drain_default_slot.py.
    """
    drain._destination_fence = lambda spool_root: None


def tearDownModule():
    drain._destination_fence = _ORIG_FENCE


class FenceNeutralizationCannotHideItsRemoval(unittest.TestCase):
    """CANARY. A module-wide stub over a safety check is exactly the shape that
    keeps passing after the real check is deleted, so this exercises the ORIGINAL
    captured before the stub was installed. If the fence is removed or made
    permissive, this fails HERE even though every other test in the file is
    insulated from it."""

    def test_the_real_fence_still_refuses_a_foreign_spool_root(self):
        orig_root = drain._own_world_root
        try:
            with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
                drain._own_world_root = lambda: Path(a).resolve()
                self.assertIsNotNone(_ORIG_FENCE(Path(b)))
        finally:
            drain._own_world_root = orig_root


class _StubApplier:
    """Stands in for planned-verb-apply.py. Records calls, returns a chosen rc."""

    def __init__(self, rc=0):
        self.rc = rc
        self.calls = []

    def main(self, argv):
        self.calls.append(list(argv))
        if isinstance(self.rc, Exception):
            raise self.rc
        return self.rc


class _FakeRt:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def aspirations_add_goal(self, asp_id, record, source="world", overrides=None):
        self.calls.append({"asp_id": asp_id, "record": record, "source": source})
        if self.fail:
            raise RuntimeError("daemon unreachable")
        return {"goal_id": "g-369-999"}


class DrainTestBase(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.env = self.root / "env-under-test"
        self.inbound = self.env / "inbound"
        self.inbound.mkdir(parents=True)

        self.applier = _StubApplier(rc=0)
        self._orig_loader = drain._load_verb_applier
        drain._load_verb_applier = lambda: self.applier

        self.fake_rt = _FakeRt()
        self._had_rt = "_rt" in sys.modules
        self._prev_rt = sys.modules.get("_rt")
        mod = types.ModuleType("_rt")
        mod.aspirations_add_goal = self.fake_rt.aspirations_add_goal
        sys.modules["_rt"] = mod

        self.addCleanup(self._restore)

    def _restore(self):
        drain._load_verb_applier = self._orig_loader
        if self._had_rt:
            sys.modules["_rt"] = self._prev_rt
        else:
            sys.modules.pop("_rt", None)
        self._tmp.cleanup()

    # helpers -----------------------------------------------------------
    def write_record(self, name, record):
        p = self.inbound / name
        p.write_text(json.dumps(record), encoding="utf-8")
        return p

    def verb(self, handle="h-abc", verb="prioritize", value="1"):
        return {"kind": "verb", "environmentKey": "env-under-test",
                "accountId": "acct-1", "queued_at": "2026-09-07T08:00:00",
                "source": "PutAyoEnvironmentDirectives",
                "handle": handle, "verb": verb, "value": value}

    def directive(self, text="please prioritise the onboarding board"):
        return {"kind": "directive", "environmentKey": "env-under-test",
                "accountId": "acct-1", "queued_at": "2026-09-07T08:00:00",
                "source": "PutAyoEnvironmentDirectives", "text": text}

    def run_drain(self, apply=True, **kw):
        return drain.drain_environment(
            self.env, apply=apply, source="world",
            asp_id=kw.pop("asp_id", "asp-369"),
            max_records=kw.pop("max_records", 0),
            tmp_age_min=kw.pop("tmp_age_min", 60))

    def lane_files(self, lane):
        d = self.env / lane
        return sorted(p.name for p in d.iterdir()) if d.is_dir() else []

    def all_files(self):
        return sorted(p.name for p in self.env.rglob("*") if p.is_file())


class TestTempResidueIsNeverParsed(DrainTestBase):
    """The central trap: the writer's temp name also ends in .json."""

    def test_tmp_residue_is_not_enumerated_as_a_record(self):
        # Deliberately unparseable, as a half-written file would be.
        (self.inbound / ".tmp-halfwritten.json").write_text('{"kind": "ver',
                                                            encoding="utf-8")
        # POSITIVE CONTROL in the same drain: a real record beside it must be
        # processed, so "parsed nothing" cannot pass this test.
        self.write_record("20260907T080000000000-aaa.json", self.verb())

        res = self.run_drain(tmp_age_min=60)

        self.assertEqual(res["processed"], 1, "the real record must still be applied")
        self.assertEqual(res["quarantined"], 0, "fresh temp residue is left alone, not quarantined")
        self.assertIn(".tmp-halfwritten.json", os.listdir(self.inbound),
                      "fresh temp residue must stay in inbound untouched")
        for entry in res["records"]:
            self.assertNotIn("tmp", entry["file"], "no temp file may appear as a record")

    def test_stale_tmp_residue_is_quarantined_not_parsed(self):
        stale = self.inbound / ".tmp-crashed.json"
        stale.write_text('{"kind": "ver', encoding="utf-8")
        old = time.time() - (120 * 60)
        os.utime(stale, (old, old))

        res = self.run_drain(tmp_age_min=60)

        self.assertEqual(res["quarantined"], 1)
        self.assertEqual(self.lane_files("quarantine"), [".tmp-crashed.json"])
        self.assertEqual(res["processed"], 0)

    def test_dotfile_that_is_not_tmp_is_still_never_a_record(self):
        (self.inbound / ".hidden.json").write_text('{"kind":"verb"}', encoding="utf-8")
        res = self.run_drain()
        self.assertEqual(res["records"], [])


class TestVerbDispositions(DrainTestBase):
    def test_applied_verb_moves_to_processed_and_uses_canonical_argv(self):
        self.write_record("20260907T080000000000-a.json", self.verb(value="3"))
        res = self.run_drain()

        self.assertEqual(res["processed"], 1)
        self.assertEqual(len(self.lane_files("processed")), 1)
        self.assertEqual(self.lane_files("processing"), [])
        argv = self.applier.calls[0]
        self.assertIn("--apply", argv, "the drain must apply, not dry-run, the applier")
        self.assertEqual(argv[argv.index("--handle") + 1], "h-abc")
        self.assertEqual(argv[argv.index("--verb") + 1], "prioritize")
        self.assertEqual(argv[argv.index("--value") + 1], "3")

    def test_applier_rc3_is_terminal_rejected_not_retried(self):
        self.applier.rc = 3
        self.write_record("20260907T080000000000-a.json", self.verb())
        res = self.run_drain()

        self.assertEqual(res["rejected"], 1)
        self.assertEqual(res["failed"], 0)
        self.assertEqual(len(self.lane_files("rejected")), 1)
        self.assertEqual(self.lane_files("processing"), [],
                         "a terminal refusal must not stay claimed")

    def test_transient_failure_stays_claimed_in_processing(self):
        self.applier.rc = 1
        self.write_record("20260907T080000000000-a.json", self.verb())
        res = self.run_drain()

        self.assertEqual(res["failed"], 1)
        self.assertEqual(len(self.lane_files("processing")), 1,
                         "a transient failure stays visible in processing/, never deleted")
        self.assertEqual(self.lane_files("processed"), [])

    def test_applier_exception_is_a_transient_failure_not_a_crash(self):
        self.applier.rc = RuntimeError("boom")
        self.write_record("20260907T080000000000-a.json", self.verb())
        res = self.run_drain()
        self.assertEqual(res["failed"], 1)
        self.assertIn("applier raised", res["records"][0]["detail"])

    def test_unknown_verb_is_rejected_before_the_applier_is_called(self):
        self.write_record("20260907T080000000000-a.json", self.verb(verb="delete-everything"))
        res = self.run_drain()
        self.assertEqual(res["rejected"], 1)
        self.assertEqual(self.applier.calls, [], "an unknown verb must not reach the applier")


class TestDirectiveDispositions(DrainTestBase):
    def test_directive_files_a_goal_carrying_the_member_text(self):
        self.write_record("20260907T080000000000-a.json",
                          self.directive("ship the referral banner"))
        res = self.run_drain()

        self.assertEqual(res["processed"], 1)
        self.assertEqual(len(self.fake_rt.calls), 1)
        call = self.fake_rt.calls[0]
        self.assertEqual(call["asp_id"], "asp-369")
        self.assertIn("ship the referral banner", call["record"]["description"])
        self.assertIn("ship the referral banner", call["record"]["title"])
        self.assertEqual(call["record"]["participants"], ["agent"])
        self.assertIn("env-under-test", call["record"]["description"],
                      "provenance must survive into the goal")
        # The daemon's origin-signal gate refuses agent-sourced filings that
        # carry no registered signal — measured as 400 origin_signal_blocked on
        # a real vessel (). The member is the user.
        self.assertEqual(call["record"]["origin_signal"], "user_directive")

    def test_daemon_failure_is_transient_and_keeps_the_record(self):
        sys.modules["_rt"].aspirations_add_goal = _FakeRt(fail=True).aspirations_add_goal
        self.write_record("20260907T080000000000-a.json", self.directive())
        res = self.run_drain()

        self.assertEqual(res["failed"], 1)
        self.assertEqual(len(self.lane_files("processing")), 1)

    def test_daemon_refusal_body_is_surfaced_in_the_failure_detail(self):
        # A 4xx from the daemon carries its reason in the body; the drain must
        # report it, not just the status. A paid vessel run was diagnosed blind
        # because only "daemon HTTP 400" surfaced ().
        class _Refused(RuntimeError):
            def __init__(self):
                super().__init__("daemon HTTP 400 for POST /v1/aspirations/add-goal")
                self.status = 400
                self.body = '{"error": "origin_signal_blocked", "gate": "origin-signal-gate"}'

        def refuse(asp_id, record, source="world", overrides=None):
            raise _Refused()

        sys.modules["_rt"].aspirations_add_goal = refuse
        self.write_record("20260907T080000000000-a.json", self.directive())
        res = self.run_drain()

        self.assertEqual(res["failed"], 1)
        failed = [r for r in res["records"] if r.get("disposition") == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertIn("origin_signal_blocked", failed[0]["detail"])
        self.assertIn("daemon HTTP 400", failed[0]["detail"])
        self.assertEqual(len(self.lane_files("processing")), 1,
                         "a refused record stays claimed and visible, never lost")

    def test_empty_text_is_rejected_without_calling_the_daemon(self):
        self.write_record("20260907T080000000000-a.json", self.directive(text="   "))
        res = self.run_drain()
        self.assertEqual(res["rejected"], 1)
        self.assertEqual(self.fake_rt.calls, [])


class TestMalformedRecords(DrainTestBase):
    def test_unparseable_json_is_quarantined(self):
        (self.inbound / "20260907T080000000000-a.json").write_text("{not json",
                                                                   encoding="utf-8")
        res = self.run_drain()
        self.assertEqual(res["quarantined"], 1)
        self.assertEqual(len(self.lane_files("quarantine")), 1)

    def test_unknown_kind_is_quarantined(self):
        self.write_record("20260907T080000000000-a.json", {"kind": "wire-money"})
        res = self.run_drain()
        self.assertEqual(res["quarantined"], 1)

    def test_json_array_is_quarantined_not_treated_as_a_record(self):
        (self.inbound / "20260907T080000000000-a.json").write_text('[{"kind":"verb"}]',
                                                                   encoding="utf-8")
        res = self.run_drain()
        self.assertEqual(res["quarantined"], 1)


class TestOrderingAndConservation(DrainTestBase):
    def test_records_are_drained_in_queue_order(self):
        self.write_record("20260907T080300000000-c.json", self.verb(handle="third"))
        self.write_record("20260907T080100000000-a.json", self.verb(handle="first"))
        self.write_record("20260907T080200000000-b.json", self.verb(handle="second"))

        self.run_drain()

        handles = [c[c.index("--handle") + 1] for c in self.applier.calls]
        self.assertEqual(handles, ["first", "second", "third"])

    def test_nothing_is_ever_deleted(self):
        """archive-before-delete: every record must survive somewhere."""
        self.write_record("20260907T080100000000-a.json", self.verb())          # processed
        self.write_record("20260907T080200000000-b.json", {"kind": "nonsense"})  # quarantined
        (self.inbound / "20260907T080300000000-c.json").write_text("{bad",
                                                                   encoding="utf-8")  # quarantined
        before = set(self.all_files())
        self.assertEqual(len(before), 3)

        self.run_drain()

        after = set(self.all_files())
        self.assertEqual(len(after), 3, f"a record was destroyed: {before - after}")
        self.assertEqual({Path(n).name for n in before}, {Path(n).name for n in after})
        self.assertEqual(os.listdir(self.inbound), [], "inbound is fully drained")

    def test_dry_run_moves_nothing_and_calls_nothing(self):
        self.write_record("20260907T080100000000-a.json", self.verb())
        self.write_record("20260907T080200000000-b.json", self.directive())

        res = self.run_drain(apply=False)

        self.assertTrue(res["dry_run"])
        self.assertEqual(self.applier.calls, [])
        self.assertEqual(self.fake_rt.calls, [])
        self.assertEqual(len(os.listdir(self.inbound)), 2, "dry run must not move records")
        self.assertEqual(self.lane_files("processed"), [])

    def test_max_records_caps_one_pass(self):
        for i in range(5):
            self.write_record(f"20260907T08000000000{i}-x.json", self.verb())
        res = self.run_drain(max_records=2)
        self.assertEqual(res["processed"], 2)
        self.assertEqual(len(os.listdir(self.inbound)), 3)


class TestRequeueStale(DrainTestBase):
    def test_stale_processing_entries_return_to_inbound(self):
        proc = self.env / "processing"
        proc.mkdir(parents=True)
        stale = proc / "20260907T080000000000-a.json"
        stale.write_text(json.dumps(self.verb()), encoding="utf-8")
        old = time.time() - (200 * 60)
        os.utime(stale, (old, old))
        fresh = proc / "20260907T090000000000-b.json"
        fresh.write_text(json.dumps(self.verb()), encoding="utf-8")

        res = drain.requeue_stale(self.env, apply=True, age_min=60)

        self.assertEqual(res["requeued"], 1)
        self.assertEqual(os.listdir(self.inbound), ["20260907T080000000000-a.json"])
        self.assertEqual(self.lane_files("processing"), ["20260907T090000000000-b.json"],
                         "a fresh in-flight record must not be requeued")

    def test_requeue_dry_run_moves_nothing(self):
        proc = self.env / "processing"
        proc.mkdir(parents=True)
        p = proc / "20260907T080000000000-a.json"
        p.write_text("{}", encoding="utf-8")
        old = time.time() - (200 * 60)
        os.utime(p, (old, old))

        res = drain.requeue_stale(self.env, apply=False, age_min=60)

        self.assertEqual(res["requeued"], 1)
        self.assertEqual(os.listdir(self.inbound), [], "dry run must not move")


class TestCliContract(DrainTestBase):
    def test_requires_an_environment_selector(self):
        with self.assertRaises(SystemExit) as cm:
            drain.main(["--root", str(self.root)])
        self.assertNotEqual(cm.exception.code, 0)

    def test_missing_root_exits_1(self):
        rc = drain.main(["--root", str(self.root / "nope"), "--all"])
        self.assertEqual(rc, 1)

    def test_transient_failure_sets_exit_2(self):
        self.applier.rc = 1
        self.write_record("20260907T080000000000-a.json", self.verb())
        rc = drain.main(["--root", str(self.root), "--environment-key", "env-under-test",
                         "--apply", "--json"])
        self.assertEqual(rc, 2, "a stuck record must not report success")

    def test_clean_drain_exits_0(self):
        self.write_record("20260907T080000000000-a.json", self.verb())
        rc = drain.main(["--root", str(self.root), "--environment-key", "env-under-test",
                         "--apply", "--json"])
        self.assertEqual(rc, 0)


class TestAspirationHasNoSilentDefault(DrainTestBase):
    """: WHICH aspiration a member's bullet files into is a per-vessel
    decision. The drain must refuse to guess it, and must refuse without
    consuming the record — a config gap may not cost a member their words."""

    def test_directive_without_aspiration_is_left_queued_not_filed(self):
        self.write_record("20260907T080000000000-a.json", self.directive("ship the banner"))

        res = self.run_drain(asp_id=None)

        self.assertEqual(res["unconfigured"], 1)
        self.assertEqual(res["processed"], 0)
        self.assertEqual(self.fake_rt.calls, [], "must not file into a guessed aspiration")
        self.assertEqual(os.listdir(self.inbound), ["20260907T080000000000-a.json"],
                         "the record stays in inbound/, so it drains itself once configured")
        self.assertEqual(self.lane_files("processing"), [],
                         "an unconfigured directive must not even be CLAIMED")
        self.assertEqual(self.lane_files("rejected"), [])
        self.assertIn("INBOUND_DIRECTIVE_ASP_ID", res["records"][0]["detail"],
                      "the refusal must name the fix")

    def test_verb_still_drains_without_an_aspiration(self):
        """POSITIVE CONTROL: the guard must not stall verbs, which need no aspiration."""
        self.write_record("20260907T080000000000-a.json", self.verb())

        res = self.run_drain(asp_id=None)

        self.assertEqual(res["processed"], 1)
        self.assertEqual(res["unconfigured"], 0)
        self.assertEqual(len(self.lane_files("processed")), 1)

    def test_mixed_spool_drains_verbs_and_holds_directives(self):
        self.write_record("20260907T080100000000-a.json", self.verb())
        self.write_record("20260907T080200000000-b.json", self.directive())

        res = self.run_drain(asp_id=None)

        self.assertEqual(res["processed"], 1)
        self.assertEqual(res["unconfigured"], 1)
        self.assertEqual(os.listdir(self.inbound), ["20260907T080200000000-b.json"])

    def test_helper_refuses_as_FAILED_not_REJECTED(self):
        """Defence in depth. FAILED is recoverable; REJECTED would be terminal and
        would discard the member's instruction on a mere config gap."""
        disposition, detail = drain._apply_directive(
            self.directive(), "world", None, spool_root=Path("/"))
        self.assertEqual(disposition, drain.FAILED)
        self.assertNotEqual(disposition, drain.REJECTED)
        self.assertIn("no target aspiration", detail)

    def test_unconfigured_directive_sets_exit_2(self):
        self.write_record("20260907T080000000000-a.json", self.directive())
        rc = drain.main(["--root", str(self.root), "--environment-key", "env-under-test",
                         "--apply", "--json"])
        self.assertEqual(rc, 2, "an undrained spool must never report success")

    def test_env_var_supplies_the_target_when_the_flag_is_absent(self):
        """The vessel config channel: systemd EnvironmentFile -> .env.local -> here."""
        self.write_record("20260907T080000000000-a.json", self.directive())
        prev = os.environ.get("INBOUND_DIRECTIVE_ASP_ID")
        os.environ["INBOUND_DIRECTIVE_ASP_ID"] = "asp-777"
        try:
            rc = drain.main(["--root", str(self.root), "--environment-key", "env-under-test",
                             "--apply", "--json"])
        finally:
            if prev is None:
                os.environ.pop("INBOUND_DIRECTIVE_ASP_ID", None)
            else:
                os.environ["INBOUND_DIRECTIVE_ASP_ID"] = prev
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.fake_rt.calls), 1)
        self.assertEqual(self.fake_rt.calls[0]["asp_id"], "asp-777",
                         "the env var must reach the filing call, not a default")

    def test_flag_beats_the_env_var(self):
        self.write_record("20260907T080000000000-a.json", self.directive())
        prev = os.environ.get("INBOUND_DIRECTIVE_ASP_ID")
        os.environ["INBOUND_DIRECTIVE_ASP_ID"] = "asp-777"
        try:
            drain.main(["--root", str(self.root), "--environment-key", "env-under-test",
                        "--apply", "--aspiration", "asp-888", "--json"])
        finally:
            if prev is None:
                os.environ.pop("INBOUND_DIRECTIVE_ASP_ID", None)
            else:
                os.environ["INBOUND_DIRECTIVE_ASP_ID"] = prev
        self.assertEqual(self.fake_rt.calls[0]["asp_id"], "asp-888")


if __name__ == "__main__":
    unittest.main()
