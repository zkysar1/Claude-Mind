"""Engine mechanics for core/scripts/inbound_drain.py.

MOVED WITH THE ENGINE 2026-09-07 (g-369-150). These tests were written against
the domain copy in world/scripts; the engine moved to core so it could reach
seeded environment hosts (world/ is excluded from the seed and is not in git),
and coverage of its mechanics has to live where the engine lives or the in-use
engine ships untested. Slot resolution and the destination fence are covered
separately in test_inbound_drain_default_slot.py.

Synthetic fixtures only: no shared filesystem, no cloud calls, no daemon. The
appliers are the seams — the verb and knowledge appliers are replaced with stubs
whose return code each test chooses, and the directive path gets a fake ``_rt``
injected into ``sys.modules`` so the REAL ``_apply_directive`` logic (including
the goal record it builds) is exercised rather than stubbed past. Two classes run
the REAL knowledge applier against a tmp world (an edit, then a forget and its undo),
because only it can say the argv the drain builds is the argv the applier parses.

The load-bearing test is ``test_nothing_is_ever_deleted``: archive-before-delete
binds on this spool because a queued record is a member's instruction and cannot
be regenerated, so a drain removes no record: the one file it unlinks is its own
lock, on release. That test asserts the property directly instead of trusting the
implementation to keep honouring it.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import stat
import sys
import threading
import time
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

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
    """Stands in for planned-verb-apply.py and knowledge-edit-apply.py. Records calls,
    returns a chosen rc, and puts ``reported`` in the report a caller passes, as the
    knowledge applier does."""

    def __init__(self, rc=0, reported=None):
        self.rc = rc
        self.reported = reported or {}
        self.calls = []

    def main(self, argv, report=None):
        self.calls.append(list(argv))
        if isinstance(self.rc, Exception):
            raise self.rc
        if report is not None:
            report.update(self.reported)
        return self.rc


class _PrintingApplier(_StubApplier):
    """An applier that prints a line on success, as both real ones do without --json."""

    LINE = "goal-1: edit -> applied"

    def main(self, argv, report=None):
        print(self.LINE)
        return super().main(argv, report)


class _ExitingApplier:
    """An applier that dies through sys.exit(<message>), as an unset WORLD_PATH does."""

    def main(self, argv, report=None):
        raise SystemExit("WORLD_PATH not set")


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
        # Stubbed for every test, as the verb applier is: the real one writes into
        # whatever world this process resolves.
        self.knowledge_applier = _StubApplier(rc=0)
        self._orig_knowledge_loader = drain._load_knowledge_applier
        drain._load_knowledge_applier = lambda: self.knowledge_applier
        # A provisioned box, as on a real environment host: the drain leaves verbs and
        # knowledge edits queued on a box that cannot resolve handles. The REAL check
        # runs in every test; TestUnprovisionedBox takes the values away.
        provisioned = mock.patch.dict(os.environ, {"KNOWLEDGE_HANDLE_SECRET": "handle-secret-for-tests",
                                                   "ENVIRONMENT_ID": "env-under-test"})
        provisioned.start()
        self.addCleanup(provisioned.stop)

        # Where this box's world is, for the retention fence: beside the spool, so it neither
        # holds the retention directory nor sits in it. TestRetentionFence moves it. A lambda,
        # so a test's own assignment is what the drain sees.
        self.world_root = self.root / "world-beside-the-spool"
        own_world = mock.patch.object(drain, "_own_world_root", lambda: self.world_root)
        own_world.start()
        self.addCleanup(own_world.stop)

        self.fake_rt = _FakeRt()
        self._had_rt = "_rt" in sys.modules
        self._prev_rt = sys.modules.get("_rt")
        mod = types.ModuleType("_rt")
        mod.aspirations_add_goal = self.fake_rt.aspirations_add_goal
        sys.modules["_rt"] = mod

        self.addCleanup(self._restore)

    def _restore(self):
        drain._load_verb_applier = self._orig_loader
        drain._load_knowledge_applier = self._orig_knowledge_loader
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

    def knowledge(self, op="edit", text="Widgets are green.", handle="0123456789abcdef",
                  base="ab" * 32):
        rec = {"kind": "knowledge", "environmentKey": "env-under-test",
               "accountId": "acct-1", "queued_at": "2026-09-30T18:00:00",
               "source": "PutAyoEnvironmentDirectives", "handle": handle, "op": op}
        if op == "edit":
            rec["text"] = text
            if base is not None:
                rec["base"] = base
        return rec

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
        self.assertIn("duplicates", res,
                      "the key must be present even at 0 — a caller asserting "
                      "payload shape must not see it appear only on a hit")

    def test_requeue_duplicate_stem_goes_to_rejected(self):
        """ outcome 3: a stem already queued must not be re-filed.

        ``_move`` suffixes a collision with a millisecond stamp, which is right
        for its own never-delete contract and wrong here — it puts a SECOND copy
        of one member's instruction in inbound/ and the drain then applies it
        twice. Measured 2026-09-08: hand re-queueing left the original plus
        ``...1788847706970.json``. The duplicate is moved aside to rejected/,
        never deleted, so it stays inspectable.
        """
        proc = self.env / "processing"
        proc.mkdir(parents=True)
        stem = "20260907T080000000000-a"
        (self.inbound / f"{stem}.json").write_text(
            json.dumps(self.verb()), encoding="utf-8")
        dup = proc / f"{stem}.json"
        dup.write_text(json.dumps(self.verb()), encoding="utf-8")
        old = time.time() - (200 * 60)
        os.utime(dup, (old, old))

        res = drain.requeue_stale(self.env, apply=True, age_min=60)

        self.assertEqual(res["requeued"], 0, "a duplicate stem must not be requeued")
        self.assertEqual(res["duplicates"], 1)
        self.assertEqual(os.listdir(self.inbound), [f"{stem}.json"],
                         "inbound must still hold exactly one copy")
        self.assertEqual(self.lane_files("rejected"), [f"{stem}.json"],
                         "the duplicate is moved aside, never deleted")
        self.assertEqual(self.lane_files("processing"), [])


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

    def test_json_output_stays_one_document_when_an_applier_prints(self):
        """Both real appliers print a line on success, and the runner parses this
        process's whole stdout as one JSON document (g-335-1726)."""
        self.applier = _PrintingApplier()
        self.knowledge_applier = _PrintingApplier()
        self.write_record("20260907T080000000000-a.json", self.verb())
        self.write_record("20260930T180000000000-b.json", self.knowledge())
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            drain.main(["--root", str(self.root), "--environment-key", "env-under-test",
                        "--apply", "--json"])

        # Control: both lines were printed. Only where they land may change.
        self.assertEqual((out.getvalue() + err.getvalue()).count(_PrintingApplier.LINE), 2)
        self.assertEqual(json.loads(out.getvalue())["environments"][0]["processed"], 2)


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
        # THE ONLY TEST IN THIS CLASS THAT REACHES main(), AND THEREFORE THE ONLY ONE
        # THE AMBIENT ENVIRONMENT CAN REACH. Its siblings go through run_drain(), which
        # calls drain_environment() with an explicit asp_id and is isolated by
        # construction; main() resolves the target from INBOUND_DIRECTIVE_ASP_ID at
        # parse time (the `--aspiration` default), and a VESSEL-CONFIGURED box carries
        # that key in .env.local. So this test read the BOX's config, not the code.
        # Measured on alpha/cc-07 2026-09-13, one variable moved both ways: ambient
        # INBOUND_DIRECTIVE_ASP_ID=asp-371 -> the record was FILED ("filed ")
        # and main() returned 0; `env -u INBOUND_DIRECTIVE_ASP_ID` -> rc 2, green.
        # A test asserting the ABSENCE of a configuration must ENFORCE that absence
        # (guard-4425, guard-2710, rb-3208), or it reports the box and reads as a real
        # defect in the one refusal path this class exists to guard.
        # The ASSERTION below is deliberately unchanged — the fix is the precondition,
        # not the expectation (rb-9217: a green won by editing the assertion is not a
        # fix). Restoring the variable is sufficient here because nothing memoizes it:
        # main() builds its parser per call, so os.environ is read at call time and
        # there is no process-global to reset beside it (guard-4358, checked).
        prev = os.environ.get("INBOUND_DIRECTIVE_ASP_ID")
        os.environ.pop("INBOUND_DIRECTIVE_ASP_ID", None)
        self.write_record("20260907T080000000000-a.json", self.directive())
        try:
            rc = drain.main(["--root", str(self.root), "--environment-key", "env-under-test",
                             "--apply", "--json"])
        finally:
            if prev is not None:
                os.environ["INBOUND_DIRECTIVE_ASP_ID"] = prev
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


