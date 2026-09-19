""" — the wm-prune eviction path, and why it must NOT share the
append path's policy.

Two eviction sites act on the same capped capture lanes:

  * APPEND  (wm.py::cmd_append via enforce_slot_limit; wm_write.py append
    endpoint inline) — evict UNFLAGGED first, with a reserved unflagged floor
    (g-306-293 / g-306-308 / g-306-316). The daemon deliberately keeps its copy
    INLINE: test_capture_fast_lane.py asserts `key=_eviction_sort_key` appears
    literally inside append_slot, so that a mirrored-but-never-called helper
    cannot pass a definition check while changing nothing at runtime.
  * PRUNE   (wm.py::cmd_prune / wm_write.py prune endpoint) uses pure FIFO
    by _item_ts, flag-neutral.

That looks like drift and invites de-duplication. It is not drift. For a Body
that has not CLOSED, `load_bearing` is the ONLY delivery channel — the fast
lane mirrors flagged entries out, unflagged ones are reachable only at
generalize-down — so unflagged == UNDELIVERED and flagged == a redundant
second copy. A policy that evicts unflagged first therefore destroys the only
copy while keeping the duplicate, and on the prune path it strictly loses
captures that FIFO would have kept.

test_unifying_the_two_policies_loses_undelivered_captures is the mutation
proof: it fails if anyone routes prune through enforce_slot_limit.
"""
import datetime
import importlib.util
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]
CLI = REPO / "core" / "scripts" / "wm.py"
DAEMON = REPO / "mind_api" / "src" / "endpoints" / "wm_write.py"


def _load_cli():
    spec = importlib.util.spec_from_file_location("wm_cli_under_test", CLI)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wm = _load_cli()


def _fifo(arr, limit):
    """The prune policy, extracted for comparison: flag-neutral, oldest-first."""
    if len(arr) <= limit:
        return 0
    arr.sort(key=lambda x: x.get("_item_ts", "0000") if isinstance(x, dict) else "0000")
    n = 0
    while len(arr) > limit:
        arr.pop(0)
        n += 1
    return n


def _lane(n_flagged, n_unflagged, flagged_older=True):
    order = (["F"] * n_flagged + ["U"] * n_unflagged) if flagged_older else (
        ["U"] * n_unflagged + ["F"] * n_flagged)
    return [{"load_bearing": k == "F", "_item_ts": f"{i + 1:04d}", "k": k}
            for i, k in enumerate(order)]


def _undelivered_lost(arr_after, n_unflagged_before):
    return n_unflagged_before - sum(1 for x in arr_after if x["k"] == "U")


# Measured 2026-08-22 (echo, cc-03). limit=10 => unflagged_floor=2.
# (n_flagged, n_unflagged, flagged_older, fifo_loss, shared_policy_loss)
SCENARIOS = [
    (8, 4, True, 0, 2),
    (4, 8, True, 0, 2),
    (2, 10, True, 0, 2),
    (10, 2, True, 0, 0),
    (8, 4, False, 2, 2),
]


@pytest.mark.parametrize("nf,nu,older,fifo_loss,shared_loss", SCENARIOS)
def test_unifying_the_two_policies_loses_undelivered_captures(
        nf, nu, older, fifo_loss, shared_loss):
    """FIFO is never worse than the shared policy for undelivered captures.

    If this fails because shared_loss dropped, the append policy changed and
    the divergence may no longer be needed — re-measure before deleting it.
    """
    a = _lane(nf, nu, older)
    b = [dict(x) for x in a]

    _fifo(a, 10)
    wm.enforce_slot_limit(b, 10)

    assert _undelivered_lost(a, nu) == fifo_loss
    assert _undelivered_lost(b, nu) == shared_loss
    assert fifo_loss <= shared_loss, (
        "prune's FIFO must never lose more undelivered captures than the "
        "append policy — that is the whole reason the two differ")


def test_floor_is_two_at_limit_ten():
    """Pins the constant the scenario table above was measured against."""
    assert wm._unflagged_floor(10) == 2


