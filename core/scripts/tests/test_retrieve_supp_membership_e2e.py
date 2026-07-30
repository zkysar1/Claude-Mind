"""test_retrieve_supp_membership_e2e.py -- .

The retrieve endpoint records which supplementary entries it returned into
`retrieval-session.json:supplementary_items`; `utilization-feedback.sh
--helpful` credits ONLY ids found in that list. Membership must therefore be
whatever the LOADER returned -- computed once.

It was not. `load_reasoning_bank` / `load_guardrails` /
`load_pattern_signatures` select the return set with `_entry_matches`
(strict category, THEN token-overlap fallback), while the endpoint's session
writer re-derived membership with the narrower `_entry_matches_category`
(category substring only). A FREE-TEXT query matches no category key, so every
entry that arrived via the text fallback was counted, counter-bumped, and then
dropped from `supplementary_items` -- impossible to credit. The denominator
grew, the numerator was unreachable, `utility_ratio` drifted toward 0, and the
record sank out of ranking permanently. `load_reasoning_bank`'s own docstring
states the violated invariant verbatim ("the bump set MUST equal the return
set") and warns off the exact `is_universal_rb or _entry_matches_category`
predicate the endpoint used.

This is the ENDPOINT-side pin. `test_retrieve_supplementary_filter.py` pins the
loaders in isolation and passes in BOTH the broken and fixed states -- it never
touches the session writer, which is why the drift survived there.

Reachable-red (guard-1475): with the re-derivation filters restored in
mind_api/src/endpoints/retrieve.py, `test_free_text_guardrail_is_attestable`
FAILS -- the free-text-matched guardrail is returned in the response body and
absent from `supplementary_items`.

Specificity (guard-1660): a PASS here must not be satisfiable by appending the
whole store. The seed carries entries matching NEITHER predicate; they must be
absent from the response AND from `supplementary_items`. That is the refuse-case
to the fix's allow-case (guard-1451).

Pure stdlib + the shared DaemonFixture. Self-contained: never touches the live
world. In-process daemon (NOT a real subprocess) so it is NOT
daemon_integration-marked and is safe to run with a live daemon present
(guard-672 / run-full-suite-after-deep-code live-daemon exception).

Cross-references:
  - g-115-3855 -- this fix (membership computed once, by the loader)
  - retrieval-triggers.md G9/R3 -- added `_entry_matches_text` and updated the
    3 LOADERS; the session writer was never updated. This is that residue.
  - guard-367 -- "measurement must see what the scorer sees" (same class,
    different field)
  - test_retrieve_as_of_endpoint_e2e.py -- the endpoint-e2e pattern reused here
"""
from __future__ import annotations

import json
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _daemon_fixture import DaemonFixture  # noqa: E402

# A category the QUERY neither contains nor is contained by, so
# `_entry_matches_category` returns False in both directions and the text
# fallback is the ONLY way these records can be returned. Non-framework and
# applies_to=domain so the RB rows land in the domain partition (the universal
# partition has its own unconditional append path and would not discriminate).
SEED_CATEGORY = "zzz-membership-seed-cat"

# Four tokens of length >= 5; `_entry_matches_text` needs >= 2 distinct ones
# present in the entry corpus. Deliberately contains no substring of
# SEED_CATEGORY.
QUERY = "quokka telemetry drifting predicate"

GOAL_ID = "g-membership-e2e-001"

# Present in the matched entries' text -> 2 tokens >= 5 chars -> text-fallback hit.
_HIT_TEXT = "quokka telemetry pipeline behaviour"
# Shares no >=5-char token with QUERY -> neither predicate matches -> the loader
# must exclude these entirely.
_MISS_TEXT = "unrelated seed row about garden tools"