class TestKnowledgeDispositions(DrainTestBase):
    """kind=knowledge: a member's edit or forget of one learned item ().

    Until this port the engine quarantined every knowledge record as an unknown kind:
    the routing lived only in the retired world engine, which no environment runs.
    """

    def test_edit_reaches_the_applier_with_canonical_argv(self):
        self.write_record("20260930T180000000000-a.json", self.knowledge(text="- green"))
        res = self.run_drain()

        self.assertEqual(res["processed"], 1)
        self.assertEqual(len(self.lane_files("processed")), 1)
        # Pinned whole: --text= and --base= are single tokens, so a leading dash stays
        # text. The applier's side of this shape is pinned in test_knowledge_edit_apply.
        self.assertEqual(self.knowledge_applier.calls,
                         [["--handle", "0123456789abcdef", "--op", "edit", "--text=- green",
                           f"--base={'ab' * 32}", "--apply"]])
        self.assertEqual(self.applier.calls, [], "a knowledge record never reaches the verb applier")

    def test_an_absent_base_is_forwarded_empty_for_the_applier_to_refuse(self):
        """The applier is the one authority on the base (view_base_missing, rc 3). A
        second copy of that rule here would drift from it."""
        self.write_record("20260930T180000000000-a.json", self.knowledge(base=None))
        self.run_drain()

        argv = self.knowledge_applier.calls[0]
        self.assertIn("--base=", argv)

    def test_a_base_that_is_not_a_string_is_rejected_before_the_applier(self):
        self.write_record("20260930T180000000000-a.json", self.knowledge(base=5))
        res = self.run_drain()

        self.assertEqual(res["rejected"], 1)
        self.assertEqual(self.knowledge_applier.calls, [])

    def test_a_forget_reaches_the_applier_with_the_environments_retention_directory(self):
        self.write_record("20260930T180000000000-a.json", self.knowledge(op="forget"))
        res = self.run_drain()

        self.assertEqual(res["processed"], 1)
        self.assertNotIn("held", res, "the hold is gone: a forget is applied like an edit")
        self.assertEqual(self.lane_files("processed"), ["20260930T180000000000-a.json"])
        self.assertEqual(os.listdir(self.inbound), [])
        # Pinned whole, as the edit's is. The retention directory is DERIVED from the
        # environment being drained, one level inside it, and so is never inside the world
        # (TestRetentionFence is the other side of this: the world root it is checked against
        # is beside the spool here, which is what makes this the positive control).
        self.assertEqual(self.knowledge_applier.calls,
                         [["--handle", "0123456789abcdef", "--op", "forget", "--text=", "--base=",
                           f"--retention-dir={self.env / drain.RETENTION}", "--apply"]])
        self.assertEqual(self.applier.calls, [], "a knowledge record never reaches the verb applier")

    def test_the_retention_directory_is_named_retention(self):
        """The erase sweep and the operator look for forgotten text in <environment>/retention."""
        self.assertEqual(drain.RETENTION, "retention")

    def test_an_undo_reaches_the_applier_the_same_way(self):
        """The applier resolves an undo from the retention directory, so without it every undo
        is refused as not addressable: the wrong answer for a member whose item is retained."""
        self.write_record("20260930T180000000000-a.json", self.knowledge(op="undo"))
        res = self.run_drain()

        self.assertEqual(res["processed"], 1)
        self.assertEqual(self.knowledge_applier.calls,
                         [["--handle", "0123456789abcdef", "--op", "undo", "--text=", "--base=",
                           f"--retention-dir={self.env / drain.RETENTION}",
                           "--queued-at=2026-09-30T18:00:00", "--apply"]])

    def test_an_undo_whose_record_has_no_usable_send_time_is_forwarded_with_none(self):
        """The applier decides what an absent stamp means (it judges at apply time); the drain
        neither rejects the member's undo over the stamp nor forwards a value that is not text."""
        for i, stamp in enumerate((None, 5, {"at": "yesterday"})):
            record = self.knowledge(op="undo")
            if stamp is None:
                record.pop("queued_at")
            else:
                record["queued_at"] = stamp
            self.write_record(f"2026093018000000000{i}-a.json", record)

        res = self.run_drain()

        self.assertEqual(res["processed"], 3, res["records"])
        self.assertEqual([call[-2] for call in self.knowledge_applier.calls], ["--queued-at="] * 3)

    def test_each_environment_has_its_own_retention_directory(self):
        other = self.root / "env-two"
        (other / "inbound").mkdir(parents=True)
        (other / "inbound" / "20260930T180000000000-b.json").write_text(
            json.dumps(self.knowledge(op="forget")), encoding="utf-8")
        self.write_record("20260930T180000000000-a.json", self.knowledge(op="forget"))
        for env in (self.env, other):
            drain.drain_environment(env, apply=True, source="world", asp_id="asp-369",
                                    max_records=0, tmp_age_min=60)

        dirs = [next(a for a in call if a.startswith("--retention-dir="))
                for call in self.knowledge_applier.calls]
        self.assertEqual(dirs, [f"--retention-dir={self.env / drain.RETENTION}",
                                f"--retention-dir={other / drain.RETENTION}"])

    def test_malformed_records_are_rejected_before_the_applier(self):
        self.write_record("20260930T180000000000-a.json", self.knowledge(op="erase"))
        bad_text = self.knowledge()
        bad_text["text"] = 5
        self.write_record("20260930T180000000000-b.json", bad_text)
        self.write_record("20260930T180000000000-c.json", self.knowledge(handle=""))
        res = self.run_drain()

        self.assertEqual(res["rejected"], 3)
        self.assertEqual(self.knowledge_applier.calls, [])

    def test_applier_refusal_is_rejected_and_failure_stays_claimed(self):
        self.knowledge_applier.rc = 3
        self.write_record("20260930T180000000000-a.json", self.knowledge())
        self.assertEqual(self.run_drain()["rejected"], 1)

        self.knowledge_applier.rc = 1
        self.write_record("20260930T180000000000-b.json", self.knowledge())
        self.assertEqual(self.run_drain()["failed"], 1)
        self.assertEqual(len(self.lane_files("processing")), 1)

    def test_an_applier_exit_with_a_message_is_a_failure_not_a_crash(self):
        """Both appliers share the mapping: sys.exit('<msg>') is rc 1, not int('<msg>')."""
        self.knowledge_applier = _ExitingApplier()
        self.applier = _ExitingApplier()
        self.write_record("20260930T180000000000-a.json", self.knowledge())
        self.write_record("20260930T180000000000-b.json", self.verb())
        res = self.run_drain()

        self.assertEqual(res["failed"], 2)
        self.assertEqual({r["detail"] for r in res["records"]}, {"applier rc=1"})
        self.assertEqual(len(self.lane_files("processing")), 2)