def test_append_policy_still_reserves_a_floor_for_unflagged():
    """Positive control: the append path's floor is intact ().

    Without it a lane saturated with flagged entries would evict every
    unflagged newcomer.
    """
    arr = _lane(12, 2, flagged_older=True)
    wm.enforce_slot_limit(arr, 10)
    assert sum(1 for x in arr if not x["load_bearing"]) == 2


def _prune_block(path, helper_name):
    """Source of the array-limit block inside the prune function.

    Bounded by the `encoding_queue` handling that follows it in BOTH copies
    rather than by a fixed character count — a fixed window silently truncated
    the longer daemon block and made this module assert against text it could
    not see.
    """
    src = path.read_text(encoding="utf-8")
    idx = src.index("g-306-353: DELIBERATELY pure FIFO")
    end = src.index("encoding_queue", idx)
    raw = src[idx:end]
    # CODE ONLY — drop comment lines. The block's own comment explains at length
    # why prune must not adopt `enforce_slot_limit`, so a raw-text search for
    # that symbol matches the warning against it and fails on a correct file.
    # Asserting on prose instead of code is how a pin ends up inverted.
    block = "\n".join(l for l in raw.splitlines()
                      if not l.strip().startswith("#"))
    assert len(block) > 200, f"{path.name}: prune block slice looks truncated"
    return block, helper_name


@pytest.mark.parametrize("path,helper", [
    (CLI, "enforce_slot_limit"),
    # The daemon keeps its append policy INLINE (no helper to name), so the
    # meaningful daemon invariant is the SORT KEY: prune must never adopt the
    # flag-aware `_eviction_sort_key`. Naming a symbol the daemon does not
    # define would make this assertion vacuously true — a green test proving
    # nothing, which is worse than no test.
    (DAEMON, "_eviction_sort_key"),
])
def test_prune_does_not_adopt_the_append_policy(path, helper):
    """Structural pin on BOTH copies — the daemon is the LIVE path (guard-742),
    so a CLI-only pin would not protect production."""
    block, symbol = _prune_block(path, helper)
    assert re.search(r"\bsort\(key=lambda x: x\.get\(\"_item_ts\"", block), (
        f"{path.name}: prune must keep its flag-neutral FIFO sort")
    assert symbol not in block, (
        f"{path.name}: prune must NOT route through {symbol} — see the "
        "measured scenario table in this test module")


def test_daemon_prune_symbol_actually_exists_positive_control():
    """Guards the test above from going vacuous.

    `_eviction_sort_key` must be a real symbol in the daemon, or the
    `not in block` assertion would pass for any spelling at all.
    """
    assert "_eviction_sort_key" in DAEMON.read_text(encoding="utf-8"), (
        "daemon lost _eviction_sort_key — the prune pin above is now vacuous")


@pytest.mark.parametrize("path,needle", [
    (CLI, "capture_evictions"),
    (DAEMON, "_record_capture_evictions("),
])
def test_prune_persists_its_eviction_tally(path, needle):
    """Prune used to record evictions ONLY into the transient response report,
    so every capture entry it destroyed was invisible to capture_evictions —
    the counter built to make exactly this loss measurable (g-306-289), and
    made reset-surviving by g-306-355. A blind lane silently undercounts any
    future cap sizing that reads it.

    STRUCTURAL ONLY: this matches SOURCE TEXT, so it survives a regression that
    keeps the symbol but breaks the behaviour (moving the write above the
    dry_run guard, writing to a dict nothing persists, losing the call in a
    refactor). The behavioural half is the endpoint tests below (g-306-357).
    """
    block, _ = _prune_block(path, "")
    assert needle in block, (
        f"{path.name}: prune must persist its eviction count, not only report it")


