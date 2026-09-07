"""Query-string integer params refuse unparseable input with a 400 ().

THE DEFECT. Five reader endpoints coerced a query-string integer with no shared
guard, and they failed in two different wrong ways:

    experience.py  most_retrieved / least_retrieved / recent  -> silent default 10
    journal.py     recent                                     -> silent default 5
    aspirations.py limit (stepping_stones)                    -> uncaught ValueError

The first four swallowed `?recent=abc` and returned a full default-sized page, so
a caller with a typo or a bad template variable got a plausible-looking answer and
no signal at all. The fifth did the opposite and raised an uncaught ValueError out
of `int(q.get("limit", "5"))`, which the server turns into an HTTP 500 — a server
fault for what is squarely a client error.

The helper `_parse_n` already existed TWICE, byte-identically apart from its
default (experience.py, journal.py). That duplication is the reason a fix to one
would never have reached the other, and it is why the resolved hypothesis
(2026-07-29_silent-parse-skip-is-narrow-not-house-style) prescribed one shared
parse-or-400 helper rather than five one-off patches.

WHAT THESE TESTS PIN, and why in this shape:

1. UNPARSEABLE -> 400 at all five sites. One test per site, not one parametrized
   test over a list, because the five sites are in three different modules with
   three different default values and the whole defect was that a fix to one did
   not reach the others. Five explicit tests fail five explicitly-named ways.

2. ABSENT/EMPTY -> the DOCUMENTED default, and the exact NUMBER. This is the half
   a naive regression test misses: asserting "no 400 when absent" would pass just
   as well against a helper that had normalised every site to the same default.
   So each test seeds MORE records than the default and asserts the returned
   count equals that site's own documented default (10/10/10/5/5). A future
   refactor that unifies the defaults fails here, loudly, with the site named.

3. The `positive_only` ASYMMETRY. Four sites clamp a parsed `n <= 0` back to the
   default (what both `_parse_n` copies did); aspirations does NOT, because its
   stepping_stones branch slices with the raw int, so `?limit=-3` means
   `archived[:-3]`. That is surprising and pre-existing, and normalising it would
   be a second, unmeasured behaviour change riding a refactor. It is pinned here
   precisely because it is the kind of difference a tidy-up deletes without
   noticing.

Absent and unparseable are DIFFERENT cases and only the second is an error —
collapsing them back together is the defect, so both halves are asserted for
every site.

Fixture note: `project_root` is function-scoped over `tmp_path`, so each test
seeds its own store and the daemon (which reads per-request through the
mtime-keyed cache) picks the writes up without a restart.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


def _get(port: int, path: str, query: dict = None, *, agent: str = "alpha"):
    url = f"http://127.0.0.1:{port}{path}"
    if query is not None:
        url += "?" + urllib.parse.urlencode(query)
    req = urllib.request.Request(url)
    req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, resp.read().decode("utf-8")


def _expect_400(port: int, path: str, query: dict, param: str):
    """Assert the request is refused with the parse-error body, and return it.

    Checks the param NAME appears in the detail: a 400 that does not say WHICH
    parameter was bad leaves the caller guessing, and with three int params on
    one endpoint that guess is a coin flip.
    """
    try:
        _get(port, path, query)
    except urllib.error.HTTPError as e:
        assert e.code == 400, f"expected 400 for {path}?{query}, got {e.code}"
        err = json.loads(e.read().decode("utf-8"))
        assert err["error"] == "invalid_param", err
        assert param in err["detail"], err
        return err
    raise AssertionError(f"expected 400 for unparseable {param} at {path}")


# ---------------------------------------------------------------------------
# Seeding helpers — each writes MORE records than the site's default, so the
# default is observable as a count rather than merely as "not an error".
# ---------------------------------------------------------------------------

def _seed_journal(project_root, n: int = 12):
    lines = [
        json.dumps({
            "session": i,
            "date": f"2026-05-{i:02d}",
            "goals_completed": [],
            "key_events": [],
            "tags": [],
        })
        for i in range(1, n + 1)
    ]
    (project_root / "agents" / "alpha" / "journal.jsonl").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")


def _seed_experience(project_root, n: int = 25):
    lines = [
        json.dumps({
            "id": f"exp-seed-{i:02d}",
            "type": "insight",
            "category": "seed-cat",
            "summary": f"seed {i}",
            "goal_id": None,
            "created": f"2026-05-{i:02d}T08:00:00",
            "retrieval_stats": {"retrieval_count": i},
        })
        for i in range(1, n + 1)
    ]
    (project_root / "agents" / "alpha" / "experience.jsonl").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")


def _seed_archived_aspirations(project_root, n: int = 12):
    lines = [
        json.dumps({
            "id": f"asp-{i:03d}",
            "title": f"archived {i}",
            "motivation": "",
            "scope": "test",
            "tags": [],
            "goals": [],
            "completed_at": f"2026-05-{i:02d}T00:00:00",
        })
        for i in range(1, n + 1)
    ]
    (project_root / "world" / "aspirations-archive.jsonl").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Site 1-3: experience.py — most_retrieved / least_retrieved / recent (default 10)
# ---------------------------------------------------------------------------

def test_experience_most_retrieved_unparseable_400(running_daemon):
    _, port = running_daemon
    _expect_400(port, "/v1/experience/read",
                {"most_retrieved": "abc"}, "most_retrieved")


def test_experience_least_retrieved_unparseable_400(running_daemon):
    _, port = running_daemon
    _expect_400(port, "/v1/experience/read",
                {"least_retrieved": "abc"}, "least_retrieved")


def test_experience_recent_unparseable_400(running_daemon):
    _, port = running_daemon
    _expect_400(port, "/v1/experience/read", {"recent": "1.5"}, "recent")


def test_experience_recent_empty_uses_default_10(running_daemon, project_root):
    _seed_experience(project_root, n=25)
    _, port = running_daemon
    status, body = _get(port, "/v1/experience/read", {"recent": ""})
    assert status == 200
    assert len(json.loads(body)) == 10


def test_experience_most_retrieved_empty_uses_default_10(running_daemon, project_root):
    _seed_experience(project_root, n=25)
    _, port = running_daemon
    status, body = _get(port, "/v1/experience/read", {"most_retrieved": ""})
    assert status == 200
    assert len(json.loads(body)) == 10


def test_experience_least_retrieved_empty_uses_default_10(running_daemon, project_root):
    _seed_experience(project_root, n=25)
    _, port = running_daemon
    status, body = _get(port, "/v1/experience/read", {"least_retrieved": ""})
    assert status == 200
    assert len(json.loads(body)) == 10


def test_experience_recent_valid_value_still_honoured(running_daemon, project_root):
    """Control: the 400 path must not have broken the parse it guards."""
    _seed_experience(project_root, n=25)
    _, port = running_daemon
    _, body = _get(port, "/v1/experience/read", {"recent": "3"})
    assert len(json.loads(body)) == 3


# ---------------------------------------------------------------------------
# Site 4: journal.py — recent (default 5)
# ---------------------------------------------------------------------------

def test_journal_recent_unparseable_400(running_daemon):
    _, port = running_daemon
    _expect_400(port, "/v1/journal/read", {"recent": "abc"}, "recent")


def test_journal_recent_empty_uses_default_5(running_daemon, project_root):
    """The default here is 5, NOT experience's 10 — the per-site difference the
    two duplicated `_parse_n` copies encoded, and the one a unified helper is
    most likely to erase."""
    _seed_journal(project_root, n=12)
    _, port = running_daemon
    status, body = _get(port, "/v1/journal/read", {"recent": ""})
    assert status == 200
    assert len(json.loads(body)) == 5


def test_journal_recent_valid_value_still_honoured(running_daemon, project_root):
    _seed_journal(project_root, n=12)
    _, port = running_daemon
    _, body = _get(port, "/v1/journal/read", {"recent": "2"})
    assert len(json.loads(body)) == 2


# ---------------------------------------------------------------------------
# Site 5: aspirations.py — limit on stepping_stones (default 5)
#
# This is the site that raised an uncaught ValueError, so its "unparseable"
# test is a 500->400 correction rather than a silence->400 one.
# ---------------------------------------------------------------------------

def test_aspirations_limit_unparseable_is_400_not_500(running_daemon):
    _, port = running_daemon
    _expect_400(port, "/v1/aspirations/read",
                {"stepping_stones": "1", "limit": "abc"}, "limit")


def test_aspirations_limit_empty_is_400_free(running_daemon, project_root):
    """`?limit=` (present but blank) reached `int("")` and was an HTTP 500.

    The server parses with keep_blank_values=True, so this arrives as "" rather
    than as an absent key — which is exactly why it hit the coercion at all.
    """
    _seed_archived_aspirations(project_root, n=12)
    _, port = running_daemon
    status, body = _get(port, "/v1/aspirations/read",
                        {"stepping_stones": "1", "limit": ""})
    assert status == 200
    assert len(json.loads(body)) == 5


def test_aspirations_limit_absent_uses_default_5(running_daemon, project_root):
    _seed_archived_aspirations(project_root, n=12)
    _, port = running_daemon
    status, body = _get(port, "/v1/aspirations/read", {"stepping_stones": "1"})
    assert status == 200
    assert len(json.loads(body)) == 5


# ---------------------------------------------------------------------------
# The helper's own contract, including the asymmetry the five sites do not share
# ---------------------------------------------------------------------------

def test_parse_int_param_absent_and_empty_yield_default():
    from mind_api.src.endpoints._jsonl_common import parse_int_param
    assert parse_int_param(None, "recent", 7) == (7, None)
    assert parse_int_param("", "recent", 7) == (7, None)


def test_parse_int_param_positive_only_clamps_non_positive():
    """The four clamping sites: a parsed value <= 0 falls back to the default."""
    from mind_api.src.endpoints._jsonl_common import parse_int_param
    assert parse_int_param("0", "recent", 7) == (7, None)
    assert parse_int_param("-3", "recent", 7) == (7, None)


def test_parse_int_param_positive_only_false_returns_raw_int():
    """The aspirations site: `?limit=-3` must keep meaning `archived[:-3]`.

    Pinned because it is a deliberate divergence, not an oversight — normalising
    it would silently change what a negative limit does to the slice.
    """
    from mind_api.src.endpoints._jsonl_common import parse_int_param
    assert parse_int_param("0", "limit", 5, positive_only=False) == (0, None)
    assert parse_int_param("-3", "limit", 5, positive_only=False) == (-3, None)


def test_parse_int_param_error_names_the_param_and_the_value():
    from mind_api.src.endpoints._jsonl_common import parse_int_param
    n, err = parse_int_param("abc", "most_retrieved", 10)
    assert n is None
    assert err.status == 400
    payload = json.loads(err.body)
    assert payload["error"] == "invalid_param"
    assert "most_retrieved" in payload["detail"]
    assert "abc" in payload["detail"]
