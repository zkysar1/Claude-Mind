""" — telemetry append streams are merge-registered; the snapshots are NOT.

WHY THIS EXISTS. world/telemetry/zakpod1-thermal.jsonl froze for 60 consecutive
`diverged_skipped` sweeps, and ONE DAY LATER a second, unrelated telemetry file
(`bridge-sessions/<port>/plugin-version.json`) turned up frozen at 1,045 sweeps on
a different box. These stores are governed-store write-class (b) — no reconciler
below the write — so an unregistered basename is not a soft default: the stale
If-Match fence is a PERMANENT wedge, and per-box repair is temporary because every
repair pushes ITS box's bytes to the same shared object.

The registration is a PATH-PATTERN branch (branch 7 of `merge_handler_for`), not a
basename entry, because the wedge is a property of the directory's write pattern
and a basename cure was demonstrably re-litigated within a day.

THE NEGATIVE HALF IS THE LOAD-BEARING HALF, and it is why this file exists rather
than a one-line assertion. The same tree holds 1,145 `*.json` SNAPSHOTS against 6
`*.jsonl` streams. A snapshot is a single JSON object (last-writer-wins); a
line-union over one concatenates two versions of a record into invalid JSON. So a
`telemetry/` prefix widened WITHOUT the extension test would turn this cure into
the next corruption. If someone later "simplifies" the branch to a bare prefix,
TestSnapshotsMustStayUnregistered goes red — that redness is the design, not a
stale expectation to edit.
"""
import json
import sys
from pathlib import Path

import pytest

# parents[1] IS core/scripts — one hop, no re-descent (the path bug called out in
# test_desync_warnings_merge_registration.py: parents[2] + re-appending
# "core/scripts" is INERT under pytest, whose conftest already fixes sys.path, and
# only fails when run-invisible-suites.sh executes the file DIRECTLY).
SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import coordination_merge as cm  # noqa: E402


# Every live *.jsonl under world/telemetry, with the writer that certifies it
# append-only. Read per rb-245 — NOT inferred from the name, which matters here
# because two of them are named "state" and "snapshot" and neither is one.
APPEND_STREAMS = [
    ("world/telemetry/zakpod1-thermal.jsonl",
     'zakpod1-thermal-record.sh: `sample >> "$CUR"`'),
    ("world/telemetry/studio-edit-state.jsonl",
     "roblox-edit-state.sh: `' >> \"$LEDGER\"`"),
    ("world/telemetry/cycle-sidecar-exits.jsonl",
     'cycle.sh: open(os.environ["OUTF"], "a")'),
    ("world/telemetry/bridge-liveness.jsonl",
     'cycle.sh: open(os.environ["OUTF"], "a")'),
    ("world/telemetry/s3-cost-telemetry.jsonl",
     "_fileops.locked_append_jsonl (also registered by basename, g-115-4288; "
     "this branch shadows that entry with the IDENTICAL handler)"),
    ("world/telemetry/ayoai-warmpool-snapshot.jsonl",
     "RESIDUAL: no writer found on this box or in the product estate — covered "
     "by inference from the directory's pattern, not by evidence"),
]

# Snapshots: single JSON objects, last-writer-wins. A line-union CORRUPTS these.
SNAPSHOTS = [
    "world/telemetry/bridge-sessions/28080/plugin-version.json",
    "world/telemetry/bridge-sessions/28081/ci-00067f27.json",
    "world/telemetry/some-future-snapshot.json",
]


def test_the_class_split_this_goal_decided():
    """The whole  verdict in one assertion: streams union, snapshots freeze.

    Top-level on purpose. run-invisible-suites.sh classifies a file with no
    top-level `def test_` as main()-style and runs it OUTSIDE pytest, where a
    class-only file silently joins a population it does not belong to.
    """
    expected = {p: cm.merge_append_only_jsonl for p, _ in APPEND_STREAMS}
    expected.update({p: None for p in SNAPSHOTS})
    actual = {p: cm.merge_handler_for(p) for p in expected}
    assert actual == expected, (
        "the telemetry class split changed. *.jsonl are append streams (line-union); "
        "*.json are snapshots (last-writer-wins) that a line-union would concatenate "
        "into invalid JSON. Re-derive against today's writers before editing this.")


class TestAppendStreamsAreRegistered:

    @pytest.mark.parametrize("path,writer", APPEND_STREAMS,
                             ids=[p.split("/")[-1] for p, _ in APPEND_STREAMS])
    def test_stream_resolves_to_the_line_union_handler(self, path, writer):
        assert cm.merge_handler_for(path) is cm.merge_append_only_jsonl, (
            f"{path} is no longer merge-registered. Certified append-only via {writer}. "
            "Unregistering restores the permanent-wedge shape (write-class (b): no "
            "reconciler below the write, so the fence never refreshes).")

    def test_a_future_telemetry_file_is_covered_without_touching_the_registry(self):
        """The entire reason this is a path-pattern branch and not a basename entry:
        a basename cure was re-litigated ONE DAY after it was proposed."""
        assert cm.merge_handler_for(
            "world/telemetry/some-telemetry-nobody-has-written-yet.jsonl"
        ) is cm.merge_append_only_jsonl

    def test_match_is_at_any_depth(self):
        """The second specimen sat two levels down (telemetry/bridge-sessions/<port>/),
        so a fixed-position `parts[-2] == "telemetry"` test would have missed it."""
        assert cm.merge_handler_for(
            "world/telemetry/nested/deeper/stream.jsonl"
        ) is cm.merge_append_only_jsonl


