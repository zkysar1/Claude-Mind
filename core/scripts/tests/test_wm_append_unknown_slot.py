""" — an append may not MINT a working-memory lane nobody reads.

THE DEFECT: `resolve_slot()` returns `(slots_dict, name, False)` for ANY
unrecognised name, and both append paths then did `parent[key] = []`. So a
mistyped slot name was accepted at rc=0 / HTTP 200, creating a brand-new lane
that no consumer iterates and no carrier mirrors. The write reported success and
the entry was gone. MEASURED 2026-08-19 (alpha worker Body, hostname cc-08,
uname -r 6.8.0-137-generic, own-cloud) across the two live WM files on that box:
ten orphan lanes, with ZERO references anywhere in the tree for any of them.

THE FIX IS SCOPED TO LANE CREATION, and the tests below pin BOTH halves of that
scoping, because getting either wrong is worse than the defect:

  * refuse too little -> the orphan lanes come back;
  * refuse too much  -> a legitimate dynamically-minted lane loses writes, and
    guard-4044 states the asymmetry that makes THAT the worse direction (a
    dropped write is unrecoverable, a skipped read is not).

`test_existing_lane_append_is_untouched` is the second half. Do not delete it as
redundant — it is the only thing standing between this check and the ~25 live WM
slots that sit in no static registry.

Every predicate assertion calls the PRODUCTION function `wm.unknown_lane_refusal`
rather than re-deriving its body in the probe (guard-4323): a matcher has a
pattern half and an application half, and re-implementing the application
validates a fix the real caller applies differently — silently green.

Run:
  STORAGE_BACKEND=local python3 -m pytest core/scripts/tests/test_wm_append_unknown_slot.py -q
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

CORE_ROOT = Path(__file__).resolve().parent.parent.parent
PROJECT_ROOT = CORE_ROOT.parent
sys.path.insert(0, str(CORE_ROOT / "scripts"))

import wm  # noqa: E402

# The ten orphan lanes measured on cc-08 2026-08-19, plus the two the goal
# reports on cc-07. Names are the evidence, so they are listed literally rather
# than generated: `spark`/`exp`/`encoding` are truncations of the *_capture
# lanes, the `--*` three are a command-line FLAG consumed as the slot argument,
# and the rest are one-off inventions with no reader.
MEASURED_ORPHANS = (
    "spark", "exp", "encoding",
    "--json", "--stdin-json", "--load-bearing",
    "handoff_rows", "recent_findings",
    "experience_candidates", "encoding_candidates",
    "experience_capture", "experience_captures",   # the two cc-07 lanes
)

# Real append targets, from the call-site census below plus the registries.
MEASURED_LEGIT = (
    "knowledge_debt", "sensory_buffer", "micro_hypotheses",
    "goals_completed_this_session", "proactive_escalation_log",
    "encoding_queue", "notification_log",
    "spark_capture", "exp_capture", "hyp_capture", "encoding_capture",
    "known_blockers", "recent_violations", "conclusions", "active_context",
    "loop_state.signals.goals_since_last_tree_update",   # dotted: root decides
)


def test_unregistered_lane_creation_is_refused():
    """Every measured orphan is refused when the lane does not yet exist."""
    allowed = [s for s in MEASURED_ORPHANS
               if wm.unknown_lane_refusal(s, False) is None]
    assert not allowed, (
        f"these orphan lane names would still be minted silently: {allowed}"
    )


def test_registered_lane_creation_is_allowed():
    """Every real append target is allowed even when the lane is absent.

    The absent case is the one that matters: wm-prune's scalar-eviction nulls a
    list slot that is not in ARRAY_SLOTS (guard-2552), so `notification_log` and
    `proactive_escalation_log` routinely read None and are re-created by the
    next append. A registry that omitted them would break /notify-user's
    rate-limiting on any pruned WM, silently.
    """
    refused = {s: wm.unknown_lane_refusal(s, False) for s in MEASURED_LEGIT}
    refused = {k: v for k, v in refused.items() if v is not None}
    assert not refused, f"legitimate append targets refused: {refused}"


def test_existing_lane_append_is_untouched():
    """The scoping half. An unregistered name whose lane ALREADY EXISTS is
    allowed — that is how the ~25 wm-set.sh-minted dynamic slots keep working.
    The `--*` flag names are the deliberate exception: no slot is legitimately
    named after a command-line flag, so those are refused either way."""
    for name in ("handoff_rows", "recent_findings", "spark", "some_future_slot"):
        assert wm.unknown_lane_refusal(name, True) is None, (
            f"append to the EXISTING lane {name!r} was refused — this check is "
            f"scoped to lane CREATION and must never gate an existing lane"
        )
    for flag in ("--json", "--stdin-json", "--load-bearing"):
        assert wm.unknown_lane_refusal(flag, True) is not None, (
            f"{flag!r} must be refused even when the lane exists"
        )


def test_refusal_names_the_near_miss():
    """The message has to be actionable, or the loud failure just relocates the
    guesswork. A truncated capture name must name its full sibling."""
    for typo, expect in (("spark", "spark_capture"),
                         ("exp", "exp_capture"),
                         ("experience_capture", "exp_capture")):
        msg = wm.unknown_lane_refusal(typo, False) or ""
        assert expect in msg, f"refusal for {typo!r} does not suggest {expect!r}: {msg}"


# --- Anti-staleness: the registry must cover the live call-site population ----

# A real invocation is piped into (or bash-invoked as) the wrapper. Prose
# mentions of the wrapper in comments and docstrings ("wm-append.sh is
# daemon-routed", "wm-append.sh discarded the response") are not, and there are
# many of them — the pipe/bash prefix is what separates the two.
_APPEND_CALL = re.compile(
    r"(?:\|\s*|bash\s+|/)\s*(?:[\w./-]*/)?wm-append\.sh\s+([A-Za-z_][A-Za-z0-9_.]*)"
)
_SCAN_ROOTS = ("core", ".claude", "mind_api", "world", ".mind-data/world")


def _scan_append_call_sites() -> dict:
    found: dict[str, set] = {}
    scanned = 0
    for sub in _SCAN_ROOTS:
        root = PROJECT_ROOT / sub
        if not root.exists():
            continue
        for f in root.rglob("*"):
            if f.suffix not in (".sh", ".py", ".md") or not f.is_file():
                continue
            scanned += 1
            try:
                txt = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for m in _APPEND_CALL.finditer(txt):
                found.setdefault(m.group(1), set()).add(str(f.relative_to(PROJECT_ROOT)))
    return {"slots": found, "scanned": scanned}


def test_every_wm_append_call_site_is_registered():
    """Derive the production append population from the tree and require every
    member to be registered.

    This is the anti-staleness mechanism for APPEND_CREATABLE_EXTRA. Without it
    the registry inherits exactly the failure mode guard-2552 describes: adding
    a new lane works in testing and the omission surfaces only at runtime, as
    silence. Here, forgetting to register a new append target fails at authoring
    time with the slot named.

    The floor assertion is not decoration (guard-1641/guard-2421): a scan that
    reports "clean" without reporting what it scanned is ambiguous between
    counted-zero and never-ran, and a regex that silently stops matching would
    otherwise make this test pass forever.
    """
    result = _scan_append_call_sites()
    found, scanned = result["slots"], result["scanned"]
    assert scanned > 500, f"call-site scan only reached {scanned} files — scan is broken"
    assert len(found) >= 8, (
        f"call-site scan found only {len(found)} slots ({sorted(found)}) — the "
        f"pattern has stopped matching real invocations"
    )
    creatable = wm.append_creatable_slots()
    unregistered = {s: sorted(files) for s, files in found.items()
                    if s.split(".")[0] not in creatable}
    assert not unregistered, (
        f"wm-append.sh call sites target unregistered slots: {unregistered}. "
        f"Register them in APPEND_CREATABLE_EXTRA in BOTH core/scripts/wm.py "
        f"and mind_api/src/endpoints/wm_write.py."
    )


# --- End-to-end through the two append paths ---------------------------------

def _init_tmp_wm(tmp: Path):
    os.environ["BODY_WM_PATH"] = str(tmp / "working-memory.yaml")
    wm.cmd_init(SimpleNamespace())


def _cli_append(slot: str, item: dict, monkeypatch):
    """Drive wm.cmd_append exactly as the CLI entry point does."""
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(json.dumps(item)))
    wm.cmd_append(SimpleNamespace(slot=slot))


def test_cli_append_end_to_end(monkeypatch):
    """cmd_append refuses an unknown lane and still creates a registered one.

    The second half is the positive control: without it, a check that refused
    EVERY append would pass the first half while breaking working memory
    entirely.
    """
    import yaml

    original = os.environ.get("BODY_WM_PATH")
    with tempfile.TemporaryDirectory() as tmpd:
        tmp = Path(tmpd)
        try:
            _init_tmp_wm(tmp)

            with pytest.raises(SystemExit) as exc:
                _cli_append("experience_capture", {"observation": "x"}, monkeypatch)
            assert exc.value.code == 1, "refusal must exit 1, not 0"

            with pytest.raises(SystemExit):
                _cli_append("--json", {"observation": "x"}, monkeypatch)

            # POSITIVE CONTROL: a registered lane that does not exist yet is
            # still created and written.
            _cli_append("spark_capture", {"observation": "kept"}, monkeypatch)

            data = yaml.safe_load(Path(os.environ["BODY_WM_PATH"]).read_text(encoding="utf-8"))
            slots = data.get("slots") or {}
            assert "experience_capture" not in slots, "refused lane was minted anyway"
            assert "--json" not in slots, "flag-named lane was minted anyway"
            assert len(slots.get("spark_capture") or []) == 1, \
                "registered lane was not created — the refusal is too broad"
        finally:
            if original is None:
                os.environ.pop("BODY_WM_PATH", None)
            else:
                os.environ["BODY_WM_PATH"] = original


def test_daemon_append_endpoint_refuses_unknown_slot():
    """The LIVE path. wm-append.sh is daemon-only, so POST /v1/wm/append is what
    actually runs; a wm.py-only fix would leave production unchanged while
    reading as correct in the diff (guard-742/547).

    Runs against an in-process daemon on a tmp project root — hermetic, so no
    daemon_integration marker is needed.
    """
    import urllib.error
    import urllib.request

    import yaml

    from _daemon_fixture import DaemonFixture

    def _post(port, slot, item):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/wm/append?slot={slot}",
            data=json.dumps(item).encode("utf-8"), method="POST")
        req.add_header("X-Mind-Agent", "alpha")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    with tempfile.TemporaryDirectory() as tmpd:
        tmp = Path(tmpd)
        world = tmp / "world"
        world.mkdir()
        with DaemonFixture(world, agent="alpha") as df:
            wm_path = (df.project_root / "agents" / "alpha" / "session"
                       / "working-memory.yaml")
            wm_path.write_text(yaml.safe_dump({
                "session_start": "2026-08-19T00:00:00",
                "slots": {"spark_capture": []},
                "slot_meta": {},
            }), encoding="utf-8")

            # Pre-fix shape: HTTP 200 and a silent new lane. Now a loud 4xx.
            status, body = _post(df.port, "experience_capture", {"observation": "x"})
            assert status == 400, f"unknown capture-lane name still accepted: {status} {body!r}"
            assert body.get("error") == "unknown_slot", f"wrong error shape: {body!r}"

            status, body = _post(df.port, "--json", {"observation": "x"})
            assert status == 400, f"flag-named slot still accepted: {status} {body!r}"

            # POSITIVE CONTROL: the endpoint still appends to a real lane, and
            # still CREATES a registered lane that is absent from the file.
            status, body = _post(df.port, "spark_capture", {"observation": "kept"})
            assert status == 200, f"registered lane refused: {status} {body!r}"
            # `sensory_buffer` is absent from the seeded file above, so this
            # exercises the CREATE path for a registered lane — the case the
            # refusal is one line away from breaking.
            status, body = _post(df.port, "sensory_buffer", {"observation": "kept"})
            assert status == 200, f"absent registered lane refused: {status} {body!r}"

            after = yaml.safe_load(wm_path.read_text(encoding="utf-8"))
            slots = after.get("slots") or {}
            assert "experience_capture" not in slots, "refused lane was minted anyway"
            assert "--json" not in slots, "flag-named lane was minted anyway"
            assert len(slots.get("spark_capture") or []) == 1
            assert len(slots.get("sensory_buffer") or []) == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