class TestKnowledgeOutcomeIsRecorded(DrainTestBase):
    """: a finished knowledge record says what became of the member's edit."""

    NAME = "20260930T180000000000-a.json"

    def finished(self, lane):
        return json.loads((self.env / lane / self.NAME).read_text(encoding="utf-8"))

    def test_an_applied_edit_is_marked_applied_and_keeps_every_queued_field(self):
        queued = self.knowledge()
        self.write_record(self.NAME, queued)
        res = self.run_drain()

        record = self.finished("processed")
        outcome = record.pop("outcome")
        self.assertEqual(record, queued, "the member's instruction is kept whole")
        self.assertEqual(outcome["result"], "applied")
        self.assertNotIn("new_handle", outcome)
        # An explicit offset: a browser reads a bare timestamp as its own local time.
        self.assertEqual(datetime.fromisoformat(outcome["at"]).utcoffset(), timedelta(0))
        self.assertEqual(res["records"][0]["outcome"], outcome)

    def test_a_refused_edit_names_the_applier_refusal(self):
        self.knowledge_applier = _StubApplier(rc=3, reported={"refused": "view_stale"})
        self.write_record(self.NAME, self.knowledge())
        res = self.run_drain()

        self.assertEqual(self.finished("rejected")["outcome"]["result"], "view_stale")
        self.assertEqual(res["records"][0]["detail"], "applier refused (rc=3): view_stale")

    def test_a_superseded_guardrail_records_its_new_handle_and_no_item_id(self):
        self.knowledge_applier = _StubApplier(rc=0, reported={
            "kind": "guardrail", "id": "guard-1", "superseded_by": "guard-3",
            "handle": "fedcba9876543210"})
        self.write_record(self.NAME, self.knowledge())
        self.run_drain()

        record = self.finished("processed")
        self.assertEqual(record["outcome"]["new_handle"], "fedcba9876543210")
        self.assertNotIn("guard-1", json.dumps(record))
        self.assertNotIn("guard-3", json.dumps(record))

    def test_a_forget_records_the_end_of_its_undo_window(self):
        until = "2026-11-02T18:00:01+00:00"
        self.knowledge_applier = _StubApplier(rc=0, reported={
            "kind": "node", "id": "acme-widgets", "forgotten": True, "undo_until": until})
        self.write_record(self.NAME, self.knowledge(op="forget"))
        res = self.run_drain()

        record = self.finished("processed")
        self.assertEqual(record["outcome"]["result"], "applied")
        self.assertEqual(record["outcome"]["undo_until"], until)
        self.assertEqual(res["records"][0]["outcome"], record["outcome"])
        self.assertNotIn("acme-widgets", json.dumps(record), "an item id never reaches the member")

    def test_an_applied_record_whose_report_has_no_window_records_none(self):
        """An edit and an undo have no window of their own to give."""
        self.knowledge_applier = _StubApplier(rc=0, reported={"restored": True})
        self.write_record(self.NAME, self.knowledge(op="undo"))
        self.run_drain()

        self.assertNotIn("undo_until", self.finished("processed")["outcome"])

    def test_a_refusal_carries_only_its_code_even_when_the_report_names_a_window(self):
        self.knowledge_applier = _StubApplier(rc=3, reported={
            "refused": "undo_expired", "undo_until": "2026-11-02T18:00:01+00:00"})
        self.write_record(self.NAME, self.knowledge(op="undo"))
        self.run_drain()

        outcome = self.finished("rejected")["outcome"]
        self.assertEqual(outcome["result"], "undo_expired")
        self.assertNotIn("undo_until", outcome)

    def test_a_record_the_drain_refuses_itself_is_marked_malformed(self):
        self.write_record(self.NAME, self.knowledge(base=5))
        self.run_drain()

        self.assertEqual(self.finished("rejected")["outcome"]["result"], "malformed_record")
        self.assertEqual(self.knowledge_applier.calls, [])

    def test_a_requeued_record_has_its_old_outcome_replaced(self):
        old = {"result": "applied", "at": "2026-09-30T18:00:01+00:00"}
        self.write_record(self.NAME, {**self.knowledge(), "outcome": old})
        self.knowledge_applier = _StubApplier(rc=3, reported={"refused": "view_stale"})
        self.run_drain()

        self.assertEqual(self.finished("rejected")["outcome"]["result"], "view_stale")

    def test_a_failed_edit_stays_claimed_without_an_outcome(self):
        self.knowledge_applier.rc = 1
        self.write_record(self.NAME, self.knowledge())
        self.run_drain()

        self.assertNotIn("outcome", self.finished("processing"))

    def test_a_verb_record_is_not_annotated(self):
        self.write_record(self.NAME, self.verb())
        self.run_drain()

        self.assertNotIn("outcome", self.finished("processed"))

    def test_the_rewrite_leaves_no_temp_file(self):
        self.write_record(self.NAME, self.knowledge())
        self.run_drain()

        self.assertEqual(self.all_files(), [self.NAME])

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_the_rewrite_keeps_the_record_readable_as_queued(self):
        """The temp file is created private (0600); the record keeps the writer's mode."""
        os.chmod(self.write_record(self.NAME, self.knowledge()), 0o644)
        self.run_drain()

        self.assertEqual(stat.S_IMODE((self.env / "processed" / self.NAME).stat().st_mode), 0o644)

    def test_a_failed_outcome_write_still_finishes_the_record(self):
        """Left claimed, an applied edit would be requeued and applied a second time."""
        self.write_record(self.NAME, self.knowledge())
        with mock.patch.object(drain, "_record_outcome",
                               side_effect=PermissionError("read-only lane")):
            res = self.run_drain()

        self.assertEqual(self.lane_files("processed"), [self.NAME])
        self.assertEqual(self.lane_files("processing"), [])
        self.assertIn("PermissionError", res["records"][0]["outcome_error"])

    def test_the_summary_line_shows_an_unrecorded_outcome(self):
        """A --json-less operator run reads only this line."""
        self.write_record(self.NAME, self.knowledge())
        out = io.StringIO()
        with mock.patch.object(drain, "_record_outcome",
                               side_effect=PermissionError("read-only lane")), \
                contextlib.redirect_stdout(out):
            drain.main(["--root", str(self.root), "--environment-key", "env-under-test",
                        "--apply"])

        self.assertIn("[outcome not recorded: PermissionError: read-only lane]", out.getvalue())


class TestRetentionFence(DrainTestBase):
    """: what a member forgot is kept OUTSIDE the world the resident reads.

    The check is made where the directory is derived, so the applier is never reached with a
    directory the resident could read. It fails closed: a world root this box cannot resolve
    is a refusal, not a pass. The record is left claimed in processing/ (FAILED), never
    rejected: the gap is on this side and the member's instruction is legitimate."""

    def refusal(self, op):
        """Queue one ``op`` record under a fresh name, drain, and return (result, name)."""
        self.seq = getattr(self, "seq", 0) + 1
        name = f"20260930T1800000000{self.seq:02d}-a.json"
        self.write_record(name, self.knowledge(op=op))
        return self.run_drain(), name

    def assert_every_retention_op_refused(self, fragment):
        for op in drain.RETENTION_OPS:
            with self.subTest(op=op):
                res, name = self.refusal(op)
                self.assertEqual(res["failed"], 1, res["records"])
                self.assertIn(fragment, res["records"][0]["detail"])
                self.assertIn(name, self.lane_files("processing"), "left claimed, never deleted")
                self.assertEqual(self.lane_files("rejected"), [], "a gap on this side is no rejection")
                self.assertEqual(self.knowledge_applier.calls, [], "the applier is never reached")

    def test_a_world_root_that_is_the_retention_directory_is_refused(self):
        self.world_root = self.env / drain.RETENTION
        self.assert_every_retention_op_refused("retention fence")

    def test_a_world_inside_the_retention_directory_is_refused(self):
        self.world_root = self.env / drain.RETENTION / "world"
        self.assert_every_retention_op_refused("retention fence")

    def test_a_retention_directory_inside_the_world_is_refused(self):
        """The case that would look fine and hide nothing: the resident reads, in its own
        world, the very text the member asked it to forget."""
        self.world_root = self.env
        self.assert_every_retention_op_refused("retention fence")

    def test_an_unresolvable_world_root_is_a_refusal_not_a_pass(self):
        self.world_root = None
        self.assert_every_retention_op_refused("cannot resolve this box's own world root")

    def test_an_edit_is_not_held_to_the_fence(self):
        """POSITIVE CONTROL: with the world root unresolvable an edit still reaches its
        applier, so the refusals above are about the retention directory and not about the
        drain. An edit keeps nothing and is handed no directory."""
        self.world_root = None
        self.write_record("20260930T180000000000-a.json", self.knowledge())
        res = self.run_drain()

        self.assertEqual(res["processed"], 1, res["records"])
        self.assertEqual(len(self.knowledge_applier.calls), 1)

    def test_no_directory_at_all_is_a_refusal(self):
        self.assertIn("no retention directory", drain._retention_refusal(None))

    def test_an_unresolvable_directory_is_a_refusal_not_a_pass(self):
        with mock.patch.object(Path, "resolve", side_effect=OSError("unreadable")):
            why = drain._retention_refusal(self.env / drain.RETENTION)

        self.assertIn("unresolvable retention directory", why)

    @unittest.skipIf(os.name == "nt", "symlinks need a privilege on Windows")
    def test_a_retention_directory_that_links_into_the_world_is_refused(self):
        """The directory is judged where it LEADS: a link to the resident's world is the
        resident's world."""
        self.world_root = self.root / "a-world"
        self.world_root.mkdir()
        (self.env / drain.RETENTION).symlink_to(self.world_root, target_is_directory=True)

        self.assertIn("retention fence", drain._retention_refusal(self.env / drain.RETENTION))


