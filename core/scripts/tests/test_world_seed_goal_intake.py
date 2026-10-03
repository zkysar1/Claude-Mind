"""World seed: the candidate-grooming template and the Triage Inbox (B5).

The seed defines FRESH-world day-one state, so each claim is pinned against the
thing that CONSUMES it, never against a second copy of its text: the grooming
goal against the engine whose flags, exit codes and caps its description quotes
(groom.py) and the detector its title pins (cargo-cult-detector.py); the inbox
against the resolver that finds it (groom.plan_rehome); the whole file against
the step init-world.sh runs on it (copy, then recompute-all-progress); the id
rule against init-world.sh's own seed_needed guard.

init-world.sh itself is not run here: it can start a daemon, which the init
tests avoid (test_init_backfill.py). Its seeding step is two commands, replayed
on a tmp copy below.
"""
import datetime
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import aspirations
import framework_pull
import groom
from _bash_helpers import BASH

SCRIPTS = Path(__file__).resolve().parents[1]
CORE = SCRIPTS.parent
SEED = CORE / "config" / "world-aspirations-initial.jsonl"
INIT_WORLD = SCRIPTS / "init-world.sh"
ASPIRATIONS_DOC = CORE / "config" / "conventions" / "aspirations.md"


def _records():
    """The seed's records; the byte count rides beside the record count so an
    empty parse cannot read as a pass (guard-2298)."""
    raw = SEED.read_bytes()
    recs = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    assert recs, f"seed parsed to 0 records from {len(raw)} bytes"
    return recs


def _grooming_goal():
    asp = next(r for r in _records() if r["id"] == "asp-002")
    found = [g for g in asp["goals"] if g["id"] == "g-002-03"]
    assert len(found) == 1, [g["id"] for g in asp["goals"]]
    return found[0]


def _inbox_records():
    return [r for r in _records() if r.get("triage_inbox") is True]