# ---------------------------------------------------------------------------
#  — the END-TO-END path: endpoint -> eviction -> persisted counter.
#
# Everything above is a unit or a source-text grep. Neither can observe the one
# thing this counter exists for: that a real POST /v1/wm/prune against a real
# over-cap capture lane leaves a LARGER NUMBER ON DISK. The daemon copy is the
# live path (wm-prune.sh is daemon-only, no-python-cli-fallback.md), so it is
# the copy tested here.
#
# The cap is seeded into the fixture's OWN core/config/memory-pipeline.yaml
# rather than borrowed from the repo's. Two reasons: the condition under test
# must be CONSTRUCTED, not inherited from an ambient fixture (guard-4425), and
# _get_pruning_config replaces the whole pruning block, so seeding it also pins
# item_stale_minutes to {} — otherwise an item-age prune could supply some of
# the evictions and the arithmetic below would be measuring two mechanisms.
# ---------------------------------------------------------------------------

LANE = "spark_capture"          # a real CAPTURE_SLOT, not a stand-in
SEEDED_CAP = 5
SEEDED_ITEMS = 12
EXPECTED_EVICTED = SEEDED_ITEMS - SEEDED_CAP      # 7
PRE_EXISTING_TALLY = 3          # makes "increments" literal, not "assigns"

PRUNING_CONFIG = {
    "working_memory_pruning": {
        "stale_threshold_minutes": 30,
        "evict_threshold_minutes": 120,
        "array_limits": {LANE: SEEDED_CAP},
        "item_stale_minutes": {},
        "protected_slots": ["known_blockers", "knowledge_debt"],
    }
}


def _seed_wm(project_root, now_iso):
    """Write an over-cap capture lane plus a pre-existing tally, and return the
    WM path. slot_meta is FRESH so neither the stale-slot report nor the
    scalar-eviction predicate can contribute to what we measure."""
    import yaml

    wm_path = (project_root / "agents" / "alpha" / "session"
               / "working-memory.yaml")
    items = [{"goal_id": f"g-000-{i:02d}", "load_bearing": bool(i % 2),
              "_item_ts": f"2026-08-22T10:{i:02d}:00"}
             for i in range(SEEDED_ITEMS)]
    wm_path.write_text(
        yaml.safe_dump({
            "session_start": now_iso,
            "capture_evictions": {LANE: PRE_EXISTING_TALLY},
            "slots": {LANE: items},
            "slot_meta": {LANE: {"updated_at": now_iso,
                                 "accessed_at": now_iso, "update_count": 1}},
        }),
        encoding="utf-8",
    )
    return wm_path


def _post_prune(port, dry_run):
    import json
    import urllib.request

    url = f"http://127.0.0.1:{port}/v1/wm/prune"
    if dry_run:
        url += "?dry_run=1"
    req = urllib.request.Request(url, method="POST")
    req.add_header("X-Mind-Agent", "alpha")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _array_limit_prunes(body):
    """Count of array_limit evictions the endpoint REPORTS for our lane.

    Asserts the envelope shape first: the report is nested under "report", and
    reading pruned_items off the top level yields an empty list on every run —
    a zero from a parser written this turn is not a measurement (guard-2298).
    """
    assert "report" in body, f"unexpected prune response shape: {body!r}"
    return sum(1 for p in (body["report"].get("pruned_items") or [])
               if p.get("slot") == LANE and p.get("reason") == "array_limit")


def _run_prune(dry_run):
    """Spin a fresh in-process daemon over a fresh tmp world, prune, and return
    (reported_count, on-disk wm dict). Fresh per call by design — these tests
    drive state-mutating production code and must not share a fixture (rb-659).
    """
    import tempfile

    import yaml

    from _daemon_fixture import DaemonFixture

    with tempfile.TemporaryDirectory() as tmpd:
        world = pathlib.Path(tmpd) / "world"
        world.mkdir()
        with DaemonFixture(world, agent="alpha") as df:
            cfg = df.project_root / "core" / "config"
            cfg.mkdir(parents=True, exist_ok=True)
            (cfg / "memory-pipeline.yaml").write_text(
                yaml.safe_dump(PRUNING_CONFIG), encoding="utf-8")

            now_iso = datetime.datetime.now().isoformat()
            wm_path = _seed_wm(df.project_root, now_iso)

            body = _post_prune(df.port, dry_run)
            reported = _array_limit_prunes(body)
            after = yaml.safe_load(wm_path.read_text(encoding="utf-8"))
            return reported, after


