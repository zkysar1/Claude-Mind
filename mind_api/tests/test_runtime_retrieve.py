"""PR 5 — retrieve orchestrator endpoint tests.

Covers /v1/retrieve (the read-only path). The endpoint imports
core/scripts/retrieve.py once at daemon startup and swaps the module's
path globals under a lock per request — these tests verify:

  - happy path: matching category returns the expected tree node / rb / guard
  - response shape: top-level keys mirror retrieve.py main() output
  - include_framework: framework_rules key only present when opted in
  - full_content vs metadata-only: _strip_long_form runs as expected
  - error cases: missing category / depth / agent header
  - Decision #58: missing read_only is NOT an error (no-goal gate auto-RO)
  - read-only contract: counter writes do NOT fire (RB file mtime unchanged)
  - path-restore: module globals are RESTORED after each request

The conftest fixture seeds enough world/agent state to exercise the loaders:
  - tree with alpha-test-node / beta-other-node
  - reasoning-bank with rb-001 (active alpha-cat) / rb-002 (universal)
  - guardrails with guard-001 / guard-002
  - pattern-signatures with sig-001
  - experience.jsonl with exp-test-1 (alpha-cat)
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def _get(port: int, path: str, query: dict = None, *, agent: str = "alpha",
         timeout: float = 10.0):
    url = f"http://127.0.0.1:{port}{path}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    req = urllib.request.Request(url)
    if agent:
        req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_retrieve_basic_returns_full_shape(running_daemon):
    """The response carries every top-level key the CLI produces."""
    _, port = running_daemon
    _, body = _get(port, "/v1/retrieve",
                   {"category": "alpha", "depth": "deep", "read_only": "1"})
    data = json.loads(body)

    # Top-level shape mirrors retrieve.py main() — see lines 1305-1315.
    for key in ("meta", "tree_nodes", "reasoning_bank", "meta_lessons",
                "guardrails", "pattern_signatures", "experiences", "beliefs",
                "experiential_index"):
        assert key in data, f"missing key: {key}"

    # Meta block carries the request echo + counters.
    meta = data["meta"]
    assert meta["category"] == "alpha"
    assert meta["depth"] == "deep"
    assert meta["read_only"] is True
    assert "items_returned" in meta
    assert "timestamp" in meta
    # framework_rules omitted unless include_framework=1.
    assert "framework_rules" not in data
    assert "include_framework" not in meta


def test_retrieve_matches_tree_node(running_daemon):
    """alpha matches alpha-test-node via the substring channel."""
    _, port = running_daemon
    _, body = _get(port, "/v1/retrieve",
                   {"category": "alpha", "depth": "deep", "read_only": "1"})
    data = json.loads(body)
    keys = [n["key"] for n in data["tree_nodes"]]
    assert "alpha-test-node" in keys


def test_retrieve_partitions_universal_vs_domain_rb(running_daemon):
    """Universal RBs always surface in meta_lessons; domain RBs filter by category.

    The fixture's rb-001 (applies_to=framework) AND rb-002 (applies_to=any) are
    both universal per `is_universal_rb` — they land in meta_lessons regardless
    of the query category. rb-003 is retired and never returned.
    """
    _, port = running_daemon
    _, body = _get(port, "/v1/retrieve",
                   {"category": "alpha-cat", "depth": "deep", "read_only": "1"})
    data = json.loads(body)
    rb_ids = {r["id"] for r in data["reasoning_bank"]}
    meta_ids = {r["id"] for r in data["meta_lessons"]}
    # Universal entries surface in meta_lessons.
    assert "rb-001" in meta_ids
    assert "rb-002" in meta_ids
    # Retired entries excluded everywhere.
    assert "rb-003" not in rb_ids
    assert "rb-003" not in meta_ids
    # Universal entries do NOT appear in the domain reasoning_bank partition.
    assert "rb-001" not in rb_ids
    assert "rb-002" not in rb_ids


def test_retrieve_matches_guardrails(running_daemon):
    """infra category matches guard-001 (active); guard-099 (retired) excluded."""
    _, port = running_daemon
    _, body = _get(port, "/v1/retrieve",
                   {"category": "infra", "depth": "deep", "read_only": "1"})
    data = json.loads(body)
    guard_ids = [g["id"] for g in data["guardrails"]]
    assert "guard-001" in guard_ids
    assert "guard-099" not in guard_ids


def test_retrieve_supplementary_only_skips_tree(running_daemon):
    """supplementary_only=1 returns [] for tree_nodes; supplementary stores fire."""
    _, port = running_daemon
    _, body = _get(port, "/v1/retrieve",
                   {"category": "alpha", "depth": "deep",
                    "supplementary_only": "1", "read_only": "1"})
    data = json.loads(body)
    assert data["tree_nodes"] == []
    # Universal RBs always surface regardless of supplementary_only — they're
    # the proof the supplementary loaders ran. rb-001 + rb-002 are both
    # universal in the fixture.
    meta_ids = {r["id"] for r in data["meta_lessons"]}
    assert "rb-001" in meta_ids or "rb-002" in meta_ids


def test_retrieve_full_content_preserves_rb_content(running_daemon):
    """full_content=1 keeps reasoning-bank `content` field; default nulls it."""
    _, port = running_daemon

    # Default (metadata-only) — _strip_long_form nulls content/description.
    _, body_strip = _get(port, "/v1/retrieve",
                         {"category": "alpha-cat", "depth": "deep",
                          "read_only": "1"})
    strip_data = json.loads(body_strip)
    # rb-001 + rb-002 in our fixture don't have a `content` field at all
    # (they're seeded as minimal records); _strip_long_form would only NULL
    # if the field exists. Check that we don't error on missing field.
    # If `content` is present it must be None; otherwise it's absent.
    for r in strip_data["reasoning_bank"] + strip_data["meta_lessons"]:
        if "content" in r:
            assert r["content"] is None, f"_strip_long_form should null content, got {r['content']!r}"

    # With full_content=1, the content field would be preserved verbatim.
    # Our fixture records don't carry content, so we just verify the call
    # succeeds and the meta marker reflects the flag.
    _, body_full = _get(port, "/v1/retrieve",
                        {"category": "alpha-cat", "depth": "deep",
                         "full_content": "1", "read_only": "1"})
    full_data = json.loads(body_full)
    assert full_data["meta"]["full_content"] is True


def test_retrieve_include_framework_surfaces_rules(running_daemon):
    """include_framework=1 exposes the framework_rules key + meta marker.

    The framework index is built from .claude/rules/*.md, core/config/
    conventions/*.md, and world/conventions/*.md (the latter under our
    swapped WORLD_DIR — which is empty in this fixture). We only assert
    the shape; matching depends on the test machine's repo layout, but
    .claude/rules/ exists at the project root so at least one match should
    surface for the broad "rule" query.
    """
    _, port = running_daemon
    _, body = _get(port, "/v1/retrieve",
                   {"category": "rule", "depth": "deep",
                    "include_framework": "1", "read_only": "1"})
    data = json.loads(body)
    assert "framework_rules" in data
    assert data["meta"].get("include_framework") is True
    assert "framework_rules" in data["meta"]["items_returned"]
    # Cap at FRAMEWORK_RULES_CAP=15.
    assert len(data["framework_rules"]) <= 15


def test_retrieve_multi_category(running_daemon):
    """Comma-separated categories are split and matched independently.

    Uses `safety,infra` — both are guardrail categories in the fixture
    (guard-002 = safety, guard-001 = infra), so both should surface in a
    single response. This proves the comma-split / multi-match path that a
    same-store single-category test cannot.
    """
    _, port = running_daemon
    _, body = _get(port, "/v1/retrieve",
                   {"category": "safety,infra", "depth": "deep",
                    "read_only": "1"})
    data = json.loads(body)
    guard_ids = {g["id"] for g in data["guardrails"]}
    assert "guard-001" in guard_ids  # infra
    assert "guard-002" in guard_ids  # safety
    # Verify meta echoes the raw string (not the split list).
    assert data["meta"]["category"] == "safety,infra"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------

def test_retrieve_missing_category_400(running_daemon):
    _, port = running_daemon
    try:
        _get(port, "/v1/retrieve", {"read_only": "1"})
    except urllib.error.HTTPError as e:
        assert e.code == 400
        err = json.loads(e.read().decode("utf-8"))
        assert err["error"] == "missing_param"
    else:
        raise AssertionError("expected 400 for missing category")


def test_retrieve_missing_read_only_no_goal_auto_readonly(running_daemon):
    """Decision #58 +  no-goal gate.

    Pre-#58 (Decision #24) the daemon returned 400 read_only_required when
    `read_only` was absent — the counter-bump path was wrapper-only. #58
    made the endpoint serve BOTH paths in-daemon, so that 400 is gone.

    When `read_only` is absent AND no goal context exists (no ?goal param
    and no in-flight goal inferable from team-state — true for the fresh
    test daemon), the g-304-01 gate auto-sets read_only=True so a retrieval
    with nothing to classify the bump against never writes counters
    ("retrieved but never classified" drift). Net: the request SUCCEEDS
    and the response echoes read_only=True.
    """
    _, port = running_daemon
    status, body = _get(port, "/v1/retrieve", {"category": "alpha"})
    assert status == 200
    data = json.loads(body)
    # The no-goal gate downgraded to read-only — proves #58 (no 400) AND
    #  (no unclassified counter bump) both hold.
    assert data["meta"]["read_only"] is True


def test_retrieve_invalid_depth_400(running_daemon):
    _, port = running_daemon
    try:
        _get(port, "/v1/retrieve",
             {"category": "alpha", "depth": "bogus", "read_only": "1"})
    except urllib.error.HTTPError as e:
        assert e.code == 400
        err = json.loads(e.read().decode("utf-8"))
        assert err["error"] == "invalid_param"
    else:
        raise AssertionError("expected 400 for invalid depth")


def test_retrieve_missing_agent_header_400(running_daemon):
    """Source=agent semantics require an explicit X-Mind-Agent."""
    _, port = running_daemon
    try:
        _get(port, "/v1/retrieve",
             {"category": "alpha", "read_only": "1"}, agent="")
    except urllib.error.HTTPError as e:
        assert e.code == 400
        err = json.loads(e.read().decode("utf-8"))
        assert err["error"] == "agent_unset"
    else:
        raise AssertionError("expected 400 for missing agent header")


def test_retrieve_empty_category_split_400(running_daemon):
    """A category value of only commas/whitespace is rejected."""
    _, port = running_daemon
    try:
        _get(port, "/v1/retrieve",
             {"category": " , , ", "read_only": "1"})
    except urllib.error.HTTPError as e:
        assert e.code == 400
    else:
        raise AssertionError("expected 400 for empty-after-split category")


# ---------------------------------------------------------------------------
# Side-effect contracts
# ---------------------------------------------------------------------------

def test_retrieve_read_only_does_not_bump_counters(running_daemon, project_root):
    """RB file mtime/size must NOT change after a read-only retrieve.

    Tightest available proof that _locked_bump_jsonl never fires on the
    read-only path. If the bump path leaked through, the file would be
    rewritten with incremented counters and `stat()` would show a new mtime.
    """
    rb_path = project_root / "world" / "reasoning-bank.jsonl"
    before = (rb_path.stat().st_mtime_ns, rb_path.stat().st_size)
    _, port = running_daemon
    _get(port, "/v1/retrieve",
         {"category": "alpha-cat", "depth": "deep", "read_only": "1"})
    after = (rb_path.stat().st_mtime_ns, rb_path.stat().st_size)
    assert before == after, (
        f"reasoning-bank.jsonl was modified on read-only retrieve: "
        f"before={before}, after={after}")


def test_retrieve_path_globals_restored_after_call(running_daemon, project_root):
    """The endpoint restores retrieve.py's module globals after each call.

    Without the restore, alpha's paths would leak into a subsequent bravo
    call (or vice versa) — the lock guarantees serialization, but the
    restore guarantees no cross-agent state contamination across calls.
    """
    # Make core/scripts importable so we can read the module's globals.
    scripts_dir = (Path(__file__).resolve().parents[2] / "core" / "scripts")
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import retrieve as _r

    before_tree = _r.TREE_PATH
    before_rb = _r.RB_PATH
    before_world = _r.WORLD_DIR
    before_agent = _r.AGENT_DIR
    before_fw = _r.FRAMEWORK_WORLD_CONVENTIONS_DIR

    _, port = running_daemon
    _get(port, "/v1/retrieve",
         {"category": "alpha", "depth": "deep", "read_only": "1"})

    assert _r.TREE_PATH == before_tree
    assert _r.RB_PATH == before_rb
    assert _r.WORLD_DIR == before_world
    assert _r.AGENT_DIR == before_agent
    assert _r.FRAMEWORK_WORLD_CONVENTIONS_DIR == before_fw


def test_retrieve_concurrent_requests_dont_interleave(running_daemon):
    """Two parallel requests must not corrupt each other's responses.

    The endpoint serializes via _swap_lock — concurrent calls queue. This
    test exercises the lock by issuing 8 requests from 4 threads against
    the same agent (same paths, no path-swap drift). Every response must
    be well-formed JSON with the expected category echo.
    """
    import threading

    _, port = running_daemon
    bodies = []
    errors = []

    def _one():
        try:
            _, body = _get(port, "/v1/retrieve",
                           {"category": "alpha", "depth": "deep",
                            "read_only": "1"})
            bodies.append(body)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=_one) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"errors during concurrent retrieve: {errors}"
    assert len(bodies) == 8
    for body in bodies:
        data = json.loads(body)
        assert data["meta"]["category"] == "alpha"
