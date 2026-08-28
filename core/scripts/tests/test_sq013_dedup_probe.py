"""Regression test for : the sq-013 replay dedup must see COMPLETED
owners, not only open ones.

THE MEASURED INCIDENT this pins (2026-08-27, first-hand, twice in one session):
g-326-711 completed 01:37:19 and g-326-712 completed 02:01:19; at 02:12:21 the
reducer spark replay filed g-326-714 as their duplicate, and a worker skipped it
as MOOT ON ARRIVAL at 02:20:54. The dedup run before filing was CORRECT and its
answer was TRUE — zero LIVE owners — because both owners had already completed.
The race was ELEVEN MINUTES, so the fix is not "a longer window", it is "a
window that reaches terminal statuses at all".

guard-4166 governs the shape of this file: a fix whose effect is that something
STOPS APPEARING needs a positive control that does NOT flip, and the mutation
proof must show that control holding. Every DECLINE assertion below is therefore
paired with a FILE assertion, so an over-broad implementation that declined
everything would fail here rather than passing silently — which is the failure
mode a decline-only test cannot distinguish from a working fix.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "sq013_dedup_probe", CORE_SCRIPTS / "sq013-dedup-probe.py")
sq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sq)

NOW = datetime(2026, 8, 27, 2, 12, 21)          # the moment  was filed
SESSION_START = datetime(2026, 8, 27, 1, 0, 0)

# The relay subject, in the worker's own phrasing — deliberately NOT the
# completed goal's wording, so this also exercises the phrasing axis the block
# already covered (guard-1204 / guard-2228).
RELAY = ("pickNearbyPlayer returns null without instrumentation so the "
         "denominator for nearby-player selection is unmeasured")

COMPLETED_OWNER = {
    "id": "g-326-711",
    "status": "completed",
    "completed_date": "2026-08-27T01:37:19",
    "title": "Instrument the pickNearbyPlayer denominator",
    "description": ("The previously-prescribed null-branch log would "
                    "instrument dead code: the composer's player==null branch "
                    "is unreachable because the composer is entered only when "
                    "the scorer already found a nearby player."),
}

# Positive control: same corpus, a subject nothing owns. Must stay FILE in every
# assertion below, including under mutation.
UNOWNED = ("warm pool scaling emits no telemetry when the breach path "
           "reports success")


def _corpus(*extra):
    return [COMPLETED_OWNER, *extra]


# --- the fix: a terminal owner inside the window is SEEN --------------------

def test_completed_minutes_earlier_declines_while_control_still_files():
    """THE REGRESSION. Both assertions run against the SAME corpus and the SAME
    call parameters, so a blanket-decline implementation fails the second."""
    declined = sq.decide(RELAY, _corpus(), NOW, SESSION_START)
    assert declined["decision"] == "DECLINE", declined
    filed = sq.decide(UNOWNED, _corpus(), NOW, SESSION_START)
    assert filed["decision"] == "FILE", filed          # control must NOT flip


def test_decline_cites_the_completed_goal_id():
    """Outcome 3: a reader must be able to tell 'already done' from 'never
    filed'. A bare DECLINE cannot carry that distinction."""
    r = sq.decide(RELAY, _corpus(), NOW, SESSION_START)
    assert r["cited_goal_id"] == "g-326-711", r
    assert r["cited_status"] == "completed", r
    assert "g-326-711" in r["reason"], r


def test_skipped_status_is_scanned_too():
    """`skipped` is terminal and means the work was considered. Same-call
    control."""
    owner = dict(COMPLETED_OWNER, id="g-326-712", status="skipped")
    assert sq.decide(RELAY, [owner], NOW, SESSION_START)["decision"] == "DECLINE"
    assert sq.decide(UNOWNED, [owner], NOW, SESSION_START)["decision"] == "FILE"


# --- the window is REAL, not "always decline" ------------------------------

def test_terminal_owner_outside_the_window_does_not_decline():
    """A goal closed months ago is not evidence the work was JUST done. Without
    this the probe would decline on any historical overlap and be useless."""
    stale = dict(COMPLETED_OWNER, completed_date="2026-04-01T00:00:00")
    assert sq.decide(RELAY, [stale], NOW, SESSION_START)["decision"] == "FILE"