def test_endpoint_prune_persists_the_eviction_count_to_disk():
    """Direction 1: an over-cap prune INCREMENTS capture_evictions on disk.

    Three assertions, not one. The counter alone would pass while the lane was
    left untouched or over-trimmed — a per-entity instrumentation count proves
    the code path ran, never that the result is right (rb-8725) — so the lane's
    surviving length is pinned beside it, and the reported count is pinned
    against the persisted delta so report and counter cannot drift apart.
    """
    reported, after = _run_prune(dry_run=False)

    assert reported == EXPECTED_EVICTED, (
        f"endpoint reported {reported} array_limit evictions for {LANE}, "
        f"expected {EXPECTED_EVICTED} — the seeded cap did not take effect, so "
        f"nothing below is a measurement of the eviction path")
    assert len(after["slots"][LANE]) == SEEDED_CAP, (
        f"{LANE} left at {len(after['slots'][LANE])} entries, expected the cap "
        f"({SEEDED_CAP}) — the count may be right while the lane is not")
    # WHICH entries survived, not just how many. Added at the 
    # fresh-eyes pass: the count and the tally are both satisfied by a
    # regression that evicts the NEWEST 7 instead of the oldest, and the
    # FIFO pins above cannot catch that — one exercises the policy in
    # isolation, the other reads source text. This is the only assertion in
    # the module that sees eviction ORDER through the live endpoint.
    survivors = [e["goal_id"] for e in after["slots"][LANE]]
    assert survivors == [f"g-000-{i:02d}"
                         for i in range(SEEDED_ITEMS - SEEDED_CAP, SEEDED_ITEMS)], (
        f"prune kept {survivors} — FIFO must drop the OLDEST entries, so the "
        f"survivors are the newest {SEEDED_CAP} in _item_ts order")
    assert after.get("capture_evictions", {}).get(LANE) == (
        PRE_EXISTING_TALLY + EXPECTED_EVICTED), (
        f"capture_evictions[{LANE}] is "
        f"{after.get('capture_evictions', {}).get(LANE)!r} on disk, expected "
        f"{PRE_EXISTING_TALLY + EXPECTED_EVICTED} — prune destroyed "
        f"{EXPECTED_EVICTED} capture entries without persisting the tally, "
        f"which is the g-306-289 blindness returning")


def test_endpoint_dry_run_prune_leaves_the_counter_untouched():
    """Direction 2: dry_run must NOT increment, and must NOT trim.

    The reported count is the positive control and is what makes this test
    non-vacuous: without it, a prune that took the wrong branch, read the wrong
    WM file, or never saw an over-cap lane at all would produce the same clean
    'nothing changed' and read as a pass (guard-2560 — 'I measured nothing' and
    'nothing is wrong' render identically).
    """
    reported, after = _run_prune(dry_run=True)

    assert reported == EXPECTED_EVICTED, (
        f"dry_run reported {reported} array_limit evictions for {LANE}, "
        f"expected {EXPECTED_EVICTED} — the eviction branch did not run, so "
        f"the untouched-state assertions below prove nothing")
    assert len(after["slots"][LANE]) == SEEDED_ITEMS, (
        f"dry_run trimmed {LANE} to {len(after['slots'][LANE])} on disk — a "
        f"dry run must not write")
    assert after.get("capture_evictions", {}).get(LANE) == PRE_EXISTING_TALLY, (
        f"dry_run moved capture_evictions[{LANE}] to "
        f"{after.get('capture_evictions', {}).get(LANE)!r} — a preview must not "
        f"inflate the counter that sizes future caps")