class TestOneDrainPerEnvironment(DrainTestBase):
    """: claiming a record by rename protects THAT record and nothing else, so two
    drains of one environment could each apply a different record of one item, and the
    applier's read-modify-write of a page or an index would race. One applying drain per
    environment at a time closes it: a drain that finds the environment held leaves its
    records queued for the next pass."""

    NAME = "20260930T180000000000-a.json"
    OTHER = "another-host:4242:7"

    @property
    def lock(self):
        return self.env / drain.LOCK_NAME

    def hold(self, age_seconds=0):
        """Another drain's lock, as LocalBackend writes one: a file holding its holder's id."""
        self.lock.write_text(self.OTHER, encoding="utf-8")
        if age_seconds:
            old = time.time() - age_seconds
            os.utime(self.lock, (old, old))

    def test_a_drain_that_finds_the_environment_held_leaves_every_record_queued(self):
        self.hold()
        self.write_record(self.NAME, self.knowledge(op="forget"))
        res = self.run_drain()

        self.assertEqual(res["busy"], 1)
        self.assertEqual((res["claimed"], res["processed"], res["rejected"], res["failed"]),
                         (0, 0, 0, 0))
        self.assertEqual(res["records"], [])
        self.assertEqual(res["stranded"], [])
        self.assertEqual(os.listdir(self.inbound), [self.NAME], "nothing was claimed")
        self.assertEqual(self.knowledge_applier.calls, [])
        self.assertEqual(self.lock.read_text(encoding="utf-8"), self.OTHER,
                         "a lock this drain did not take is never released by it")

    def test_a_drain_that_finds_the_environment_held_does_not_wait_for_it(self):
        """timeout 0: the drain goes away and its records drain on the next pass. Asserted on
        the call, because a wall clock flakes on a loaded box."""
        import storage_backend  # noqa: PLC0415
        real = storage_backend.LocalBackend.acquire_lock
        timeouts = []

        def spy(backend, lock_path, timeout=10, stale_seconds=30):
            timeouts.append(timeout)
            return real(backend, lock_path, timeout=timeout, stale_seconds=stale_seconds)

        self.hold()
        self.write_record(self.NAME, self.knowledge())
        with mock.patch.object(storage_backend.LocalBackend, "acquire_lock", spy):
            res = self.run_drain()

        self.assertEqual((res["busy"], timeouts), (1, [0]))

    def test_the_environment_is_locked_while_its_records_are_applied(self):
        seen = []
        lock = self.lock

        class Probe(_StubApplier):
            def main(self, argv, report=None):
                seen.append(lock.is_file())
                return super().main(argv, report)

        self.knowledge_applier = Probe(rc=0)
        self.write_record(self.NAME, self.knowledge(op="forget"))
        self.write_record("20260930T180000000001-b.json", self.knowledge(op="undo"))
        self.run_drain()

        self.assertEqual(seen, [True, True], "held for every record, in the environment's directory")

    def test_the_records_drain_on_the_next_pass_once_the_holder_is_gone(self):
        self.hold()
        self.write_record(self.NAME, self.knowledge(op="forget"))
        self.assertEqual(self.run_drain()["busy"], 1)
        self.lock.unlink()

        res = self.run_drain()
        self.assertEqual((res["busy"], res["processed"]), (0, 1))

    def test_a_drain_leaves_no_lock_behind(self):
        self.write_record(self.NAME, self.knowledge())
        self.run_drain()

        self.assertFalse(self.lock.exists())

    def test_the_lock_is_released_when_the_drain_raises(self):
        self.write_record(self.NAME, self.knowledge())
        with mock.patch.object(drain, "_record_files", side_effect=RuntimeError("spool unreadable")):
            with self.assertRaises(RuntimeError):
                self.run_drain()

        self.assertFalse(self.lock.exists(), "a drain that died must not strand the environment")

    def test_a_lock_left_by_a_dead_holder_is_broken(self):
        self.hold(age_seconds=drain.LOCK_STALE_SECONDS + 60)
        self.write_record(self.NAME, self.knowledge())
        err = io.StringIO()
        with contextlib.redirect_stderr(err):  # the backend says so there, on the rare branch
            res = self.run_drain()

        self.assertEqual((res["busy"], res["processed"]), (0, 1))
        self.assertIn("lock-stale-break", err.getvalue())
        self.assertFalse(self.lock.exists())

    def test_a_lock_younger_than_the_stale_age_is_respected(self):
        """The control for the test above: the same lock, not yet stale."""
        self.hold(age_seconds=drain.LOCK_STALE_SECONDS - 60)
        self.write_record(self.NAME, self.knowledge())
        res = self.run_drain()

        self.assertEqual((res["busy"], res["processed"]), (1, 0))
        self.assertEqual(self.lock.read_text(encoding="utf-8"), self.OTHER)

    def test_a_dry_run_takes_no_lock_and_is_not_stopped_by_one(self):
        self.write_record(self.NAME, self.knowledge())
        res = self.run_drain(apply=False)
        self.assertEqual(res["busy"], 0)
        self.assertFalse(self.lock.exists(), "a dry run writes nothing")

        self.hold(age_seconds=100)
        held_at = self.lock.stat().st_mtime
        res = self.run_drain(apply=False)
        self.assertEqual(res["busy"], 0)
        self.assertEqual(len(res["records"]), 1, "it still lists what an apply would do")
        self.assertEqual(self.lock.read_text(encoding="utf-8"), self.OTHER)
        self.assertEqual(self.lock.stat().st_mtime, held_at,
                         "nor does it keep another drain's lock alive")

    def test_a_long_drain_keeps_its_lock_from_going_stale(self):
        ages = []
        lock = self.lock

        class Slow(_StubApplier):
            def main(self, argv, report=None):
                ages.append(time.time() - lock.stat().st_mtime)
                # A backlog this long outlasts the stale age, one record at a time.
                old = time.time() - (drain.LOCK_STALE_SECONDS + 60)
                os.utime(lock, (old, old))
                return super().main(argv, report)

        self.knowledge_applier = Slow(rc=0)
        for i in range(3):
            self.write_record(f"2026093018000000000{i}-a.json", self.knowledge(op="forget"))
        res = self.run_drain()

        self.assertEqual(res["processed"], 3)
        self.assertLess(max(ages), 60, "the lock is touched before every record")

    def test_a_lock_that_vanishes_mid_drain_does_not_stop_the_drain(self):
        """Keeping the lock fresh is best effort: a lock already gone has nothing to show."""
        lock = self.lock

        class Vanishing(_StubApplier):
            def main(self, argv, report=None):
                lock.unlink(missing_ok=True)
                return super().main(argv, report)

        self.knowledge_applier = Vanishing(rc=0)
        self.write_record("20260930180000000000-a.json", self.knowledge(op="forget"))
        self.write_record("20260930180000000001-b.json", self.knowledge(op="undo"))
        res = self.run_drain()

        self.assertEqual(res["processed"], 2)
        self.assertFalse(self.lock.exists())

    def test_the_lock_is_per_environment(self):
        self.hold()
        other = self.root / "env-two"
        (other / "inbound").mkdir(parents=True)
        (other / "inbound" / self.NAME).write_text(json.dumps(self.knowledge()), encoding="utf-8")
        res = drain.drain_environment(other, apply=True, source="world", asp_id="asp-369",
                                      max_records=0, tmp_age_min=60)

        self.assertEqual((res["busy"], res["processed"]), (0, 1))
        self.assertFalse((other / drain.LOCK_NAME).exists())

    def test_busy_is_on_the_summary_line_and_is_not_a_failure(self):
        self.hold()
        self.write_record(self.NAME, self.knowledge())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = drain.main(["--root", str(self.root), "--environment-key", "env-under-test",
                             "--apply"])

        self.assertEqual(rc, 0, "the drain that holds the environment is doing the work")
        self.assertIn(" busy=1 ", out.getvalue())

    def test_a_second_drain_started_while_the_first_is_mid_record_stands_aside(self):
        entered, release = threading.Event(), threading.Event()

        class Blocking(_StubApplier):
            def main(self, argv, report=None):
                # Only the first record of the first drain waits, so a second drain that got
                # in anyway fails the assertions below at once instead of waiting out the event.
                if not entered.is_set():
                    entered.set()
                    if not release.wait(timeout=30):
                        raise TimeoutError("the test never released the first drain")
                return super().main(argv, report)

        self.knowledge_applier = Blocking(rc=0)
        self.write_record("20260930T180000000000-a.json", self.knowledge(op="forget"))
        self.write_record("20260930T180000000001-b.json", self.knowledge(op="undo"))
        first = []

        def first_drain():
            try:
                first.append(self.run_drain())
            except BaseException as exc:  # noqa: BLE001 - reported by the assertions below
                first.append(exc)

        worker = threading.Thread(target=first_drain)
        self.addCleanup(worker.join, 30)
        self.addCleanup(release.set)  # an assertion that fails must not leave it parked
        worker.start()
        self.assertTrue(entered.wait(timeout=30), "the first drain never reached its applier")

        second = self.run_drain()
        release.set()
        worker.join(timeout=30)

        self.assertEqual((second["busy"], second["claimed"], second["processed"]), (1, 0, 0))
        self.assertIsInstance(first[0], dict, first)
        self.assertEqual((first[0]["busy"], first[0]["processed"]), (0, 2))
        self.assertEqual([call[3] for call in self.knowledge_applier.calls], ["forget", "undo"],
                         "the first drain's two records, in order, and nothing from the second")