def _seed_world(tmp: Path) -> Path:
    world = tmp / "world"
    (world / "knowledge" / "tree").mkdir(parents=True, exist_ok=True)

    def _util():
        return {"retrieval_count": 0, "times_helpful": 0,
                "times_noise": 0, "last_retrieved": None}

    rb_rows = [
        {"id": "rb-supp-hit", "title": "membership seed one", "type": "success",
         "category": SEED_CATEGORY, "content": _HIT_TEXT, "applies_to": "domain",
         "status": "active", "created": "2026-07-01T00:00:00",
         "utilization": _util()},
        {"id": "rb-supp-miss", "title": "membership seed two", "type": "success",
         "category": SEED_CATEGORY, "content": _MISS_TEXT, "applies_to": "domain",
         "status": "active", "created": "2026-07-01T00:00:00",
         "utilization": _util()},
    ]
    guard_rows = [
        {"id": "guard-supp-hit", "title": "membership guard one",
         "rule": _HIT_TEXT, "trigger_condition": "always",
         "category": SEED_CATEGORY, "status": "active",
         "created": "2026-07-01T00:00:00", "utilization": _util()},
        {"id": "guard-supp-miss", "title": "membership guard two",
         "rule": _MISS_TEXT, "trigger_condition": "always",
         "category": SEED_CATEGORY, "status": "active",
         "created": "2026-07-01T00:00:00", "utilization": _util()},
    ]

    (world / "reasoning-bank.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rb_rows) + "\n", encoding="utf-8")
    (world / "guardrails.jsonl").write_text(
        "\n".join(json.dumps(r) for r in guard_rows) + "\n", encoding="utf-8")
    (world / "knowledge" / "tree" / "_tree.yaml").write_text(
        "nodes: {}\n", encoding="utf-8")
    return world


def _retrieve(port, category, goal):
    params = {"category": category, "depth": "shallow", "goal": goal}
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/retrieve?" + urllib.parse.urlencode(params),
        headers={"X-Mind-Agent": "alpha"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        assert resp.status == 200, f"HTTP {resp.status}"
        return json.loads(resp.read().decode("utf-8"))


def _manifest(project_root: Path) -> dict:
    p = project_root / "agents" / "alpha" / "session" / "retrieval-session.json"
    if not p.exists():
        raise AssertionError(
            f"retrieve wrote no utilization manifest at {p} -- the endpoint's "
            f"`effective_goal and not read_only and agent_dir` gate did not "
            f"fire, so this test measured nothing")
    return json.loads(p.read_text(encoding="utf-8"))


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="retrieve-supp-membership-") as tmpd:
        world = _seed_world(Path(tmpd))
        with DaemonFixture(world) as df:
            data = _retrieve(df.port, QUERY, GOAL_ID)
            manifest = _manifest(df.project_root)

    resp_guard_ids = {g.get("id") for g in (data.get("guardrails") or [])}
    resp_rb_ids = {r.get("id") for r in (data.get("reasoning_bank") or [])}

    supp = manifest.get("supplementary_items") or []
    supp_guard_ids = {i.get("id") for i in supp if i.get("type") == "guardrail"}
    supp_rb_ids = {i.get("id") for i in supp
                   if i.get("type") == "reasoning_bank"}

    # guard-1639: assert the collections are non-empty BEFORE drawing any
    # conclusion from set comparisons over them. An empty response would make
    # every equality below vacuously true.
    if not resp_guard_ids:
        print("FAIL: response returned 0 guardrails -- the text fallback did "
              "not fire, so the membership assertions would be vacuous. Check "
              "_entry_matches_text token thresholds against QUERY.",
              file=sys.stderr)
        return 1
    if not resp_rb_ids:
        print("FAIL: response returned 0 reasoning_bank rows -- assertions "
              "would be vacuous.", file=sys.stderr)
        return 1

    # ALLOW CASE (the fix): a guardrail reachable ONLY through the text
    # fallback is attestable. This is the reachable-red assertion -- it fails
    # with the re-derivation filters restored.
    if "guard-supp-hit" not in supp_guard_ids:
        print(f"FAIL: guard-supp-hit was RETURNED ({sorted(resp_guard_ids)}) "
              f"but is absent from supplementary_items "
              f"({sorted(supp_guard_ids)}) -- a free-text-matched guardrail "
              f"is counter-bumped and can never be credited by "
              f"utilization-feedback --helpful.", file=sys.stderr)
        return 1
    if "rb-supp-hit" not in supp_rb_ids:
        print(f"FAIL: rb-supp-hit returned but absent from "
              f"supplementary_items ({sorted(supp_rb_ids)}).", file=sys.stderr)
        return 1

    # THE INVARIANT, stated directly: membership IS the return set. This is
    # strictly stronger than the two membership checks above and is what
    # "computed once" means operationally.
    if supp_guard_ids != resp_guard_ids:
        print(f"FAIL: guardrail membership diverged from the return set. "
              f"returned-not-recorded={sorted(resp_guard_ids - supp_guard_ids)}, "
              f"recorded-not-returned={sorted(supp_guard_ids - resp_guard_ids)}",
              file=sys.stderr)
        return 1
    if supp_rb_ids != resp_rb_ids:
        print(f"FAIL: reasoning_bank membership diverged from the return set. "
              f"returned-not-recorded={sorted(resp_rb_ids - supp_rb_ids)}, "
              f"recorded-not-returned={sorted(supp_rb_ids - resp_rb_ids)}",
              file=sys.stderr)
        return 1

    # REFUSE CASE / SPECIFICITY (guard-1451, guard-1660): the fix appends the
    # loader's return set, NOT the whole store. An entry matching neither
    # predicate must be absent from the response AND the manifest. Without
    # this, "append everything unconditionally" would also pass.
    for miss_id, resp_set, supp_set in (
            ("guard-supp-miss", resp_guard_ids, supp_guard_ids),
            ("rb-supp-miss", resp_rb_ids, supp_rb_ids)):
        if miss_id in resp_set:
            print(f"FAIL: {miss_id} matches neither category nor text yet was "
                  f"RETURNED -- the loader predicate is too loose, and the "
                  f"membership equality above is therefore not evidence.",
                  file=sys.stderr)
            return 1
        if miss_id in supp_set:
            print(f"FAIL: {miss_id} was not returned yet IS recorded in "
                  f"supplementary_items -- membership is over-crediting.",
                  file=sys.stderr)
            return 1

    # The counts block and supplementary_items must agree, which is the
    # user-visible form of the same invariant (verification outcome 1).
    counts = manifest.get("counts") or {}
    if counts.get("guardrails") != len(supp_guard_ids):
        print(f"FAIL: counts.guardrails={counts.get('guardrails')} but "
              f"supplementary_items holds {len(supp_guard_ids)} guardrail(s) "
              f"-- counts and membership still disagree.", file=sys.stderr)
        return 1

    print(f"PASS: /v1/retrieve free-text membership -- guardrails "
          f"{sorted(supp_guard_ids)} and reasoning_bank {sorted(supp_rb_ids)} "
          f"recorded exactly as returned; non-matching seeds excluded from "
          f"both; counts agree. Membership computed once, by the loader.")
    return 0


def _configured_embedding_model_is_loadable() -> bool:
    """True when this box can actually load the CONFIGURED embedding model.

    Same guard as test_retrieve_as_of_endpoint_e2e.py, for the same reason and
    the same code path: on a box lacking the model pinned in core/config/tree.yaml
    the retrieve endpoint's encoder load fails under local_files_only=True, the
    request blows past the 15s urlopen bound, and the test dies with a bare
    socket TimeoutError naming neither the model nor the cause. Skipping is
    strictly more informative than that red (environment provisioning gap,
    tracked under g-115-3109). On any box where the model IS present the probe
    returns True and the membership assertions run exactly as written.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from _embedding_model import load_encoder  # noqa: PLC0415
        import yaml  # noqa: PLC0415
        # parents[3] is PROJECT_ROOT (tests -> scripts -> core -> root).
        # parents[2] is `core/`, which composes `core/core/config/tree.yaml`;
        # that path does not exist, the read raises FileNotFoundError, the
        # `except Exception` below swallows it, and the test SKIPS FOREVER
        # while reporting an unloadable embedding model. Measured 2026-07-30:
        # the model loads fine here, so the skip was pure fabrication.
        # test_retrieve_as_of_endpoint_e2e.py:186 carries the same off-by-one
        # () -- this guard was copied from it.
        cfg = yaml.safe_load(
            (Path(__file__).resolve().parents[3] / "core" / "config" / "tree.yaml")
            .read_text(encoding="utf-8"))
        name = None
        for section in (cfg or {}).values():
            if isinstance(section, dict) and section.get("embedding_model_name"):
                name = section["embedding_model_name"]
                break
        if not name:
            return True          # cannot determine -> do not mask anything
        load_encoder(name)
        return True
    except Exception:
        return False


def test_free_text_guardrail_is_attestable():
    if not _configured_embedding_model_is_loadable():
        pytest.skip(
            "configured embedding model (core/config/tree.yaml "
            "embedding_model_name) is not loadable on this box — the retrieve "
            "endpoint cannot serve, producing an opaque 15s socket timeout. "
            "Environment provisioning gap, tracked under g-115-3109; not a "
            "regression in the supplementary-membership path."
        )
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