# ---------------------------------------------------------------------------
#  — ARCHIVE BEFORE DELETE on the prune path, and an eviction record
# that NAMES what it destroyed.
#
# The incident this section pins: a routine MAINTAIN call enforced
# array_limits.spark_capture and destroyed 2,198 worker observations
# (4,453,737 -> 121,862 bytes) with no archive, logging every one of them as
# the literal string '?'. Unrecoverable AND unauditable, from one call.
#
# The tests are ordered by how much each would have caught: the summary units
# catch the '?', the archive tests catch the destruction, and
# test_a_failed_archive_keeps_the_entries catches the regression that would
# quietly re-arm it — a fail-OPEN archive, which looks like robustness and is
# the defect.
# ---------------------------------------------------------------------------

# The two capture shapes that mattered, plus the shape that produced the '?'.
SPARK_ENTRY = {"observation": "selector has no per-role filter",
               "goal_id": "g-115-1", "_item_ts": "0001"}
EXP_ENTRY = {"execution_summary": "ran the census end to end",
             "_item_ts": "0002"}
NO_TEXT_ENTRY = {"goal_id": "g-115-3", "load_bearing": True, "_item_ts": "0003"}


def _old_array_limit_summary(removed):
    """The pre-fix expression, verbatim from the array_limit record.

    Kept as the POSITIVE CONTROL: without it, an assertion that the new
    summariser returns something useful cannot show that the old one did not,
    and this whole section would pass just as well against unchanged code.
    """
    return (str(removed.get("claim", removed.get("reason", "?")))[:80]
            if isinstance(removed, dict) else "?")


@pytest.mark.parametrize("entry,expected_fragment", [
    (SPARK_ENTRY, "selector has no per-role filter"),
    (EXP_ENTRY, "ran the census end to end"),
])
def test_evicted_summary_names_the_capture_it_dropped(entry, expected_fragment):
    """A capture entry's own headline field reaches the eviction record.

    The control is the point: both shapes returned a bare '?' from the pre-fix
    expression, which is how 2,198 destroyed observations left an audit trail
    that named none of them.
    """
    assert _old_array_limit_summary(entry) == "?", (
        "positive control failed: the pre-fix expression no longer returns '?' "
        "for this shape, so this test proves nothing about the fix")
    assert expected_fragment in wm.evicted_summary(entry), (
        f"evicted_summary({entry!r}) = {wm.evicted_summary(entry)!r} — the "
        f"record must name what was dropped")


def test_evicted_summary_never_returns_a_bare_question_mark():
    """An entry carrying NO known text field still names its keys.

    This is the half that keeps the fix from going stale. A capture lane whose
    shape _EVICT_SUMMARY_FIELDS does not yet cover is exactly the case the '?'
    hid; here the omission announces itself in the record reporting the loss.
    """
    assert _old_array_limit_summary(NO_TEXT_ENTRY) == "?", "positive control failed"
    summary = wm.evicted_summary(NO_TEXT_ENTRY)
    assert summary != "?" and "?" not in summary, (
        f"evicted_summary fell back to a question mark: {summary!r}")
    assert "g-115-3" in summary, "the goal_id is the one identifier this shape has"
    assert "load_bearing" in summary, (
        f"the keys fallback must name the keys actually present: {summary!r}")


def test_evicted_summary_is_bounded_and_single_line():
    """A 10 KB observation cannot turn the eviction report into the payload."""
    fat = {"observation": "x" * 10_000 + "\n\nsecond para", "_item_ts": "0004"}
    summary = wm.evicted_summary(fat)
    assert len(summary) <= 120, f"summary is {len(summary)} chars, cap is 120"
    assert "\n" not in summary, "an eviction record is one line per entry"