class TestUnprovisionedBox(DrainTestBase):
    """: a box that cannot resolve member handles must not REJECT a member's
    write. Both resolvers fold that case into "no such item", so the record is left in
    inbound/, unclaimed, and drains by itself once the box is provisioned."""

    def unset(self, var, value=None):
        """Within the test, ``var`` is removed (or set to ``value``) and restored after."""
        patch = mock.patch.dict(os.environ)
        patch.start()
        self.addCleanup(patch.stop)
        if value is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = value

    def test_a_knowledge_edit_without_the_handle_secret_stays_queued_unclaimed(self):
        self.unset("KNOWLEDGE_HANDLE_SECRET")
        self.write_record("20260930T180000000000-a.json", self.knowledge())
        res = self.run_drain()

        self.assertEqual(res["unprovisioned"], 1)
        self.assertEqual(res["claimed"], 0)
        self.assertEqual(os.listdir(self.inbound), ["20260930T180000000000-a.json"])
        self.assertEqual(self.lane_files("rejected"), [], "a gap on this side must not reject")
        self.assertEqual(self.knowledge_applier.calls, [])
        detail = res["records"][0]["detail"]
        self.assertIn("KNOWLEDGE_HANDLE_SECRET", detail)
        self.assertIn("ENVIRONMENT_ID", detail)

    def test_a_verb_with_a_blank_environment_id_stays_queued_unclaimed(self):
        self.unset("ENVIRONMENT_ID", "  ")
        self.write_record("20260930T180000000000-a.json", self.verb())
        res = self.run_drain()

        self.assertEqual(res["unprovisioned"], 1)
        self.assertEqual(os.listdir(self.inbound), ["20260930T180000000000-a.json"])
        self.assertEqual(self.applier.calls, [])

    def test_a_directive_still_drains_on_an_unprovisioned_box(self):
        """POSITIVE CONTROL: a directive carries its target, so it needs no handle."""
        self.unset("KNOWLEDGE_HANDLE_SECRET")
        self.unset("ENVIRONMENT_ID")
        self.write_record("20260930T180000000000-a.json", self.directive())
        res = self.run_drain()

        self.assertEqual(res["processed"], 1)
        self.assertEqual(res["unprovisioned"], 0)
        self.assertEqual(len(self.fake_rt.calls), 1)

    def test_a_queued_record_drains_once_the_box_is_provisioned(self):
        self.write_record("20260930T180000000000-a.json", self.knowledge())
        with mock.patch.dict(os.environ):
            os.environ.pop("ENVIRONMENT_ID")
            self.assertEqual(self.run_drain()["unprovisioned"], 1)

        res = self.run_drain()
        self.assertEqual(res["processed"], 1)
        self.assertEqual(len(self.knowledge_applier.calls), 1)
        self.assertEqual(os.listdir(self.inbound), [])

    def test_unprovisioned_sets_exit_2_and_is_on_the_summary_line(self):
        self.unset("KNOWLEDGE_HANDLE_SECRET")
        self.write_record("20260930T180000000000-a.json", self.verb())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = drain.main(["--root", str(self.root), "--environment-key", "env-under-test",
                             "--apply"])
        self.assertEqual(rc, 2, "a member instruction left queued must not report success")
        self.assertIn(" unprovisioned=1 ", out.getvalue())

    def test_a_check_that_cannot_answer_leaves_the_record_to_its_applier(self):
        """Unknown is not unprovisioned: when the export module cannot be loaded, the
        record goes on to its applier, which fails on the same import (FAILED, claimed)."""
        orig = drain._load_core_module

        def no_export(filename, module_name):
            if filename == "knowledge-export.py":
                raise ImportError("export unreadable")
            return orig(filename, module_name)

        drain._load_core_module = no_export
        self.addCleanup(setattr, drain, "_load_core_module", orig)
        self.assertIsNone(drain._handles_provisioned())
        self.write_record("20260930T180000000000-a.json", self.knowledge())
        res = self.run_drain()

        self.assertEqual(res["unprovisioned"], 0)
        self.assertEqual(len(self.knowledge_applier.calls), 1)


class _RealWorldBase(DrainTestBase):
    """One exposed node in a tmp world, and a base taken from the row the export
    publishes, which is what a member's browser hashes. The REAL knowledge applier
    runs against it."""

    NODE = "---\ntopic: Acme widgets\nlast_updated: 2026-01-01\n---\n\n# Acme widgets\n\nWidgets are blue.\n"

    def setUp(self):
        super().setUp()
        drain._load_knowledge_applier = self._orig_knowledge_loader
        import yaml  # noqa: PLC0415
        from knowledge_projection import item_handle, view_digest  # noqa: PLC0415

        wtmp = tempfile.TemporaryDirectory()
        self.addCleanup(wtmp.cleanup)
        self.world = Path(wtmp.name) / "world"
        self.world_root = self.world  # the fence is checked against THIS world
        tree = self.world / "knowledge" / "tree"
        (tree / "acme").mkdir(parents=True)
        self.node = tree / "acme" / "acme-widgets.md"
        self.node.write_text(self.NODE, encoding="utf-8")
        (tree / "_tree.yaml").write_text(yaml.safe_dump({
            "last_updated": "2026-01-01",
            "nodes": {"acme-widgets": {"file": "world/knowledge/tree/acme/acme-widgets.md",
                                       "summary": "Widgets.", "last_updated": "2026-01-01"}},
        }, sort_keys=False), encoding="utf-8")
        env = mock.patch.dict(os.environ, {"WORLD_PATH": str(self.world),
                                           "KNOWLEDGE_HANDLE_SECRET": "handle-secret-for-tests",
                                           "ENVIRONMENT_ID": "env-under-test",
                                           "MIND_SID": "sid-under-test"})
        env.start()
        self.addCleanup(env.stop)

        self.handle = item_handle("node", "acme-widgets", "handle-secret-for-tests",
                                  "env-under-test")
        spec = importlib.util.spec_from_file_location(
            "knowledge_export_for_drain_tests", DRAIN_PATH.parent / "knowledge-export.py")
        export = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(export)
        meta = os.environ.get("META_PATH")
        bundle = export.build_bundle(self.world, DRAIN_PATH.parents[2],
                                     extra_paths=(meta,) if meta else ())
        row = next(r for r in bundle.tree if r.get("handle") == self.handle)
        self.base = view_digest(row["body"])

    def outcome(self, lane, name=None):
        if name is None:
            [name] = self.lane_files(lane)
        return json.loads((self.env / lane / name).read_text(encoding="utf-8"))["outcome"]