def _detector():
    spec = importlib.util.spec_from_file_location(
        "cargo_cult_detector_seed_pin", SCRIPTS / "cargo-cult-detector.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _engine_flags(sub):
    out = subprocess.run([sys.executable, str(SCRIPTS / "groom.py"), sub, "--help"],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return set(re.findall(r"--[a-z][a-z-]*", out.stdout)) - {"--help"}


# --- : the grooming template ----------------------------------------

def test_grooming_goal_is_a_24h_recurring_in_operating_rhythm():
    g = _grooming_goal()
    assert g["recurring"] is True and g["interval_hours"] == 24
    assert g["participants"] == ["agent"] and g["status"] == "pending"
    assert g["lastAchievedAt"] is None
    assert (g["achievedCount"], g["currentStreak"], g["longestStreak"]) == (0, 0, 0)
    # the operator-offload gate requires a stated reason on every recurring goal
    assert g["offload_decision"].startswith("stays-mind")
    assert g["verification"]["outcomes"], "a recurring goal with no outcomes verifies nothing"


def test_grooming_title_pins_the_interval_against_the_cargo_cult_detector():
    ccd = _detector()
    g = _grooming_goal()
    hits = [k for k in ccd.ARTIFACT_PRODUCING_KEYWORDS if k in g["title"].lower()]
    assert hits, f"the TITLE itself must carry a keyword (spec section 7): {g['title']!r}"
    assert ccd.is_artifact_producing(g)[0] is True
    # control: the predicate is not vacuously true
    assert ccd.is_artifact_producing({"title": "Think it over", "description": "ponder"})[0] is False


def test_description_quotes_only_flags_the_engine_has():
    desc = _grooming_goal()["description"]
    for script in ("groom-bite.sh", "groom-verdict.sh", "groom-ledger.sh"):
        assert (SCRIPTS / script).is_file(), script
        assert script in desc, script
    engine = _engine_flags("bite") | _engine_flags("verdict") | _engine_flags("ledger")
    quoted = set(re.findall(r"--[a-z][a-z-]*", desc))
    assert quoted <= engine, f"stale flag(s) in the seeded call shapes: {sorted(quoted - engine)}"
    must_show = {"--goal", "--verdict", "--evidence", "--survivor", "--rb-id", "--rehome-to",
                 "--dry-run", "--source", "--roster", "--agent", "--since", "--sample"}
    assert must_show <= quoted, f"call shape incomplete: {sorted(must_show - quoted)}"
    assert all(v in desc for v in groom.VERDICTS)


def test_description_numbers_match_the_engine():
    desc = _grooming_goal()["description"]
    d = groom.SPEC_DEFAULTS
    assert int(re.search(r"HARD CAP of (\d+) promotions", desc).group(1)) == d["promote_cap_per_firing"]
    assert int(re.search(r"the <= (\d+) candidates", desc).group(1)) == d["groom_bite"]
    assert int(re.search(r"within the last (\d+) hours", desc).group(1)) == d["touch_stale_hours"]
    quoted = dict(re.findall(
        r"(\d) (applied|stale|promote budget spent|invalid argument|partial|error)", desc))
    engine = {groom.RC_OK: "applied", groom.RC_STALE: "stale", groom.RC_CAP: "promote budget spent",
              groom.RC_INVALID: "invalid argument", groom.RC_PARTIAL: "partial", groom.RC_ERROR: "error"}
    assert quoted == {str(k): v for k, v in engine.items()}


def test_description_carries_the_age_is_never_a_verdict_clause():
    assert "AGE ORDERS THE BITE AND NEVER DECIDES A VERDICT" in _grooming_goal()["description"]


def test_new_seed_text_cites_no_world_specific_ids():
    """Guardrail, reasoning-bank and goal ids are PER-WORLD: in a seed they would
    name a different record downstream. Only the new records are held to it, and
    only their text: a record's own id is the one id a seed is meant to carry."""
    new_text = json.dumps([{k: v for k, v in rec.items() if k != "id"}
                           for rec in (_grooming_goal(), _inbox_records()[0])])
    for pat in (r"\bguard-\d+", r"\brb-\d+", r"\bg-\d+-\d+"):
        assert re.search(pat, new_text) is None, pat


# --- the Triage Inbox --------------------------------------------------------

def test_triage_inbox_is_one_empty_live_aspiration():
    inboxes = _inbox_records()
    assert len(inboxes) == 1, [r["id"] for r in inboxes]
    inbox = inboxes[0]
    assert inbox["title"] == "Triage Inbox"
    assert inbox["goals"] == [] and inbox["status"] == "active" and inbox["archived"] is False
    assert inbox["source"] == "bootstrap"
    assert inbox["id"] not in {"asp-001", "asp-002", "asp-003", groom.LEGACY_INBOX_ASP}


def test_infrastructure_cadences_is_not_seeded():
    """Dropped as a near-duplicate of Operating Rhythm (spec section 7)."""
    titles = [r["title"] for r in _records()]
    assert not any("infrastructure cadences" in t.lower() for t in titles), titles


def test_engine_resolves_the_seeded_inbox_by_its_flag():
    inbox = _inbox_records()[0]
    goal = {"id": "g-x", "status": "candidate", "recurring": False}
    legacy = {"id": groom.LEGACY_INBOX_ASP, "status": "active", "archived": False, "goals": [goal]}
    asps = [legacy] + _records()
    got = groom.plan_rehome(asps, groom.LEGACY_INBOX_ASP, goal, "close-moot", None)
    assert got == {"to": inbox["id"], "lane": "triage-inbox"}
    # control: with the flag off nothing resolves, so the flag is what does the work
    unflagged = [dict(a, triage_inbox=False) if a.get("triage_inbox") else a for a in asps]
    assert groom.plan_rehome(unflagged, groom.LEGACY_INBOX_ASP, goal, "close-moot", None) \
        == {"deferred": "no-triage-inbox"}


def test_a_fresh_world_starts_with_an_empty_bite():
    out = groom.build_bite(_records(), "agent-a", ["agent-a"], {}, dict(groom.SPEC_DEFAULTS),
                           datetime.datetime(2026, 10, 1))
    assert out["bite"] == [] and out["candidates_total"] == 0


# --- fresh init: what init-world.sh does to the seed -------------------------

def _init_world_seed_step(tmp_path):
    """init-world.sh's aspirations seeding minus the daemon: copy, then
    recompute-all-progress. STORAGE_BACKEND and MIND_WORLD keep every write in
    the tmp dir (guard-708, guard-955)."""
    target = tmp_path / "aspirations.jsonl"
    shutil.copyfile(SEED, target)
    env = dict(os.environ, STORAGE_BACKEND="local", MIND_WORLD=str(tmp_path),
               PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run([sys.executable, str(SCRIPTS / "aspirations.py"),
                        "recompute-all-progress", str(target)],
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, (r.stdout, r.stderr)
    return [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_fresh_init_replay_keeps_both_records_valid_and_progress_green(tmp_path):
    seeded = _records()
    after = _init_world_seed_step(tmp_path)
    assert [a["id"] for a in after] == [a["id"] for a in seeded]
    for before, got in zip(seeded, after):
        # asp-001's  carries a file_check the schema validator rejects. That
        # predates this change and is not its to fix, so only the records it owns are
        # validated (raises ValueError on an invalid record).
        if got["id"] in ("asp-002", "asp-004"):
            aspirations.validate_aspiration(got)
        # the step recomputes progress and nothing else: no seeded field may be dropped or altered
        assert {k: v for k, v in got.items() if k != "progress"} \
            == {k: v for k, v in before.items() if k != "progress"}, before["id"]
        # a count the seed states must be the count the step derives; adding a
        # recurring goal without bumping recurring_goals is how one goes stale
        for key, val in before["progress"].items():
            assert got["progress"][key] == val, (before["id"], key)
    inbox = [a for a in after if a.get("triage_inbox") is True]
    assert len(inbox) == 1 and inbox[0]["status"] == "active" and inbox[0]["goals"] == []
    assert [g["id"] for a in after if a["id"] == "asp-002" for g in a["goals"]].count("g-002-03") == 1


# --- the id-collision rule ---------------------------------------------------

def _seed_needed(target):
    src = INIT_WORLD.read_text(encoding="utf-8")
    fn = re.search(r"^seed_needed\(\) \{.*?^\}", src, re.S | re.M)
    assert fn, "seed_needed() not found in init-world.sh"
    r = subprocess.run([BASH, "-c", f'BACKFILL=0; STORAGE_BACKEND=local\n{fn.group(0)}\nseed_needed "$1"; echo "rc=$?"',
                        "_", str(target)], capture_output=True, text=True, timeout=30)
    return r.stdout


def test_seed_never_lands_in_a_world_that_already_has_its_store(tmp_path):
    """Seed ids are not unique across world generations, so a seed record must
    never be inserted into a world that already holds an aspirations store."""
    existing = tmp_path / "aspirations.jsonl"
    existing.write_text('{"id": "asp-004", "title": "A different aspiration"}\n', encoding="utf-8")
    assert "rc=1" in _seed_needed(existing)
    assert "rc=0" in _seed_needed(tmp_path / "absent.jsonl")


def test_an_adopting_world_is_shown_the_inbox_as_a_new_record():
    """An initialised world never re-runs the seed. The pull's seed-delta lane is
    how it learns of the inbox, and it files it with create-aspiration at the
    next free id."""
    text = SEED.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    old = "\n".join(ln for ln in lines if json.loads(ln).get("triage_inbox") is not True)
    delta = framework_pull.seed_delta(old, text)
    assert [r["id"] for r in delta] == [_inbox_records()[0]["id"]]


def test_id_collision_rule_and_new_records_are_documented():
    doc = ASPIRATIONS_DOC.read_text(encoding="utf-8")
    assert "ID-COLLISION RULE" in doc
    rule = doc.split("ID-COLLISION RULE", 1)[1][:1800]
    for needle in ("create-aspiration", "next free", "NEVER", "triage_inbox"):
        assert needle in rule, needle
    assert "g-002-03" in doc and "Triage Inbox" in doc
