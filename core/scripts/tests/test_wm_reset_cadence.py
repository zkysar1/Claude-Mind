"""Unit test for cmd_reset cadence-tracker preservation ().

cmd_reset historically only preserved SESSION_IDENTITY_FIELDS = {session_start},
nulling cadence-tracker slots like last_felt_sense_checkin. g-115-318
investigation confirmed this caused duplicate firings of cadence gates
after consolidate Step 5 wm-reset (the same gap appeared overdue twice).

This test verifies the fix: cadence-tracker slots (matching the patterns in
CADENCE_TRACKER_PATTERNS) survive cmd_reset; non-cadence slots are nulled.

Also covers RESET_SURVIVING_SLOTS (g-115-1992): journal_cluster_summaries is
written by aspirations-consolidate Step 0.65 BEFORE the Step-5 wm-reset and
consumed one-shot by Step 9 AFTER it — cmd_reset must preserve it (value +
slot_meta) or Step 9 silently degrades to the linear key_outcomes fallback
every full-path session.

And the CLI↔daemon mirror (g-115-2042): wm_write.py (the LIVE wm-reset.sh
path) hand-inlines six wm.py constants; the parity test pins all six in sync
and the endpoint test exercises the daemon reset() loop end-to-end via an
in-process daemon.

Runs against an isolated temp-dir WM file via module-level path patching.
Does NOT touch live agent working memory.

Run:
  py -3 core/scripts/tests/test_wm_reset_cadence.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

CORE_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(CORE_ROOT / "scripts"))

import wm  # noqa: E402


def main() -> int:
    failures = []

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        # Route WM I/O to the temp dir via BODY_WM_PATH — the env var that
        # wm.py's wm_path() honors per-call ( Mind/Body routing).
        # Patching wm.WM_PATH is a NO-OP for I/O and was DANGEROUS: WM_PATH is
        # a dynamic module __getattr__ property, and cmd_init/cmd_reset/read_wm/
        # write_wm resolve through wm_path() (BODY_WM_PATH env -> else
        # AGENT_DIR/session/working-memory.yaml), NOT the module attribute. The
        # old wm.WM_PATH=tmp patch left cmd_init/cmd_reset targeting the REAL
        # bound agent's WM -- running this under MIND_AGENT clobbered live
        # working memory. (, 2026-06-23)
        original_body = os.environ.get("BODY_WM_PATH")
        os.environ["BODY_WM_PATH"] = str(tmp / "working-memory.yaml")
        try:
            # 1. Initialize a fresh WM via cmd_init
            init_args = SimpleNamespace()
            wm.cmd_init(init_args)
            assert wm.WM_PATH.exists(), "init failed to create WM file"

            # 2. Seed cadence-tracker + non-cadence slots with known values.
            #    The cadence-tracker patterns require the slot name to start
            #    with "last_" and end with one of: _tick, _check, _checkin,
            #    _scan, _fire.
            cadence_slots = {
                "last_test_tick": "2026-04-30T01:00:00",
                "last_test_check": "2026-04-30T01:01:00",
                "last_test_checkin": "2026-04-30T01:02:00",
                "last_test_scan": "2026-04-30T01:03:00",
                "last_test_fire": "2026-04-30T01:04:00",
            }
            # Only use slots not in slot_types so we test the dynamically-created
            # non-cadence-slot path. (Slots in slot_types reset to their defaults:
            # None for scalars, empty dict for MAP_SLOTS, [] for ARRAY_SLOTS.
            # Verifying default-reset is wm-init's territory, not this test's.)
            non_cadence_slots = {
                "test_canary": "this_should_disappear",
                "test_marker_a": "remove_me",
            }
            # RESET_SURVIVING_SLOTS members — content payloads (not timestamps),
            # written pre-reset and consumed post-reset ().
            #
            # EVERY member is seeded by name, deliberately (). This block
            # used to carry journal_cluster_summaries ALONE as a representative, so
            # the survive-assertion below passed for ANY subset of the four worker
            # capture lanes — and encoding_capture was in fact absent from
            # RESET_SURVIVING_SLOTS for its whole life while its three siblings were
            # present. The parity test could not see it either: both the CLI and
            # daemon copies agreed, on the same wrong set. A representative member is
            # not coverage of a family. If a fifth capture lane is added, add it here.
            surviving_slots = {
                "journal_cluster_summaries": [
                    {"label": "asp-115", "summary": "framework fixes cluster"},
                    {"label": "asp-001", "summary": "recurring upkeep cluster"},
                ],
                "spark_capture": [
                    {"goal_id": "g-000-01", "observation": "worker spark payload"},
                ],
                "exp_capture": [
                    {"goal_id": "g-000-01", "execution_summary": "worker narrative"},
                ],
                "hyp_capture": [
                    {"goal_id": "g-000-01", "hypothesis_id": "2026-01-01_probe"},
                ],
                "encoding_capture": [
                    {"goal_id": "g-000-01", "fact": "worker tree-worthy fact"},
                ],
            }

            data = wm.read_wm()
            for k, v in {**cadence_slots, **non_cadence_slots, **surviving_slots}.items():
                data["slots"][k] = v
                data["slot_meta"][k] = {
                    "updated_at": "2026-04-30T01:00:00",
                    "accessed_at": "2026-04-30T01:00:00",
                    "update_count": 1,
                }
            # Also seed a session-identity field
            data["session_start"] = "2026-04-30T00:00:00"
            wm.write_wm(data)

            # Sanity: verify seeds landed
            seeded = wm.read_wm()
            for k in cadence_slots:
                if seeded["slots"].get(k) != cadence_slots[k]:
                    failures.append(f"seed failed: {k}")
            for k in non_cadence_slots:
                if seeded["slots"].get(k) != non_cadence_slots[k]:
                    failures.append(f"seed failed: {k}")

            # 3. Call cmd_reset
            reset_args = SimpleNamespace()
            wm.cmd_reset(reset_args)

            # 4. Verify cadence-tracker slots survived
            after = wm.read_wm()
            for k, expected in cadence_slots.items():
                actual = after["slots"].get(k)
                if actual != expected:
                    failures.append(
                        f"PRESERVE FAIL: cadence slot {k} expected {expected!r}, got {actual!r}"
                    )
                # Verify slot_meta also survived for cadence trackers
                meta = after.get("slot_meta", {}).get(k)
                if not meta or meta.get("update_count") != 1:
                    failures.append(
                        f"PRESERVE FAIL: cadence slot_meta {k} not preserved (got {meta!r})"
                    )

            # 5. Verify non-cadence slots were nulled (or removed for test_canary
            #    which isn't in slot_types)
            for k in non_cadence_slots:
                actual = after["slots"].get(k)
                if actual is not None:
                    failures.append(
                        f"NULL FAIL: non-cadence slot {k} should be None, got {actual!r}"
                    )

            # 6. Verify session_start was preserved (SESSION_IDENTITY_FIELDS)
            if after.get("session_start") != "2026-04-30T00:00:00":
                failures.append(
                    f"IDENTITY FAIL: session_start should be preserved, got {after.get('session_start')!r}"
                )

            # 7. Verify RESET_SURVIVING_SLOTS members survived with value + meta
            #    ( — journal_cluster_summaries crosses the reset boundary)
            for k, expected in surviving_slots.items():
                actual = after["slots"].get(k)
                if actual != expected:
                    failures.append(
                        f"SURVIVE FAIL: reset-surviving slot {k} expected {expected!r}, got {actual!r}"
                    )
                meta = after.get("slot_meta", {}).get(k)
                if not meta or meta.get("update_count") != 1:
                    failures.append(
                        f"SURVIVE FAIL: reset-surviving slot_meta {k} not preserved (got {meta!r})"
                    )

        finally:
            if original_body is None:
                os.environ.pop("BODY_WM_PATH", None)
            else:
                os.environ["BODY_WM_PATH"] = original_body

    if failures:
        print("[test] FAIL — cmd_reset cadence-tracker preservation broken")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("[test] PASS — cmd_reset preserves cadence-tracker slots, reset-surviving slots, and session_start; nulls other slots")
    return 0


def test_wm_reset_cadence_preservation():
    """Pytest entry point — runs main() under BODY_WM_PATH isolation so the
    cmd_reset cadence-preservation regression is actually exercised in the
    suite. The file previously exposed only `main()` (no test_* fn), so pytest
    collected 0 tests from it and the regression went un-run in CI. (g-115-1626)
    """
    assert main() == 0


# Constants wm_write.py hand-mirrors from wm.py. A one-sided edit to ANY of
# them silently diverges CLI-vs-daemon reset/maintain behavior (rb-915 class;
#  hit RESET_SURVIVING_SLOTS,  pins the other five).
SHARED_WM_CONSTANTS = (
    "SESSION_IDENTITY_FIELDS",
    "DEFAULT_SLOT_TYPES",
    "ARRAY_SLOTS",
    "MAP_SLOTS",
    "CADENCE_TRACKER_PATTERNS",
    "RESET_SURVIVING_SLOTS",
    # : the four worker->reducer capture lanes, as an ordered tuple.
    # Joins this list rather than getting its own bespoke check so a fifth lane
    # is covered by being registered, not by someone remembering to add a test.
    "CAPTURE_SLOTS",
)


def _extract_daemon_constants(daemon_src: str) -> dict:
    """AST-extract SHARED_WM_CONSTANTS values from the daemon source.

    literal_eval covers plain literals; CADENCE_TRACKER_PATTERNS is a tuple of
    re.compile(...) calls, extracted as its tuple of pattern STRINGS. Pass
    node.value (the RHS), never the Assign node itself.
    """
    import ast

    def value_of(vnode):
        try:
            return ast.literal_eval(vnode)
        except (ValueError, SyntaxError, TypeError):
            if isinstance(vnode, ast.Tuple):
                vals = []
                for elt in vnode.elts:
                    if (isinstance(elt, ast.Call)
                            and getattr(elt.func, "attr", "") == "compile"
                            and elt.args and isinstance(elt.args[0], ast.Constant)):
                        vals.append(elt.args[0].value)
                    else:
                        return None
                return tuple(vals)
            return None

    out = {}
    for node in ast.walk(ast.parse(daemon_src)):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in SHARED_WM_CONSTANTS):
            out[node.targets[0].id] = value_of(node.value)
    return out


def test_shared_wm_constants_parity_with_daemon():
    """The daemon endpoint module (mind_api/src/endpoints/wm_write.py) is the
    LIVE runtime path for wm-reset.sh (POST /v1/wm/reset) and inlines its own
    copies of six wm.py constants (package-relative imports prevent importing
    wm.py there). Asserts ALL six stay in sync AND that the daemon's reset()
    actually consults RESET_SURVIVING_SLOTS — a wm.py-only edit silently does
    nothing at runtime, which is how the g-115-1992 bug class re-enters.
    AST-parses the daemon source instead of importing it (relative imports
    make a standalone module load fail). (g-115-1992 + g-115-2042)
    """
    daemon_path = CORE_ROOT.parent / "mind_api" / "src" / "endpoints" / "wm_write.py"
    daemon_src = daemon_path.read_text(encoding="utf-8")
    daemon_vals = _extract_daemon_constants(daemon_src)

    for name in SHARED_WM_CONSTANTS:
        cli_val = getattr(wm, name, None)
        assert cli_val is not None, f"wm.py lost constant {name}"
        if name == "CADENCE_TRACKER_PATTERNS":
            cli_val = tuple(p.pattern for p in cli_val)
        daemon_val = daemon_vals.get(name)
        assert daemon_val is not None, (
            f"daemon wm_write.py lost its {name} mirror (or its shape became "
            f"un-extractable — update _extract_daemon_constants)"
        )
        assert daemon_val == cli_val, (
            f"{name} drift: wm.py={cli_val!r} daemon={daemon_val!r} — the two "
            f"copies MUST be edited together (rb-915 / g-115-1992)"
        )

    # Use-site check: RESET_SURVIVING_SLOTS must be consulted INSIDE reset(),
    # not just defined at module top.
    reset_body = daemon_src.split("def reset(", 1)[1].split("\ndef ", 1)[0]
    assert "RESET_SURVIVING_SLOTS" in reset_body, \
        "daemon reset() no longer consults RESET_SURVIVING_SLOTS (g-115-1992)"


def test_daemon_reset_endpoint_preserves_surviving_slot():
    """ (2): actual endpoint invocation. POST /v1/wm/reset against an
    in-process daemon (thread-local, tmp project root — hermetic, no
    daemon_integration marker needed) must preserve journal_cluster_summaries
    value + slot_meta AND a cadence tracker, null a canary slot, keep
    session_start, and report preserved_surviving in the response. Complements
    the AST parity test above: this exercises the daemon reset() LOOP, not
    just its constants.
    """
    import json
    import tempfile
    import urllib.request

    import yaml

    from _daemon_fixture import DaemonFixture

    with tempfile.TemporaryDirectory() as tmpd:
        tmp = Path(tmpd)
        world = tmp / "world"
        world.mkdir()
        with DaemonFixture(world, agent="alpha") as df:
            wm_path = (df.project_root / "agents" / "alpha" / "session"
                       / "working-memory.yaml")
            clusters = [{"label": "asp-1", "summary": "framework cluster"}]
            meta = {"updated_at": "2026-07-12T01:00:00",
                    "accessed_at": "2026-07-12T01:00:00", "update_count": 1}
            seeded = {
                "session_start": "2026-07-12T00:00:00",
                "slots": {
                    "journal_cluster_summaries": clusters,
                    "last_test_tick": "2026-07-12T01:00:00",
                    "reset_canary": "wipe_me",
                },
                "slot_meta": {
                    "journal_cluster_summaries": dict(meta),
                    "last_test_tick": dict(meta),
                    "reset_canary": dict(meta),
                },
            }
            wm_path.write_text(yaml.safe_dump(seeded), encoding="utf-8")

            req = urllib.request.Request(
                f"http://127.0.0.1:{df.port}/v1/wm/reset", method="POST")
            req.add_header("X-Mind-Agent", "alpha")
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode("utf-8"))

            assert body.get("ok") is True, f"reset endpoint failed: {body!r}"
            assert body.get("preserved_surviving") == ["journal_cluster_summaries"], (
                f"response missing/wrong preserved_surviving: {body!r}"
            )

            after = yaml.safe_load(wm_path.read_text(encoding="utf-8"))
            assert after["slots"]["journal_cluster_summaries"] == clusters, \
                "surviving slot value lost across daemon reset"
            assert after["slot_meta"]["journal_cluster_summaries"]["update_count"] == 1, \
                "surviving slot_meta lost across daemon reset"
            assert after["slots"]["last_test_tick"] == "2026-07-12T01:00:00", \
                "cadence tracker lost across daemon reset"
            assert after["slots"].get("reset_canary") is None, \
                "non-preserved slot should be nulled by reset"
            assert after.get("session_start") == "2026-07-12T00:00:00", \
                "session identity lost across daemon reset"


# --- : the predicate must match the CLASS, not a list of verbs ------

# Class (b) in guard-362's terms — "age IS signal". Every name here is a real
# live slot measured on 2026-08-18, and NINE of them were unprotected under the
# old eight-suffix-verb enumeration. The `*_last_dispatch` family is the sharpest
# case: those are the consumption-aware stamps stale-sentinel-canary uses as its
# ONLY discriminator between "consumer kept up" and "consumer bypassed the gate",
# so nulling them at evict_threshold_minutes defeats that protection on a 2h
# cycle and false-fires "stale sentinel" Investigates.
CADENCE_CLASS_SLOTS = (
    # were already protected (the eight enumerated verbs)
    "last_fresh_eyes_review", "last_strategic_scan", "last_strategic_scan_tick",
    "last_felt_sense_checkin", "last_l1_skew_check", "last_scar_tissue_check",
    "last_evolution_at_time", "last_reflection_at",
    # were NOT — verb absent from the enumeration
    "last_curriculum_eval", "last_completed_not_closed_triage",
    # were NOT — name does not START with `last_`, so no `^last_` pattern
    # could ever reach them however many verbs were listed
    "fresh_eyes_last_dispatch", "fresh_eyes_last_fire",
    "force_tree_maintain_last_dispatch", "force_metric_encoding_last_dispatch",
    "force_experience_archival_last_dispatch",
    "force_evolution_finalize_last_dispatch",
    "force_pre_apply_consult_last_dispatch",
    "force_metric_encoding_pending_last_dispatch",
)

# Class (a) — "age IS staleness", must stay evictable. The negative control
# guard-362's action_hint requires: without it, `lambda _: True` passes.
NON_CADENCE_SLOTS = (
    "active_context", "active_strategy", "knowledge_debt", "known_blockers",
    "micro_hypotheses", "sensory_buffer", "conclusions", "loop_state",
    "spark_capture", "exp_capture", "consolidation_health", "curator_overflow",
    "belief_contradiction_streaks", "portfolio_health_signal",
    # `last_goal_category` is the sharp one and the reason the TOP_LEVEL_KEYS
    # exclusion exists: it OPENS WITH `last_` yet holds a category string, not
    # a timestamp — stale-from-neglect, so it must stay evictable. It is the
    # standing negative control in test-wm-prune-cadence-protection.sh
    # (), and the first cut of the class rewrite broke it. The
    # 75-slot measurement did NOT catch it, because it is a TOP_LEVEL_KEY and
    # so never appeared in the `slots` dict that measurement enumerated — the
    # full suite caught it. Measure the population your predicate will actually
    # be APPLIED to, not the one that is convenient to list.
    "last_goal_category",
)


def test_cadence_predicate_covers_the_whole_stamp_class():
    """Class-(b) survival + class-(a) negative control (guard-362 action_hint).

    Pins the DEFECT, not just the current pattern text: before g-115-6697 this
    was eight `^last_.*_<verb>$` patterns, so a stamp's protection depended on
    which verb its author happened to choose and on the name starting `last_`.
    """
    unprotected = [s for s in CADENCE_CLASS_SLOTS if not wm._is_cadence_tracker(s)]
    assert not unprotected, (
        "cadence/dispatch stamps left evictable by CADENCE_TRACKER_PATTERNS "
        f"(wm-prune nulls these at evict_threshold_minutes): {unprotected}"
    )

    over = [s for s in NON_CADENCE_SLOTS if wm._is_cadence_tracker(s)]
    assert not over, (
        f"non-cadence slots wrongly exempted from eviction: {over}"
    )


def test_cadence_patterns_are_anchored_for_match_semantics():
    """`_is_cadence_tracker` applies `p.match()`, which anchors at position 0
    whatever the pattern says — so an un-anchored infix (`_last_`) compiles
    cleanly, reads correctly, and matches NOTHING.

    All eight original patterns opened with `^`, so this held by accident and
    was never stated. It was violated within minutes of the g-115-6697 rewrite
    and caught only because the exploratory measurement used `.search()` while
    production uses `.match()`.
    """
    for pat in wm.CADENCE_TRACKER_PATTERNS:
        assert pat.pattern.startswith("^"), (
            f"pattern {pat.pattern!r} is not ^-anchored; _is_cadence_tracker "
            f"uses .match(), so it can never fire. Write an infix as '^.*_x_'."
        )

    # Direct proof against the exact shape that regressed.
    assert wm._is_cadence_tracker("fresh_eyes_last_dispatch")


if __name__ == "__main__":
    sys.exit(main())