def test_open_owner_declines_regardless_of_age():
    """Pre-existing behaviour preserved: an OPEN goal owns its work however old
    it is, so the window must not be applied to it."""
    old_open = dict(COMPLETED_OWNER, id="g-326-700", status="pending",
                    completed_date=None, created="2026-01-05T00:00:00")
    assert sq.decide(RELAY, [old_open], NOW, SESSION_START)["decision"] == "DECLINE"


def test_undated_terminal_goal_is_counted_in_not_out():
    """An undated terminal record is AMBIGUOUS, not old. Counting it out would
    reproduce the original defect on exactly the records whose timestamps a
    writer forgot to stamp."""
    undated = {k: v for k, v in COMPLETED_OWNER.items()
               if k != "completed_date"}
    assert sq.decide(RELAY, [undated], NOW, SESSION_START)["decision"] == "DECLINE"


# --- window arithmetic ------------------------------------------------------

def test_window_takes_the_earlier_of_session_start_and_fixed_lookback():
    """Both are FLOORS. A long session must not lose its own early completions,
    and a short session must not become blinder than the plain lookback — a
    single anchor cannot satisfy both, which is why this is a min()."""
    long_session = NOW - timedelta(hours=200)
    assert sq.window_start(NOW, long_session, 72) == long_session
    short_session = NOW - timedelta(hours=2)
    assert sq.window_start(NOW, short_session, 72) == NOW - timedelta(hours=72)
    assert sq.window_start(NOW, None, 72) == NOW - timedelta(hours=72)


# --- an unusable corpus is never a FILE ------------------------------------

def test_empty_and_garbage_corpora_refuse_rather_than_file(monkeypatch, capsys):
    """verify-before-assuming rule 4 / guard-2298: a silently-failed read that
    returns nothing has told you nothing. Reporting FILE on it would convert a
    broken probe into confident permission to duplicate."""
    for payload in ("", "   ", "not json at all", "[]"):
        monkeypatch.setattr(sys, "stdin", _FakeStdin(payload))
        rc = sq.main(["--subject", RELAY])
        assert rc == 2, (payload, rc)
        capsys.readouterr()


def test_exit_codes_separate_decline_from_breakage(monkeypatch, capsys):
    """3 = an owner exists, 0 = file, 2 = the probe could not run. Collapsing
    DECLINE and breakage onto one non-zero code makes each readable as the
    other (deploy-hold-check.sh precedent)."""
    import json as _json
    monkeypatch.setattr(sys, "stdin", _FakeStdin(_json.dumps(_corpus())))
    assert sq.main(["--subject", RELAY, "--now", NOW.isoformat(),
                    "--session-start", SESSION_START.isoformat()]) == 3
    capsys.readouterr()
    monkeypatch.setattr(sys, "stdin", _FakeStdin(_json.dumps(_corpus())))
    assert sq.main(["--subject", UNOWNED, "--now", NOW.isoformat(),
                    "--session-start", SESSION_START.isoformat()]) == 0
    capsys.readouterr()


class _FakeStdin:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload


# --- the rarity gate, exercised at a corpus size where IDF is LIVE ----------
#
# Everything above runs on a 1-2 goal corpus, which is BELOW MIN_IDF_CORPUS, so
# IDF is inert and the rarity gate cannot fire there. That is precisely the
# layer a fixture cannot reach by accident (guard-1462), and it is where the
# live dogfood found a real false positive: against the 3,140-goal queue the
# probe first cited an unrelated goal on five generic tokens. These two tests
# build a corpus big enough for document frequencies to exist, so the gate is
# under test rather than merely documented.

