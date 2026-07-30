#!/usr/bin/env python3
"""Tests for the advisory add-time near-duplicate warning ().

Every test that touches a store uses a pytest tmp_path corpus, NEVER the live
one: guard-692 — the *-add.sh registration scripts are not idempotent, so a test
that exercised the real add path would append real records to the real store.

The suite is deliberately weighted toward two properties, because those are the
ones whose absence would make the feature worse than not shipping it:

  1. IT CAN FIRE (guard-1465). g-115-3035 was closed deep with verification:null
     while the capability did not exist. The sibling failure is shipping a check
     whose threshold sits above anything real, so it passes vacuously forever.
     `test_configured_thresholds_are_not_vacuous` pins the thresholds against
     measured corpus statistics so a later "tighten it a bit" cannot silently
     make the warning unreachable.
  2. IT NEVER BLOCKS. The add must survive a helper that is broken, starved,
     or fed garbage.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SCRIPTS.parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import store_dupe_warn as sdw  # noqa: E402
from _bash_helpers import BASH  # noqa: E402  (guard-580: never a bare "bash" argv[0])

HELPER = SCRIPTS / "store_dupe_warn.py"


def _write_store(tmp_path: Path, filename: str, records) -> Path:
    p = tmp_path / filename
    p.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return p


def _run(record: dict, store: str, world_dir: Path):
    """Run the helper exactly as the wrapper does: JSON on stdin, flags on argv."""
    return subprocess.run(
        [sys.executable, str(HELPER), "--store", store, "--world-dir", str(world_dir)],
        input=json.dumps(record), capture_output=True, text=True, timeout=60,
    )


# --------------------------------------------------------------------------- #
# 1. Positive control — the warning must actually fire (guard-1465)
# --------------------------------------------------------------------------- #

def test_positive_control_near_verbatim_guardrail_warns(tmp_path):
    """A near-verbatim guardrail MUST produce the advisory, naming the existing id."""
    _write_store(tmp_path, "guardrails.jsonl", [
        {"id": "guard-0001", "status": "active",
         "rule": "Always pass grep -a when grepping a captured test log, because a "
                 "binary-classified log makes grep silently emit nothing."},
        {"id": "guard-0002", "status": "active",
         "rule": "Never deploy on a Friday without a rollback plan reviewed by a peer."},
    ])
    candidate = {"id": "guard-0003",
                 "rule": "Always pass grep -a when grepping a captured test log, since a "
                         "binary-classified log makes grep silently emit nothing at all."}
    r = _run(candidate, "guardrails", tmp_path)
    assert r.returncode == 0
    assert "guard-0001" in r.stderr, r.stderr
    assert "ADVISORY" in r.stderr
    assert "NOT blocked" in r.stderr
    # It must not name the unrelated entry.
    assert "guard-0002" not in r.stderr


def test_positive_control_reasoning_bank_title_warns(tmp_path):
    """rb comparison is TITLE-based; a restated title must fire."""
    _write_store(tmp_path, "reasoning-bank.jsonl", [
        {"id": "rb-0001", "status": "active",
         "title": "Gotcha: gradle --tests FQN filter reports no tests found; wildcard works",
         "content": "Long body text that differs completely between the two entries."},
    ])
    candidate = {"id": "rb-0002",
                 "title": "Gotcha: gradle --tests FQN filter reports no tests found; use wildcard",
                 "content": "An entirely unrelated body, written from scratch, sharing nothing."}
    r = _run(candidate, "reasoning-bank", tmp_path)
    assert r.returncode == 0
    assert "rb-0001" in r.stderr, r.stderr


def test_pattern_signatures_store_is_wired(tmp_path):
    """All three stores are configured — not just the two with richer corpora."""
    _write_store(tmp_path, "pattern-signatures.jsonl", [
        {"id": "sig-0001", "status": "active",
         "name": "retry storm after upstream timeout",
         "description": "Repeated client retries amplify an upstream timeout into an outage."},
    ])
    candidate = {"id": "sig-0002",
                 "name": "retry storm after upstream timeout",
                 "description": "Repeated client retries amplify an upstream timeout into outage."}
    r = _run(candidate, "pattern-signatures", tmp_path)
    assert r.returncode == 0
    assert "sig-0001" in r.stderr, r.stderr


def test_configured_thresholds_are_not_vacuous():
    """Pin thresholds against MEASURED corpus statistics (guard-1465).

    Measured on the live corpus 2026-07-28 (see the module docstring): the
    nearest-neighbour similarity p95 was 0.258 for guardrails (rule) and 0.300
    for reasoning-bank (title), with rb p99 at 0.545. A threshold at or above
    mdl_gate's 0.80 default is unreachable for these stores — adopting it would
    ship a warning that can never fire. A threshold at or below the p95 would
    fire on >5% of adds and become noise the reader learns to ignore.

    This test fails if someone later moves a threshold outside that band.
    """
    for store, cfg in sdw.STORES.items():
        th = cfg["threshold"]
        assert 0.30 < th < 0.80, f"{store} threshold {th} is outside the calibrated band"


def test_known_duplicate_pair_would_fire_at_configured_threshold():
    """End-to-end on the algorithm: a realistic near-verbatim pair clears the bar.

    Guards the specific regression where tokenisation or field selection changes
    and true duplicates silently stop scoring above threshold.
    """
    import mdl_gate
    a = "Gotcha: gradle --tests FQN filter reports no tests found; wildcard works"
    b = "Gotcha: gradle --tests FQN filter reports no tests found; use wildcard"
    sim = mdl_gate.jaccard(mdl_gate.tokenize(a), mdl_gate.tokenize(b))
    assert sim >= sdw.STORES["reasoning-bank"]["threshold"], (
        f"near-verbatim pair scored {sim:.3f}, below the configured "
        f"{sdw.STORES['reasoning-bank']['threshold']} — the warning would be vacuous")


# --------------------------------------------------------------------------- #
# 2. No false positives
# --------------------------------------------------------------------------- #

def test_novel_candidate_is_silent(tmp_path):
    _write_store(tmp_path, "guardrails.jsonl", [
        {"id": "guard-0001", "status": "active",
         "rule": "Always pass grep -a when grepping a captured test log."},
    ])
    candidate = {"id": "guard-0009",
                 "rule": "Rotate the signing certificate ninety days before expiry and "
                         "verify the new chain against the staging endpoint first."}
    r = _run(candidate, "guardrails", tmp_path)
    assert r.returncode == 0
    assert r.stderr.strip() == "", r.stderr


def test_retired_entries_are_not_matched(tmp_path):
    """A retired near-duplicate is not live knowledge; warning about it is noise."""
    _write_store(tmp_path, "guardrails.jsonl", [
        {"id": "guard-0001", "status": "retired",
         "rule": "Always pass grep -a when grepping a captured test log, because a "
                 "binary-classified log makes grep silently emit nothing."},
    ])
    candidate = {"id": "guard-0003",
                 "rule": "Always pass grep -a when grepping a captured test log, because a "
                         "binary-classified log makes grep silently emit nothing."}
    r = _run(candidate, "guardrails", tmp_path)
    assert r.returncode == 0
    assert r.stderr.strip() == ""


def test_record_does_not_match_itself(tmp_path):
    """A re-run, or a concurrent writer that already appended, must not report the
    record as a duplicate of itself."""
    rec = {"id": "guard-0001", "status": "active",
           "rule": "Always pass grep -a when grepping a captured test log, because a "
                   "binary-classified log makes grep silently emit nothing."}
    _write_store(tmp_path, "guardrails.jsonl", [rec])
    r = _run(rec, "guardrails", tmp_path)
    assert r.returncode == 0
    assert r.stderr.strip() == ""


# --------------------------------------------------------------------------- #
# 3. It never blocks — every degenerate input still exits 0, silently
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("payload", [
    "",                      # empty stdin
    "not json at all",       # unparseable
    "[]",                    # valid JSON, wrong shape
    "null",
    '{"id": "guard-1"}',     # dict with no signal field
    '{"rule": ""}',          # empty signal field
])
def test_degenerate_input_never_blocks(tmp_path, payload):
    _write_store(tmp_path, "guardrails.jsonl", [
        {"id": "guard-0001", "status": "active", "rule": "Some existing rule text here."},
    ])
    r = subprocess.run(
        [sys.executable, str(HELPER), "--store", "guardrails", "--world-dir", str(tmp_path)],
        input=payload, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"payload {payload!r} -> rc={r.returncode} stderr={r.stderr}"


def test_missing_store_file_never_blocks(tmp_path):
    r = _run({"id": "guard-1", "rule": "anything at all goes here"}, "guardrails", tmp_path)
    assert r.returncode == 0
    assert r.stderr.strip() == ""


def test_unknown_store_never_blocks(tmp_path):
    """argparse rejects the choice; the helper must still exit 0, not SystemExit(2)."""
    r = subprocess.run(
        [sys.executable, str(HELPER), "--store", "no-such-store", "--world-dir", str(tmp_path)],
        input='{"rule": "x"}', capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


def test_malformed_corpus_lines_are_skipped(tmp_path):
    """One bad line must not silence the advisory for the whole store."""
    p = tmp_path / "guardrails.jsonl"
    p.write_text(
        "{ this is not json\n"
        + json.dumps({"id": "guard-0001", "status": "active",
                      "rule": "Always pass grep -a when grepping a captured test log, because "
                              "a binary-classified log makes grep silently emit nothing."}) + "\n"
        + "\n",
        encoding="utf-8")
    candidate = {"id": "guard-0003",
                 "rule": "Always pass grep -a when grepping a captured test log, because a "
                         "binary-classified log makes grep silently emit nothing."}
    r = _run(candidate, "guardrails", tmp_path)
    assert r.returncode == 0
    assert "guard-0001" in r.stderr, r.stderr


# --------------------------------------------------------------------------- #
# 4. Wiring — structural, and explicitly labelled as such
# --------------------------------------------------------------------------- #
# guard-1451 / rb-5146: a test asserting text EXISTS in a source proves wiring,
# never that it RUNS. The subprocess tests above are what prove the behaviour;
# these only prove each wrapper is connected to it and cannot fail the add.

@pytest.mark.parametrize("wrapper,store", [
    ("guardrails-add.sh", "guardrails"),
    ("reasoning-bank-add.sh", "reasoning-bank"),
    ("pattern-signatures-add.sh", "pattern-signatures"),
])
def test_wrapper_invokes_helper_non_blocking(wrapper, store):
    text = (SCRIPTS / wrapper).read_text(encoding="utf-8")
    assert "store_dupe_warn.py" in text, f"{wrapper} does not invoke the helper"
    assert f"--store {store}" in text, f"{wrapper} passes the wrong --store"
    line = next(l for l in text.splitlines() if "--store " + store in l)
    assert "|| true" in line or "|| true" in text.split("store_dupe_warn.py")[1][:300], (
        f"{wrapper} must not let the advisory fail the add")


@pytest.mark.parametrize("wrapper", [
    "guardrails-add.sh", "reasoning-bank-add.sh", "pattern-signatures-add.sh",
])
def test_wrapper_still_parses(wrapper):
    r = subprocess.run([BASH, "-n", str(SCRIPTS / wrapper)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


# --------------------------------------------------------------------------- #
# 5. The wiring itself cannot abort the add — behavioural, not structural
# --------------------------------------------------------------------------- #
# Verification outcome 2 of  asks for proof that the record still lands
# when the advisory misbehaves. Running the REAL add would append to the real
# store (guard-692: the *-add.sh scripts are not idempotent) and, on an own-cloud
# box, a tmp-world write collides on the production S3 key (guard-955). So this
# reproduces the wrapper's exact shell composition — `set -euo pipefail`, then
# `printf BODY | launcher helper --store X || true`, then the append — with a
# DELIBERATELY BROKEN helper, and asserts the following statement still runs.
# That is the property that matters: no failure of the advisory can prevent the
# append that follows it.

_WRAPPER_FRAGMENT = """
set -euo pipefail
BODY='{{"id":"guard-1","rule":"some rule text"}}'
printf '%s' "$BODY" | {helper} --store guardrails || true
echo APPEND_RAN
"""


@pytest.mark.parametrize("helper,label", [
    ("false", "helper exits non-zero"),
    ("sh -c 'exit 3'", "helper exits 3"),
    ("sh -c 'echo boom >&2; exit 1'", "helper writes stderr then fails"),
    ("/nonexistent/helper/path", "helper binary missing"),
    ("sh -c 'kill -TERM $$'", "helper killed by signal"),
])
def test_broken_helper_cannot_abort_the_append(tmp_path, helper, label):
    script = tmp_path / "frag.sh"
    script.write_text(_WRAPPER_FRAGMENT.format(helper=helper), encoding="utf-8")
    r = subprocess.run([BASH, str(script)], capture_output=True, text=True, timeout=60)
    assert "APPEND_RAN" in r.stdout, (
        f"{label}: the append did NOT run — the advisory can abort the add. "
        f"rc={r.returncode} stdout={r.stdout!r} stderr={r.stderr!r}")


def test_fragment_would_catch_a_missing_guard(tmp_path):
    """Positive control for the test ABOVE (guard-1465 applied to my own test).

    Without `|| true`, `set -e` must abort before the append. If this passes-by-
    aborting assertion ever fails, the harness above has stopped being able to
    detect a regression and is itself vacuous.
    """
    script = tmp_path / "frag_noguard.sh"
    script.write_text(
        _WRAPPER_FRAGMENT.format(helper="false").replace(" || true", ""), encoding="utf-8")
    r = subprocess.run([BASH, str(script)], capture_output=True, text=True, timeout=60)
    assert "APPEND_RAN" not in r.stdout, (
        "the unguarded fragment still reached the append — this test harness "
        "cannot detect a missing `|| true`, so the test above proves nothing")


# --------------------------------------------------------------------------- #
# 5. Telemetry () — END-TO-END, not structural
#
# guard-1451: a test asserting the `_gate_log.log(...)` call exists in the
# source proves WIRING, never that it RUNS. Every test below spawns the helper
# and reads the firing back off disk.
#
# Hermeticity: MIND_META redirects the firing log to tmp_path (never the live
# meta store); GATE_LOG_ALLOW_PYTEST=1 lifts _gate_log's pytest no-op, which
# exists so ordinary tests cannot contaminate the retirement evaluator's corpus;
# STORAGE_BACKEND=local pins LocalBackend so the write cannot reach S3
# (guard-955 / rb-2983) and cannot divert into the own-cloud spool lane.
# --------------------------------------------------------------------------- #

_TELEMETRY_ENV = {"GATE_LOG_ALLOW_PYTEST": "1", "STORAGE_BACKEND": "local"}


def _run_logged(record: dict, store: str, world_dir: Path, meta_dir: Path,
                raw_stdin: str = None):
    import os
    env = dict(os.environ)
    env.update(_TELEMETRY_ENV)
    env["MIND_META"] = str(meta_dir)
    meta_dir.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [sys.executable, str(HELPER), "--store", store, "--world-dir", str(world_dir)],
        input=raw_stdin if raw_stdin is not None else json.dumps(record),
        capture_output=True, text=True, timeout=60, env=env,
    )


def _firings(meta_dir: Path):
    p = meta_dir / "gate-firings.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


@pytest.fixture
def seeded(tmp_path):
    """A one-entry guardrails corpus plus a separate meta dir for the firing log."""
    world = tmp_path / "world"
    world.mkdir()
    _write_store(world, "guardrails.jsonl", [
        {"id": "guard-0001", "status": "active",
         "rule": "Always pass grep -a when grepping a captured test log, because a "
                 "binary-classified log makes grep silently emit nothing."},
    ])
    return world, tmp_path / "meta"


def test_warned_case_lands_a_firing(seeded):
    """The WARNED case must be recorded, with the evidence that produced it."""
    world, meta = seeded
    r = _run_logged({"id": "g-new",
                     "rule": "Always pass grep -a when grepping a captured test log, since "
                             "a binary-classified log makes grep silently emit nothing at all."},
                    "guardrails", world, meta)
    assert r.returncode == 0
    assert "ADVISORY" in r.stderr           # it really did warn
    rows = _firings(meta)
    assert len(rows) == 1, rows
    assert rows[0]["gate_id"] == "store-dupe-warn"
    assert rows[0]["decision"] == "pass"    # fired, did NOT block — never "block"
    assert rows[0]["extra"]["nearest_id"] == "guard-0001"
    assert rows[0]["extra"]["similarity"] >= rows[0]["extra"]["threshold"]


def test_silent_case_also_lands_a_firing(seeded):
    """The SILENT case must be recorded TOO.

    This is the whole point of the goal. Logging only on warn makes the count
    uninterpretable — a low number cannot be told apart from a helper nobody
    invokes. Recording every invocation makes fired/invoked a RATE.
    """
    world, meta = seeded
    r = _run_logged({"id": "g-new",
                     "rule": "Rotate the signing certificate before its expiry window closes."},
                    "guardrails", world, meta)
    assert r.returncode == 0
    assert r.stderr.strip() == ""           # it really was silent
    rows = _firings(meta)
    assert len(rows) == 1, rows
    assert rows[0]["gate_id"] == "store-dupe-warn"
    assert rows[0]["decision"] == "noop"    # silent == no trigger matched


def test_near_miss_records_the_similarity_it_was_measured_against(seeded):
    """A sub-threshold neighbour must carry similarity + threshold.

    Without this pair, a future reader deciding whether to retire the gate on a
    low fire count cannot tell "nothing is ever close" from "everything clusters
    just under the line" — opposite conclusions, same fire count. gates.yaml
    tuning_notes sends the reader here, so the data has to actually be here.
    """
    world, meta = seeded
    _run_logged({"id": "g-new",
                 "rule": "Always pass a timeout when grepping a remote log directory."},
                "guardrails", world, meta)
    rows = _firings(meta)
    assert len(rows) == 1
    assert rows[0]["decision"] == "noop"
    assert 0.0 < rows[0]["extra"]["similarity"] < rows[0]["extra"]["threshold"]
    assert rows[0]["extra"]["nearest_id"] == "guard-0001"


def test_exactly_one_firing_per_invocation(seeded):
    """Three runs, three rows. A duplicated emit would inflate the denominator."""
    world, meta = seeded
    for i in range(3):
        _run_logged({"id": f"g-{i}", "rule": f"Unrelated rule number {i} about certificates."},
                    "guardrails", world, meta)
    assert len(_firings(meta)) == 3


def test_malformed_stdin_records_fail_open_not_silence(seeded):
    """A crash must be VISIBLE as fail_open, not indistinguishable from silence.

    fail_open and noop both produce no stderr and rc=0. If a broken helper
    logged `noop`, a permanently-crashing gate would read as a healthy quiet
    one — exactly the blind spot this goal closes, reintroduced one layer down.
    """
    world, meta = seeded
    r = _run_logged(None, "guardrails", world, meta, raw_stdin="}{ not json")
    assert r.returncode == 0
    rows = _firings(meta)
    assert len(rows) == 1
    assert rows[0]["decision"] == "fail_open"
    assert rows[0]["extra"]["reason"] == "JSONDecodeError"


def test_firing_record_carries_no_stale_seed_fields(seeded):
    """main() seeds `detail` with reason='unreached'; a reached call must not
    carry it. Caught in review — the first cut left it in every record, so the
    telemetry contradicted itself."""
    world, meta = seeded
    _run_logged({"id": "g-new", "rule": "Rotate the signing certificate before expiry."},
                "guardrails", world, meta)
    assert _firings(meta)[0]["extra"].get("reason") != "unreached"


def test_telemetry_failure_cannot_break_the_helper(seeded):
    """Point MIND_META at a path that cannot be written. The advisory must
    still warn and still exit 0 — telemetry is best-effort by construction."""
    world, _ = seeded
    unwritable = world / "guardrails.jsonl" / "not-a-dir"   # parent is a FILE
    import os
    env = dict(os.environ)
    env.update(_TELEMETRY_ENV)
    env["MIND_META"] = str(unwritable)
    r = subprocess.run(
        [sys.executable, str(HELPER), "--store", "guardrails", "--world-dir", str(world)],
        input=json.dumps({"id": "g-new",
                          "rule": "Always pass grep -a when grepping a captured test log, since "
                                  "a binary-classified log makes grep silently emit nothing at all."}),
        capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0
    assert "ADVISORY" in r.stderr


def test_gate_is_registered_in_gates_yaml():
    """An instrumented gate absent from the registry is invisible to the
    retirement evaluator — it would emit firings nobody scores."""
    import yaml
    reg = yaml.safe_load((PROJECT_ROOT / "core/config/gates.yaml").read_text(encoding="utf-8"))
    row = next((g for g in reg["gates"] if g["id"] == "store-dupe-warn"), None)
    assert row is not None, "store-dupe-warn missing from core/config/gates.yaml"
    assert row["instrumented"] is True
    assert row["script"] == "core/scripts/store_dupe_warn.py"
    # Every wrapper that invokes it must be listed as a site.
    sites = {s["file"] for s in row["sites"]}
    for w in ("guardrails-add.sh", "reasoning-bank-add.sh", "pattern-signatures-add.sh"):
        assert f"core/scripts/{w}" in sites, f"{w} invokes the gate but is not a listed site"


def test_help_does_not_manufacture_a_firing(seeded):
    """`--help` must emit NOTHING. Regression for a bug caught by fresh-eyes.

    argparse raises SystemExit for `--help` too — with code 0. The first cut
    caught SystemExit undiscriminated and logged `fail_open, reason=bad
    arguments`, so merely reading the usage text wrote a phantom gate failure.
    That is not cosmetic: gate-retirement-eval routes ANY fail_open to
    `investigate`, so a human running --help would manufacture a spurious
    investigation — the same "decision label misdescribes what happened" class
    as g-115-3093, reappearing one layer down.
    """
    world, meta = seeded
    import os
    env = dict(os.environ)
    env.update(_TELEMETRY_ENV)
    env["MIND_META"] = str(meta)
    meta.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([sys.executable, str(HELPER), "--help"],
                       capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0
    assert _firings(meta) == [], "a --help invocation wrote a firing record"


def test_genuine_argument_error_still_records_fail_open(seeded):
    """The counterpart: suppressing --help must NOT suppress real arg errors.

    Without this, the fix above could be 'implemented' by dropping the
    SystemExit emit entirely, and a caller invoking the helper with a bad
    --store would fail silently forever with no telemetry — trading a phantom
    signal for a blind spot.
    """
    world, meta = seeded
    import os
    env = dict(os.environ)
    env.update(_TELEMETRY_ENV)
    env["MIND_META"] = str(meta)
    meta.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([sys.executable, str(HELPER), "--store", "no-such-store"],
                       input="{}", capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0            # still never breaks the add
    rows = _firings(meta)
    assert len(rows) == 1, rows
    assert rows[0]["decision"] == "fail_open"
    assert "exit 2" in rows[0]["extra"]["reason"]