class TestKnowledgeEditThroughTheRealApplier(_RealWorldBase):
    """The class above pins the argv the drain BUILDS; only the real applier can say it
    PARSES it (guard-920: the canonical binary with the production call shape)."""

    def test_an_edit_carrying_the_published_base_lands_in_the_world(self):
        self.write_record("20260930T180000000000-a.json",
                          self.knowledge(text="Widgets are green.", handle=self.handle,
                                         base=self.base))
        res = self.run_drain()

        self.assertEqual(res["processed"], 1, res["records"])
        self.assertTrue(self.node.read_text(encoding="utf-8").endswith("\n\nWidgets are green.\n"))

    def test_an_edit_whose_base_is_stale_is_rejected_and_writes_nothing(self):
        self.write_record("20260930T180000000000-a.json",
                          self.knowledge(text="Widgets are green.", handle=self.handle,
                                         base="0" * 64))
        res = self.run_drain()

        self.assertEqual(res["rejected"], 1, res["records"])
        self.assertEqual(self.node.read_text(encoding="utf-8"), self.NODE)

    def test_the_record_of_a_landed_edit_says_applied(self):
        self.write_record("20260930T180000000000-a.json",
                          self.knowledge(text="Widgets are green.", handle=self.handle,
                                         base=self.base))
        self.run_drain()

        outcome = self.outcome("processed")
        self.assertEqual(outcome["result"], "applied")
        self.assertNotIn("new_handle", outcome, "a page keeps its handle")

    def test_the_record_of_a_stale_edit_names_the_refusal(self):
        """The code crosses from the REAL applier: a stub cannot say the channel is wired."""
        self.write_record("20260930T180000000000-a.json",
                          self.knowledge(text="Widgets are green.", handle=self.handle,
                                         base="0" * 64))
        self.run_drain()

        self.assertEqual(self.outcome("rejected")["result"], "view_stale")

    def test_the_json_report_stays_parseable_when_a_real_edit_lands(self):
        """Measured before the fix: the real applier's "node <id>: edit -> ..." line came
        first, and json.loads failed at line 1, column 1."""
        self.write_record("20260930T180000000000-a.json",
                          self.knowledge(text="Widgets are green.", handle=self.handle,
                                         base=self.base))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            drain.main(["--root", str(self.root), "--environment-key", "env-under-test",
                        "--apply", "--json"])

        self.assertIn(": edit -> body", out.getvalue() + err.getvalue(), "control: it printed")
        self.assertEqual(json.loads(out.getvalue())["environments"][0]["processed"], 1)

    def test_an_edit_on_a_box_without_an_environment_id_is_queued_not_rejected(self):
        """The drain's check and the REAL resolver must agree: without the fix this record
        resolves to nothing, the applier refuses it (rc 3) and it is REJECTED."""
        self.write_record("20260930T180000000000-a.json",
                          self.knowledge(text="Widgets are green.", handle=self.handle,
                                         base=self.base))
        with mock.patch.dict(os.environ):
            os.environ.pop("ENVIRONMENT_ID")
            res = self.run_drain()

        self.assertEqual(res["unprovisioned"], 1, res["records"])
        self.assertEqual(res["rejected"], 0)
        self.assertEqual(os.listdir(self.inbound), ["20260930T180000000000-a.json"])
        self.assertEqual(self.node.read_text(encoding="utf-8"), self.NODE)


class TestForgetAndUndoThroughTheRealApplier(_RealWorldBase):
    """: the forget the drain used to hold, applied for real. The argv tests above
    pin what the drain BUILDS; only the real applier can say it parses it, finds the node,
    keeps its text OUTSIDE the world, and puts it back byte for byte."""

    FORGET = "20260930T180000000000-a.json"
    UNDO = "20260930T180000000001-b.json"

    def forget(self, name=None):
        self.write_record(name or self.FORGET, self.knowledge(op="forget", handle=self.handle))
        return self.run_drain()

    def undo(self, name=None):
        self.write_record(name or self.UNDO, self.knowledge(op="undo", handle=self.handle))
        return self.run_drain()

    def indexed(self):
        import yaml  # noqa: PLC0415
        tree = self.world / "knowledge" / "tree" / "_tree.yaml"
        return yaml.safe_load(tree.read_text(encoding="utf-8"))["nodes"]

    def retained(self):
        import knowledge_retention as kr  # noqa: PLC0415
        return [kr.retained_content(rec)
                for _path, rec in kr.list_records(str(self.env / drain.RETENTION))]

    def test_a_forget_hides_the_node_and_keeps_its_text_outside_the_world(self):
        res = self.forget()

        self.assertEqual(res["processed"], 1, res["records"])
        self.assertNotIn("acme-widgets", self.indexed(), "the index no longer lists the node")
        self.assertNotIn("Widgets are blue.", self.node.read_text(encoding="utf-8"))
        self.assertEqual(self.retained(), [self.NODE.encode("utf-8")],
                         "the page is kept whole, for the undo")
        for held in self.world.rglob("*"):
            if held.is_file():
                self.assertNotIn(b"Widgets are blue.", held.read_bytes(),
                                 f"the world still holds the forgotten text: {held}")

    def test_a_forget_records_when_it_can_be_taken_back(self):
        self.forget()
        outcome = self.outcome("processed")

        until = datetime.fromisoformat(outcome["undo_until"])
        at = datetime.fromisoformat(outcome["at"])
        self.assertEqual(until.utcoffset(), timedelta(0), "an explicit offset, as every stamp")
        # The ruling is 30 days from the forget, which came before the outcome was stamped.
        self.assertGreater(until - at, timedelta(days=29, hours=23))
        self.assertLessEqual(until - at, timedelta(days=30))

    def test_an_undo_puts_the_page_back_byte_for_byte_and_lists_the_node_again(self):
        self.forget()
        res = self.undo()

        self.assertEqual(res["processed"], 1, res["records"])
        self.assertEqual(self.node.read_bytes(), self.NODE.encode("utf-8"))
        self.assertEqual(self.indexed()["acme-widgets"]["summary"], "Widgets.")
        outcome = self.outcome("processed", self.UNDO)
        self.assertEqual(outcome["result"], "applied")
        self.assertNotIn("undo_until", outcome, "an undo has no window of its own")

    def test_an_undo_of_an_item_never_forgotten_is_refused_and_writes_nothing(self):
        res = self.undo()

        self.assertEqual(res["rejected"], 1, res["records"])
        self.assertEqual(self.outcome("rejected", self.UNDO)["result"], "not_addressable")
        self.assertEqual(self.node.read_text(encoding="utf-8"), self.NODE)

    def test_a_forget_sent_twice_still_leaves_the_page_to_undo(self):
        """A second click finds no node to forget (the first hid it) and is refused. It must
        not cost the first its undo."""
        self.forget()
        again = self.forget("20260930T180000000000-c.json")

        self.assertEqual(again["rejected"], 1, again["records"])
        self.assertEqual(self.outcome("rejected", "20260930T180000000000-c.json")["result"],
                         "not_addressable")
        self.undo()
        self.assertEqual(self.node.read_bytes(), self.NODE.encode("utf-8"))

    def test_the_members_list_shows_the_forget_with_its_window_and_the_undo_without(self):
        import knowledge_changes as kc  # noqa: PLC0415
        self.forget()
        self.undo()

        rows = {row["op"]: row for row in kc.list_changes(self.env)["changes"]}
        self.assertEqual(rows["forget"]["state"], "applied")
        self.assertEqual(rows["forget"]["undo_until"],
                         self.outcome("processed", self.FORGET)["undo_until"])
        self.assertEqual(rows["undo"]["state"], "applied")
        self.assertNotIn("undo_until", rows["undo"])