class TestSnapshotsMustStayUnregistered:
    """The half that a bare `telemetry/` prefix would break. 1,145 files."""

    @pytest.mark.parametrize("path", SNAPSHOTS,
                             ids=[p.split("/")[-1] for p in SNAPSHOTS])
    def test_snapshot_is_not_line_unioned(self, path):
        assert cm.merge_handler_for(path) is None, (
            f"{path} resolved a handler. Telemetry *.json files are SNAPSHOTS — one "
            "JSON object, last-writer-wins. A line-union concatenates two versions "
            "into invalid JSON. They are also session-UUID / port-keyed machine-local "
            "telemetry, which guard-1055's scope correction (g-115-3863) routes to "
            ".gitignore rather than to this registry. Do not widen branch 7 to a bare "
            "directory prefix.")

    def test_a_bare_prefix_would_have_failed_this(self):
        """Positive control for the assertion above: prove the negatives are
        reachable by the branch's *directory* test, so their exclusion is doing
        real work rather than passing vacuously."""
        for p in SNAPSHOTS:
            parts = p.split("/")
            assert "telemetry" in parts, (
                f"{p} is not under a telemetry/ dir, so it would be excluded for the "
                "wrong reason and this test would pass vacuously")


class TestTheHandlerActuallyResolvesADivergence:
    """Outcome 2: demonstrated, not asserted."""

    @staticmethod
    def _lines(*recs):
        return b"".join(
            (json.dumps(r, ensure_ascii=True) + "\n").encode() for r in recs)

    def test_both_diverged_state_reconciles_to_the_union(self):
        """The wedge shape: a shared baseline prefix plus per-box appends."""
        base = [{"ts": "2026-08-29T10:00:00", "gpu": 0, "c": 60},
                {"ts": "2026-08-29T10:00:30", "gpu": 0, "c": 61}]
        local = self._lines(*base, {"ts": "2026-08-29T10:01:00", "gpu": 0, "c": 62})
        remote = self._lines(*base, {"ts": "2026-08-29T10:01:30", "gpu": 0, "c": 63})

        merged = cm.merge_handler_for("world/telemetry/zakpod1-thermal.jsonl")(
            local, remote)
        recs = [json.loads(l) for l in merged.splitlines() if l.strip()]

        # baseline collapses to ONE copy; both boxes' appends survive
        assert len(recs) == 4, recs
        assert [r["c"] for r in recs] == [60, 61, 62, 63], "must stay chronological"

    def test_merge_is_commutative(self):
        """guard-907. A non-commutative handler makes the reconcile depend on which
        box happens to PUT first, which is exactly the nondeterminism the fence
        exists to prevent."""
        a = self._lines({"ts": "2026-08-29T10:00:00", "v": 1},
                        {"ts": "2026-08-29T10:02:00", "v": 3})
        b = self._lines({"ts": "2026-08-29T10:00:00", "v": 1},
                        {"ts": "2026-08-29T10:01:00", "v": 2})
        h = cm.merge_handler_for("world/telemetry/bridge-liveness.jsonl")
        assert h(a, b) == h(b, a)

    def test_union_preserves_order_and_dedupes_exact_lines(self):
        """Outcome 1's two named properties, separately."""
        dup = {"ts": "2026-08-29T09:00:00", "x": 1}
        h = cm.merge_handler_for("world/telemetry/cycle-sidecar-exits.jsonl")
        merged = h(self._lines(dup, dup), self._lines(dup))
        assert len([l for l in merged.splitlines() if l.strip()]) == 1


def test_docstring_branch_count_matches_reality():
    """The stale-counter hazard, made executable.

    `merge_handler_for`'s docstring states how many path-pattern branches run
    before the _HANDLERS lookup, and its own text tells readers that a basename
    grep is NOT a complete classifier — which is only true if the enumeration is
    complete. An authoritative-sounding count that has silently gone stale is
    worse than no count, so the number and the enumeration are pinned together.
    """
    doc = cm.merge_handler_for.__doc__
    assert "EIGHT path-pattern branches" in doc, (
        "the branch count in merge_handler_for's docstring no longer reads EIGHT. "
        "If a branch was added or removed, update BOTH the count and the numbered "
        "list below it — they are the classifier's only index.")
    for n in range(1, 9):
        assert f"\n      {n}. " in doc, f"branch {n} missing from the enumeration"