def test_archive_refuses_and_reports_false_when_the_sink_is_unwritable():
    """THE FAIL-CLOSED CONTRACT, at the unit level.

    False is not a status line — it is the caller's instruction to KEEP the
    entry. A regression that makes this return True on failure re-arms the
    silent destroy, and every other test in this file would still pass.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmpd:
        agent_dir = pathlib.Path(tmpd)
        # A DIRECTORY where the sink file belongs: the append raises, and the
        # helper must absorb it as "do not delete" rather than propagate.
        (agent_dir / wm.CAPTURE_EVICTION_ARCHIVE).mkdir()
        assert wm.archive_evicted_capture(
            agent_dir, "spark_capture", SPARK_ENTRY, "array_limit") is False, (
            "an unwritable sink must report False so the caller keeps the entry")
    assert wm.archive_evicted_capture(
        None, "spark_capture", SPARK_ENTRY, "array_limit") is False, (
        "an unresolved agent dir must report False, never silently skip the "
        "archive and return success")


# --- the END-TO-END half: the live daemon path (wm-prune.sh is daemon-only) ---

ARCHIVE_NAME = "capture-evictions-archive.jsonl"


def _archive_rows(project_root):
    import json as _json

    path = project_root / "agents" / "alpha" / ARCHIVE_NAME
    # is_file(), not exists(): the sabotage case puts a DIRECTORY at this path,
    # and exists() is True for it — read_text would then raise IsADirectoryError
    # and the fail-closed test would error out instead of measuring.
    if not path.is_file():
        return []
    return [_json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _run_prune_observing_archive(sabotage_sink=False):
    """Fresh daemon over a fresh tmp world; prune an over-cap capture lane and
    return (response body, on-disk wm dict, archived rows).

    Seeds the same over-cap lane as _run_prune above but with a text field per
    entry, so the archived CONTENT can be compared against what was in the lane
    rather than merely counted.
    """
    import tempfile

    import yaml

    from _daemon_fixture import DaemonFixture

    with tempfile.TemporaryDirectory() as tmpd:
        world = pathlib.Path(tmpd) / "world"
        world.mkdir()
        with DaemonFixture(world, agent="alpha") as df:
            cfg = df.project_root / "core" / "config"
            cfg.mkdir(parents=True, exist_ok=True)
            (cfg / "memory-pipeline.yaml").write_text(
                yaml.safe_dump(PRUNING_CONFIG), encoding="utf-8")

            now_iso = datetime.datetime.now().isoformat()
            wm_path = (df.project_root / "agents" / "alpha" / "session"
                       / "working-memory.yaml")
            items = [{"goal_id": f"g-000-{i:02d}",
                      "observation": f"worker capture {i}",
                      "_item_ts": f"2026-08-22T10:{i:02d}:00"}
                     for i in range(SEEDED_ITEMS)]
            wm_path.write_text(
                yaml.safe_dump({
                    "session_start": now_iso,
                    "slots": {LANE: items},
                    "slot_meta": {LANE: {"updated_at": now_iso,
                                         "accessed_at": now_iso,
                                         "update_count": 1}},
                }),
                encoding="utf-8",
            )
            if sabotage_sink:
                (df.project_root / "agents" / "alpha" / ARCHIVE_NAME).mkdir(
                    parents=True, exist_ok=True)

            body = _post_prune(df.port, dry_run=False)
            after = yaml.safe_load(wm_path.read_text(encoding="utf-8"))
            return body, after, _archive_rows(df.project_root)


def test_prune_archives_every_capture_entry_before_removing_it():
    """The headline outcome: nothing leaves the lane without a copy landing.

    Count, CONTENT and order are all pinned. A count alone passes against an
    archive of 7 empty rows — the original defect wearing a receipt.
    """
    body, after, rows = _run_prune_observing_archive()

    assert _array_limit_prunes(body) == EXPECTED_EVICTED, (
        "the seeded cap did not take effect — nothing below is a measurement")
    assert len(after["slots"][LANE]) == SEEDED_CAP, (
        f"{LANE} left at {len(after['slots'][LANE])}, expected the cap")
    assert len(rows) == EXPECTED_EVICTED, (
        f"{len(rows)} rows archived against {EXPECTED_EVICTED} entries "
        f"evicted — archive-before-delete means the two are equal, always")
    assert [r["entry"]["goal_id"] for r in rows] == [
        f"g-000-{i:02d}" for i in range(EXPECTED_EVICTED)], (
        "the archive must hold the OLDEST entries — the ones FIFO dropped")
    assert all(r["entry"]["observation"] == f"worker capture {i}"
               for i, r in enumerate(rows)), (
        "the archived row must carry the ENTRY, not just a note that one existed")
    for r in rows:
        assert r["slot"] == LANE and r["eviction_reason"] == "array_limit"
        assert "?" not in r["summary"], f"archived summary is a '?': {r!r}"


def test_a_failed_archive_keeps_the_entries():
    """THE LOAD-BEARING TEST: fail CLOSED, and terminate.

    When the sink cannot be written, the cap stays exceeded. That is the
    deliberate direction: a lane over its cap costs memory and says so in this
    very response, while a destroyed capture is unrecoverable — the WM store is
    git-untracked and the own-cloud recovery config is unreadable from the
    fleet identity, which archive-before-delete step 2 says to treat as ABSENT.

    The termination property is not padding. The eviction loop re-tests the
    same over-limit condition every pass, so a `continue` where the code says
    `break` spins forever on a failing sink — and what catches it is this test
    hanging rather than any assertion in it.
    """
    body, after, rows = _run_prune_observing_archive(sabotage_sink=True)

    assert rows == [], "the sabotaged sink must not have produced rows"
    assert len(after["slots"][LANE]) == SEEDED_ITEMS, (
        f"{LANE} left at {len(after['slots'][LANE])} entries — a prune that "
        f"cannot archive MUST NOT delete; keeping the lane over its cap is the "
        f"recoverable direction and destroying captures is not")
    assert _array_limit_prunes(body) == 0, (
        "nothing was evicted, so nothing may be reported as evicted")
    failures = body["report"].get("archive_failures") or []
    assert any(f.get("slot") == LANE for f in failures), (
        f"a refused eviction must be VISIBLE in the report, not silent: "
        f"{body['report']!r}")
    assert after.get("capture_evictions", {}).get(LANE) in (None, 0), (
        "nothing was destroyed, so the destruction tally must not move")


@pytest.mark.parametrize("path", [CLI, DAEMON])
def test_both_twins_archive_before_deleting_a_capture(path):
    """Structural parity: the daemon is the LIVE path and the CLI is its twin.

    A behavioural test can only reach the daemon copy (wm-prune.sh is
    daemon-only), so without this the CLI twin could lose the archive call and
    every other test in this file would stay green.
    """
    block, _ = _prune_block(path, "")
    # : the call is now the BATCHED plural. The invariant this pins is
    # unchanged — archive before delete — but the singular name must NOT come
    # back here: one archive call per evicted row re-PUTs the whole object per
    # row on a whole-object-PUT backend (guard-6134/guard-6904), which is the
    # O(N^2) defect that cost 4,829 versions / 231.3 GiB in a single day.
    assert "archive_evicted_captures(" in block, (
        f"{path.name}: the prune eviction loop must archive before it deletes, "
        f"via the BATCHED archive_evicted_captures")
    assert "archive_evicted_capture(" not in block, (
        f"{path.name}: the prune loop must not archive ONE ROW AT A TIME — that "
        f"is one whole-object PUT per row (g-115-9962)")
    assert "evicted_summary(" in block, (
        f"{path.name}: the eviction record must name what was dropped")
    src = path.read_text(encoding="utf-8")
    assert ("def archive_evicted_captures(" in src
            and "def archive_evicted_capture(" in src
            and "def evicted_summary(" in src), (
        f"{path.name}: must DEFINE the helpers it calls, not import them from "
        f"its twin — these two files are deliberate copies (guard-742)")


# ---------------------------------------------------------------------------
#  — the BATCHED archive. One prune, one write.
# ---------------------------------------------------------------------------
# The defect these pin: the prune loop archived ONE ROW PER CALL, and under a
# whole-object-PUT backend each call re-PUT the entire archive object, so N
# evictions cost N full PUTs of a growing file. Measured on the live store,
# UTC day 2026-09-17: 4,829 versions / 231.3 GiB of a single key, mean PUT
# 51,425,047 B against a 49,857,387 B object (guard-6134, guard-6904).


def test_archive_evicted_captures_is_ONE_write_for_N_rows(tmp_path, monkeypatch):
    """The whole point of the change: N rows must cost exactly ONE locked write.

    Counting the WRITES is the only assertion that distinguishes the fix from
    the defect — the archived CONTENT is identical either way, which is why a
    content-only test stayed green through the O(N^2) behaviour.
    """
    import _fileops
    calls = []
    monkeypatch.setattr(_fileops, "locked_append_jsonl_many",
                        lambda path, items: calls.append((path, list(items))))
    rows = [{"goal_id": f"g-000-{i:02d}", "_item_ts": f"2026-09-18T10:0{i}:00"}
            for i in range(6)]

    assert wm.archive_evicted_captures(str(tmp_path), LANE, rows,
                                       "array_limit") is True
    assert len(calls) == 1, (
        f"6 evicted rows produced {len(calls)} locked writes — the batched "
        f"archive must issue exactly ONE (one whole-object PUT), not one per row")
    assert len(calls[0][1]) == 6, "all six rows must ride in the single write"


def test_batched_archive_writes_N_DICT_LINES_not_one_array(tmp_path):
    """guard-5469, verified BY TYPE rather than by count.

    Passing a list to a per-record append writes one JSON array on one line:
    valid JSON, wrong shape, no error — and every per-record consumer then
    mis-handles it (the measured case crashed the FLEET MERGE, not the writer).
    A line COUNT cannot tell 6 records from one array of 6, so this asserts the
    parsed TYPE of every line.
    """
    import json
    rows = [{"goal_id": f"g-000-{i:02d}", "_item_ts": f"2026-09-18T10:0{i}:00"}
            for i in range(6)]
    assert wm.archive_evicted_captures(str(tmp_path), LANE, rows,
                                       "array_limit") is True

    archive = tmp_path / wm.CAPTURE_EVICTION_ARCHIVE
    lines = [ln for ln in archive.read_text(encoding="utf-8").splitlines()
             if ln.strip()]
    assert len(lines) == 6, f"expected 6 JSONL rows, got {len(lines)}"
    for ln in lines:
        parsed = json.loads(ln)
        assert isinstance(parsed, dict), (
            f"a JSONL row parsed as {type(parsed).__name__}, not dict — this is "
            f"the guard-5469 list-on-one-line shape")
        assert parsed["slot"] == LANE and parsed["eviction_reason"] == "array_limit"
    assert [p["entry"]["goal_id"] for p in (json.loads(ln) for ln in lines)] == \
        [r["goal_id"] for r in rows], "FIFO order must survive the batch"


def test_a_failed_batch_archive_keeps_EVERY_row(tmp_path, monkeypatch):
    """Outcome 2 at the contract level: False means KEEP, and it is all-or-nothing.

    The per-row loop could archive k rows and fail on k+1, leaving a batch half
    durable. One write cannot do that — so the caller never has to reason about
    a partial archive.
    """
    import _fileops

    def _boom(path, items):
        raise OSError("sink unavailable")

    monkeypatch.setattr(_fileops, "locked_append_jsonl_many", _boom)
    rows = [{"goal_id": f"g-000-{i:02d}"} for i in range(4)]

    assert wm.archive_evicted_captures(str(tmp_path), LANE, rows,
                                       "array_limit") is False, (
        "a failed archive MUST report False so the caller keeps the rows — "
        "fail-open here is a silent destroy")
    assert not (tmp_path / wm.CAPTURE_EVICTION_ARCHIVE).exists(), (
        "nothing may have been written by a failed batch")


def test_empty_victim_list_is_a_noop_that_still_reports_success(tmp_path):
    """No rows to archive is SUCCESS, not failure — returning False here would
    make the caller keep a lane it never needed to keep."""
    assert wm.archive_evicted_captures(str(tmp_path), LANE, [],
                                       "array_limit") is True
    assert not (tmp_path / wm.CAPTURE_EVICTION_ARCHIVE).exists()