def _big_corpus(with_owner=True):
    """A corpus that REPRODUCES THE LIVE DOCUMENT-FREQUENCY DISTRIBUTION.

    This shape is load-bearing and the first version of it was wrong, which the
    mutation proof caught: with generic tokens in ~59 of 60 records their idf is
    0, so the WEIGHT threshold rejected the decoy and the rarity gate never
    fired — the test passed, for the wrong reason, and removing the rarity gate
    did not flip it. A test green by default is worse than no test (guard-2903).

    Live measurement being reproduced: n=3,140 with `returns` at df=799 (25%),
    idf 1.37, and five such tokens summing to weight 11.04 — comfortably above
    any sane weight floor. So here ~25% of a 201-record corpus carries the
    generic vocabulary, giving idf log(201/52) ~ 1.35 per token and a decoy
    weight ~6.7. Weight alone therefore CANNOT reject the decoy, and the rarity
    gate is the only thing that can — which is exactly the condition under test.
    """
    unrelated = [{
        "id": "g-900-%03d" % i,
        "status": "completed",
        "completed_date": "2026-08-27T01:00:00",
        "title": "Unrelated maintenance record %d about caching layers" % i,
        "description": ("Housekeeping concerning cache eviction, retention "
                        "windows and archival snapshots. Record %d." % i),
    } for i in range(150)]

    generic = [{
        "id": "g-000-%03d" % i,
        "status": "completed",
        "completed_date": "2026-08-27T01:00:00",
        "title": "Generic goal %d returns selection without denominator" % i,
        "description": ("This record returns a selection without a denominator "
                        "and is unmeasured, like its siblings."),
    } for i in range(49)]

    decoy = {
        "id": "g-115-3421",
        "status": "completed",
        "completed_date": "2026-08-27T01:30:00",
        "title": "pipeline-archive has no scheduled caller",
        "description": ("It returns a selection without a denominator and the "
                        "prune cadence is unmeasured."),
    }
    owner = {
        "id": "g-326-711",
        "status": "completed",
        "completed_date": "2026-08-27T01:37:19",
        "title": "Instrument the pickNearbyPlayer denominator",
        "description": ("pickNearbyPlayer instrumentation: the null branch is "
                        "unreachable, so the denominator is unmeasured."),
    }
    corpus = unrelated + generic + [decoy]
    return corpus + [owner] if with_owner else corpus


def test_the_fixture_actually_reproduces_the_live_idf_shape():
    """Guards the guard. If the corpus drifts back to df~n the generic tokens
    weigh 0, weight alone rejects the decoy, and the two tests below stop
    testing the rarity gate while still passing."""
    corpus = _big_corpus()
    docs = [sq._tokens(" ".join(str(g.get(f) or "")
                                for f in ("title", "description")))
            for g in corpus]
    idf, n = sq._compute_idf(docs, sq._tokens(RELAY))
    assert n >= sq.MIN_IDF_CORPUS, n            # IDF must be LIVE, not inert
    decoy_tokens = sq._tokens(RELAY) & docs[-2]
    decoy_weight = sum(idf[t][1] for t in decoy_tokens)
    assert decoy_weight > sq.WEIGHT_THRESHOLD, (
        "decoy weight %.2f must EXCEED the weight floor, or the rarity gate is "
        "not what rejects it" % decoy_weight)
    assert not [t for t in decoy_tokens if idf[t][0] <= max(2, n // sq.RARE_DF_DIVISOR)], \
        "decoy must carry NO rare token"


def test_generic_token_overlap_alone_does_not_decline():
    """THE LIVE-FOUND FALSE POSITIVE, pinned. The decoy shares four generic
    tokens with the subject and no rare one; it must not be cited."""
    r = sq.decide(RELAY, _big_corpus(with_owner=False), NOW, SESSION_START)
    assert r["decision"] == "FILE", r
    assert r["cited_goal_id"] != "g-115-3421", r


def test_rare_token_owner_is_cited_over_the_generic_decoy():
    """Same corpus WITH the true owner present: it wins, and the decoy is not
    the citation. Ranked by IDF weight, not raw overlap count — by count the
    decoy would have tied or beaten it, which is how the live miss happened."""
    r = sq.decide(RELAY, _big_corpus(), NOW, SESSION_START)
    assert r["decision"] == "DECLINE", r
    assert r["cited_goal_id"] == "g-326-711", r
    assert "picknearbyplayer" in r["matches"][0]["rare_tokens"], r["matches"][0]
