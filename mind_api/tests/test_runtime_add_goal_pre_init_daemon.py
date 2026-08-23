""": add-goal must survive a daemon that bound _paths.WORLD_DIR = None
at import (a daemon started before its world existed).

The category-suggest mutator runs for every goal filed WITHOUT a category and
builds tree_match's concept index. Before the fix, gates/category_suggest.py
built that index through the import-bound module global, so the whole
add-goal request 500'd with "WORLD_DIR unresolved" while ctx.paths.world was
correct on disk -- the fourth daemon call site of the g-367-08 class (the other
three are mind_api/src/world/tree.py, the retrieve endpoint and retrieve.py).

The daemon fixture runs IN-PROCESS (ThreadingHTTPServer thread), so the
monkeypatch below reaches the very module objects the endpoint uses; the
sibling g-367-08 tests in test_runtime_tree.py / test_runtime_retrieve.py
were HTTP 500 before their fix the same way, so this shape is known non-vacuous.
"""
from __future__ import annotations

import importlib
import json
import sys
import urllib.error
import urllib.parse
import urllib.request


def _post(port, path, query, body, *, agent="alpha"):
    qs = urllib.parse.urlencode(query)
    url = f"http://127.0.0.1:{port}{path}?{qs}"
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Mind-Agent", agent)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def test_add_goal_without_category_serves_when_daemon_started_before_world_was_configured(
        running_daemon, monkeypatch):
    _project_root, port = running_daemon
    # Model the pre-world daemon, and drop the memoized tree so the concept
    # index is rebuilt under the patched global (a warm cache would hide the
    # defect -- and the fix -- equally).
    monkeypatch.setattr(sys.modules["_paths"], "WORLD_DIR", None)
    cs = sys.modules.get("gates.category_suggest") or importlib.import_module("gates.category_suggest")
    cs._TREE_CACHE.clear()

    goal = {
        "title": "Probe the alpha test node after a pre-world daemon start",
        "status": "pending",
        "origin_signal": "user_directive",
        "description": ("g-367-14 endpoint probe: filed WITHOUT a category so the "
                        "category-suggest mutator runs and builds the concept index "
                        "against the fixture tree. ") * 2,
    }
    code, resp = _post(port, "/v1/aspirations/add-goal",
                       {"asp_id": "asp-001", "source": "world"}, goal)
    assert code == 200, f"add-goal must not 500 on a pre-world daemon: {code} {resp[:500]}"
    filed_goal = json.loads(resp)["goal"]
    # Both fixture nodes sit at depth 1, which category-suggest treats as
    # STRUCTURAL and never suggests -- so "uncategorized" is the mutator's
    # legitimate fall-through, not a sign it was skipped.
    assert filed_goal.get("category") == "uncategorized", resp[:500]
    # Proof the index was BUILT, and built under the REQUEST's world: the
    # memoized loader stores (nodes, concept_index) only after
    # build_concept_index returns, and its key now carries str(world_root).
    keys = list(cs._TREE_CACHE)
    assert keys, "category-suggest never cached a tree -- the loader did not complete"
    assert all(k[0].endswith("_tree.yaml") and k[3] != "None" for k in keys), keys