class TestTheErasureThroughTheDrain(_RealWorldBase):
    """ u4: the 30-day erase, reached the way production reaches it. The sweep's own
    behaviour is pinned in test_knowledge_erase_sweep.py; what is pinned here is the wiring: it
    runs after the queued records, inside the environment's lock, behind the same two fences a
    forget takes, and what it did reaches the summary line, the JSON and the exit code."""

    FORGET = "20260930T180000000000-a.json"

    def forget(self):
        self.write_record(self.FORGET, self.knowledge(op="forget", handle=self.handle))
        return self.run_drain()

    def retained_files(self):
        d = self.env / drain.RETENTION
        return sorted(p.name for p in d.iterdir()) if d.is_dir() else []

    def age_retention(self, days=31):
        """Make every retained record as old as a forget from ``days`` ago. The digest covers the
        retained text only, so a record rewritten with earlier stamps still reads."""
        import knowledge_retention as kr  # noqa: PLC0415
        then = datetime.now(tz=timezone.utc) - timedelta(days=days)
        for path, record in kr.list_records(str(self.env / drain.RETENTION)):
            record["forgotten_at"] = then.replace(microsecond=0).isoformat()
            record["undo_until"] = (then + timedelta(days=kr.RETENTION_DAYS)).replace(microsecond=0).isoformat()
            path.write_bytes(json.dumps(record, sort_keys=True).encode("utf-8"))

    def cli(self, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = drain.main(["--environment-key", self.env.name, "--root", str(self.root),
                             "--aspiration", "asp-369", *extra])
        return rc, out.getvalue()

    def test_a_forget_still_inside_its_window_is_left_alone_by_the_next_drain(self):
        self.forget()
        before = self.retained_files()

        res = self.run_drain()

        self.assertEqual((res["erased"], res["erase_failed"], res["erase_pending"]), (0, 0, 0))
        self.assertEqual(self.retained_files(), before)
        self.assertFalse((self.env / "erasures.jsonl").exists())

    def test_an_expired_forget_is_erased_by_the_next_drain_and_leaves_a_receipt_without_its_text(self):
        self.forget()
        self.age_retention()

        res = self.run_drain()

        self.assertEqual((res["erased"], res["erase_failed"], res["erase_pending"]), (1, 0, 0), res["erasures"])
        self.assertEqual(self.retained_files(), [], "the retained copy is gone")
        for held in list(self.env.rglob("*")) + list(self.world.rglob("*")):
            if held.is_file():
                self.assertNotIn(b"Widgets are blue.", held.read_bytes(), f"text survives in {held}")
        receipts = [json.loads(ln) for ln in (self.env / "erasures.jsonl").read_text().splitlines()]
        self.assertEqual([r["event"] for r in receipts], ["erasing", "erased"])
        self.assertNotIn("acme-widgets", json.dumps(receipts), "an item id is not in a receipt")
        self.assertEqual([e["action"] for e in res["erasures"]], ["erased"])

    def test_the_erase_runs_after_the_queued_records_in_the_same_pass(self):
        """A forget sent in the same drain as the expiry of an older one: both land, the record
        loop first."""
        self.forget()
        self.age_retention()
        # The forget below has nothing left to forget (the node is already gone) and is refused,
        # which is enough to prove the loop ran before the sweep reported.
        self.write_record("20260930T180000000009-z.json", self.knowledge(op="forget", handle=self.handle))

        res = self.run_drain()

        self.assertEqual((res["rejected"], res["erased"]), (1, 1), res["records"])

    # --- an undo is judged by when it was sent, and the sweep waits for a queue (u7e) ---------------
    #
    # ``age_retention()`` makes the forget 31 days old, so the window closed a day ago. A stopped
    # home would then find the member's undo in ``inbound/`` and the retained copy due for erasure
    # in one pass.

    UNDO = "20260930T180000000001-b.json"

    def sent(self, delta):
        return (datetime.now(tz=timezone.utc) + delta).replace(microsecond=0).isoformat()

    def queue_undo(self, sent, name=None):
        record = self.knowledge(op="undo", handle=self.handle)
        if sent is None:
            record.pop("queued_at")
        else:
            record["queued_at"] = sent
        self.write_record(name or self.UNDO, record)

    def restored(self):
        return self.node.read_text(encoding="utf-8") == self.NODE

    def test_an_undo_sent_inside_the_window_is_restored_by_the_pass_that_would_have_erased_it(self):
        self.forget()
        self.age_retention()
        self.queue_undo(self.sent(timedelta(days=-2)))

        res = self.run_drain()

        self.assertEqual((res["processed"], res["rejected"]), (1, 0), res["records"])
        self.assertEqual(self.outcome("processed", self.UNDO)["result"], "applied")
        self.assertEqual((res["erased"], res["erase_failed"], res["erase_pending"]), (0, 0, 0), res["erasures"])
        self.assertEqual(res["erasures"], [])
        self.assertTrue(self.restored(), "the page is back byte for byte")
        import knowledge_retention as kr  # noqa: PLC0415
        [(_path, record)] = list(kr.list_records(str(self.env / drain.RETENTION)))
        self.assertFalse(kr.is_live(record), "the retained record holds a marker now, not the text")

    def test_the_same_undo_sent_after_the_window_is_refused_and_the_same_pass_erases(self):
        """The control for the test above: only the send time differs."""
        self.forget()
        self.age_retention()
        self.queue_undo(self.sent(timedelta(hours=-12)))

        res = self.run_drain()

        self.assertEqual((res["processed"], res["rejected"]), (0, 1), res["records"])
        self.assertEqual(self.outcome("rejected")["result"], "undo_expired")
        self.assertEqual((res["erased"], res["erase_failed"]), (1, 0), res["erasures"])
        self.assertEqual(self.retained_files(), [])
        self.assertFalse(self.restored())

    def test_an_undo_with_no_send_time_is_judged_when_the_pass_applies_it(self):
        self.forget()
        self.age_retention()
        self.queue_undo(None)

        res = self.run_drain()

        self.assertEqual(self.outcome("rejected")["result"], "undo_expired")
        self.assertEqual((res["rejected"], res["erased"]), (1, 1), res["erasures"])

    def test_a_pass_that_leaves_records_queued_behind_its_cap_does_not_erase(self):
        """A cap is an operator's (the fleet's wrapper passes none). The undo sorts after the cap,
        so this pass does not reach it, and erasing now would leave it nothing to restore."""
        self.forget()
        self.age_retention()
        self.write_record("20260930T180000000000-z.json",
                          self.knowledge(op="edit", handle=self.handle, base=self.base))
        self.queue_undo(self.sent(timedelta(days=-2)))
        kept = self.retained_files()

        capped = self.run_drain(max_records=1)

        self.assertEqual(os.listdir(self.inbound), [self.UNDO], "the undo is still queued")
        self.assertEqual((capped["erased"], capped["erase_failed"], capped["erase_pending"]), (0, 0, 0))
        self.assertEqual(self.retained_files(), kept, "nothing was erased")
        self.assertEqual([(e["kind"], e["record"], e["action"]) for e in capped["erasures"]],
                         [("-", "-", "deferred")])
        self.assertIn("1 queued record", capped["erasures"][0]["detail"])

        res = self.run_drain()

        self.assertEqual((res["processed"], res["erased"]), (1, 0), res["records"])
        self.assertTrue(self.restored(), "the next pass restores what the capped pass left queued")

    def test_a_pass_that_left_a_record_unclaimed_on_an_unprovisioned_box_does_not_erase(self):
        self.forget()
        self.age_retention()
        self.queue_undo(self.sent(timedelta(days=-2)))
        kept = self.retained_files()

        with mock.patch.dict(os.environ):
            os.environ.pop("KNOWLEDGE_HANDLE_SECRET")
            blind = self.run_drain()

        self.assertEqual((blind["unprovisioned"], blind["claimed"]), (1, 0))
        self.assertEqual(os.listdir(self.inbound), [self.UNDO])
        self.assertEqual((blind["erased"], blind["erase_failed"]), (0, 0))
        self.assertEqual(self.retained_files(), kept, "nothing was erased")
        self.assertEqual([e["action"] for e in blind["erasures"]], ["deferred"])

        res = self.run_drain()

        self.assertEqual((res["processed"], res["erased"]), (1, 0), res["records"])
        self.assertTrue(self.restored())

    def test_a_cap_that_truncates_nothing_does_not_defer_the_erase(self):
        """A cap larger than the queue leaves nothing behind it, so the sweep still runs."""
        self.forget()
        self.age_retention()

        res = self.run_drain(max_records=50)

        self.assertEqual((res["erased"], res["erase_failed"]), (1, 0), res["erasures"])
        self.assertNotIn("deferred", [e["action"] for e in res["erasures"]])

    def test_a_capped_pass_on_an_environment_that_never_forgot_has_nothing_to_defer(self):
        """No retention directory means no erase to wait on, so the pass says nothing about one."""
        for i in range(3):
            self.write_record(f"2026093018000000000{i}-z.json",
                              self.knowledge(op="edit", handle=self.handle, base=self.base))

        res = self.run_drain(max_records=1)

        self.assertEqual(len(os.listdir(self.inbound)), 2, "two records are left behind the cap")
        self.assertEqual(res["erasures"], [])
        self.assertFalse((self.env / drain.RETENTION).exists())

    def test_an_uncapped_pass_with_nothing_left_behind_adds_no_deferred_entry(self):
        """The positive control for the deferral: the sweep still runs when nothing waits."""
        self.forget()
        self.age_retention()

        res = self.run_drain()

        self.assertEqual((res["erased"], res["erase_failed"]), (1, 0), res["erasures"])
        self.assertNotIn("deferred", [e["action"] for e in res["erasures"]])

    def test_the_summary_line_the_json_and_the_exit_code_carry_the_erase(self):
        self.forget()
        self.age_retention()

        rc, line = self.cli("--apply")

        self.assertEqual(rc, 0)
        self.assertIn(" erased=1 erase_pending=0 erase_failed=0", line)
        self.assertIn("erase node ", line)

    def test_the_json_names_each_erase_by_a_random_id_and_holds_no_text_or_id(self):
        self.forget()
        self.age_retention()

        rc, text = self.cli("--apply", "--json")

        payload = json.loads(text)
        env = payload["environments"][0]
        self.assertEqual((rc, env["erased"], env["erase_failed"], env["erase_pending"]), (0, 1, 0, 0))
        self.assertEqual(len(env["erasures"]), 1)
        import knowledge_retention as kr  # noqa: PLC0415
        for forbidden in ("acme-widgets", "Widgets are blue.", kr.record_name("node", "acme-widgets")):
            self.assertNotIn(forbidden, text)
        self.assertRegex(env["erasures"][0]["record"], r"^[0-9a-f]{16}$")

    def test_a_dry_run_says_what_it_would_erase_and_changes_nothing(self):
        self.forget()
        self.age_retention()
        before = {p: p.read_bytes() for p in self.env.rglob("*") if p.is_file()}

        res = self.run_drain(apply=False)
        rc, line = self.cli()

        self.assertEqual((res["would_erase"], res["erased"]), (1, 0))
        self.assertEqual({p: p.read_bytes() for p in self.env.rglob("*") if p.is_file()}, before)
        self.assertEqual(rc, 0)
        self.assertIn("would_erase=1", line)
        self.assertIn("(dry run", line)

    def test_an_environment_another_drain_holds_is_not_swept(self):
        self.forget()
        self.age_retention()

        lock = self.env / drain.LOCK_NAME
        lock.write_text("another-host:4242:7", encoding="utf-8")  # another drain's lock, as LocalBackend writes one
        res = self.run_drain()

        self.assertEqual((res["busy"], res["erased"]), (1, 0))
        self.assertEqual(len(self.retained_files()), 1, "the holder's pass, not this one, owns the sweep")
        lock.unlink()
        self.assertEqual(self.run_drain()["erased"], 1, "and the next pass finishes it")

    def test_an_environment_that_never_forgot_is_not_swept_and_checks_no_fence(self):
        """An idle box has no obligation, so a fence that would refuse it reports nothing."""
        with mock.patch.object(drain, "_destination_fence", lambda root: "refused: not this box's"):
            res = self.run_drain()

        self.assertEqual((res["erased"], res["erase_failed"], res["erase_pending"]), (0, 0, 0))
        self.assertEqual(res["erasures"], [])

    def test_a_fence_that_refuses_fails_the_erase_loudly_and_deletes_nothing(self):
        self.forget()
        self.age_retention()

        for patched, reason in ((("_destination_fence", lambda root: "destination fence: not this box"),
                                 "destination fence"),
                                (("_retention_refusal", lambda path: "retention fence: inside the world"),
                                 "retention fence")):
            with self.subTest(reason), mock.patch.object(drain, patched[0], patched[1]):
                res = self.run_drain()
                rc, _line = self.cli("--apply")

                self.assertEqual((res["erased"], res["erase_failed"]), (0, 1))
                self.assertIn(reason, res["erasures"][0]["detail"])
                self.assertEqual(rc, 2)
                self.assertEqual(len(self.retained_files()), 1)

    def test_an_applier_the_sweep_was_not_written_against_fails_the_erase_without_taking_the_drain_down(self):
        self.forget()
        self.age_retention()

        with mock.patch.object(drain, "_load_knowledge_applier", lambda: _StubApplier(rc=0)):
            res = self.run_drain()

        self.assertEqual((res["erased"], res["erase_failed"]), (0, 1))
        self.assertTrue(res["erasures"][0]["detail"].startswith("the erase sweep refused: "), res["erasures"])
        self.assertIn("the knowledge applier lacks", res["erasures"][0]["detail"])
        self.assertEqual(len(self.retained_files()), 1)

    def test_an_unset_world_path_is_a_failed_erase_not_a_dead_drain(self):
        self.forget()
        self.age_retention()

        with mock.patch.dict(os.environ):
            os.environ.pop("WORLD_PATH", None)
            os.environ.pop("MIND_WORLD", None)
            res = self.run_drain()

        self.assertEqual((res["erased"], res["erase_failed"]), (0, 1))
        self.assertIn("could not resolve the world", res["erasures"][0]["detail"])
        self.assertEqual(len(self.retained_files()), 1)

    def test_an_erase_that_is_due_and_cannot_be_done_exits_2_and_is_in_the_line(self):
        """A page that is neither the retained copy nor the tombstone cannot be shown to be the
        member's text, so its expired retained record is reported every pass and keeps the exit code
        at 2: an overdue promise to a member must not read as clean."""
        self.forget()
        self.age_retention()
        self.node.write_text("---\ntopic: Acme widgets\n---\n\nSomebody else wrote this page.\n", encoding="utf-8")

        res = self.run_drain()
        rc, line = self.cli("--apply")

        self.assertEqual((res["erased"], res["erase_pending"], res["erase_failed"]), (0, 1, 0))
        self.assertEqual(rc, 2)
        self.assertIn(" erase_pending=1 ", line)
        self.assertEqual(len(self.retained_files()), 1)

    def test_an_exception_out_of_the_sweep_is_reported_by_its_type_and_never_its_message(self):
        self.forget()
        self.age_retention()
        import knowledge_erase  # noqa: PLC0415

        def boom(*args, **kwargs):
            raise ValueError("Widgets are blue. a message that quotes the text")

        with mock.patch.object(knowledge_erase, "sweep", boom):
            res = self.run_drain()
            _rc, line = self.cli("--apply")

        self.assertEqual((res["erased"], res["erase_failed"]), (0, 1))
        self.assertEqual(res["erasures"][0]["detail"], "the erase sweep raised ValueError")
        self.assertNotIn("Widgets are blue.", line)
        self.assertEqual(len(self.retained_files()), 1)

    def test_a_sweep_that_cannot_be_loaded_is_a_failed_erase_not_a_dead_drain(self):
        self.forget()
        self.age_retention()

        with mock.patch.dict(sys.modules, {"knowledge_erase": None}):
            res = self.run_drain()

        self.assertEqual((res["erased"], res["erase_failed"]), (0, 1))
        self.assertTrue(res["erasures"][0]["detail"].startswith("the erase sweep could not load: "),
                        res["erasures"])
        self.assertEqual(len(self.retained_files()), 1)

    def test_an_applying_drain_shows_its_lock_is_held_while_it_erases_and_a_dry_run_does_not(self):
        """A pass that outlasts LOCK_STALE_SECONDS on a slow daemon would otherwise be taken for a
        dead drain and run beside a second one."""
        self.forget()
        self.age_retention()

        with mock.patch.object(drain, "_keep_lock_fresh") as fresh:
            self.run_drain(apply=False)
            self.assertEqual(fresh.call_count, 0, "a dry run holds no lock, so it has none to show")
            res = self.run_drain()

        self.assertEqual(res["erased"], 1)
        self.assertGreaterEqual(fresh.call_count, 1, "the lock is shown before each record the sweep settles")

    def test_a_sweep_that_exits_is_reported_by_its_status_and_never_its_message(self):
        self.forget()
        self.age_retention()
        import knowledge_erase  # noqa: PLC0415

        def exits_with_text(*args, **kwargs):
            sys.exit("Widgets are blue. a message that quotes the text")

        def exits_with_status(*args, **kwargs):
            sys.exit(3)

        with mock.patch.object(knowledge_erase, "sweep", exits_with_text):
            quoted = self.run_drain()
        with mock.patch.object(knowledge_erase, "sweep", exits_with_status):
            coded = self.run_drain()

        self.assertEqual(quoted["erasures"][0]["detail"], "the erase sweep exited")
        self.assertEqual(coded["erasures"][0]["detail"], "the erase sweep exited: 3")
        self.assertEqual((quoted["erase_failed"], coded["erase_failed"]), (1, 1))
        self.assertEqual(len(self.retained_files()), 1)


if __name__ == "__main__":
    unittest.main()
