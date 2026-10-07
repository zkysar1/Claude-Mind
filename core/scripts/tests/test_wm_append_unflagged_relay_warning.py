""" - an UNFLAGGED sq-013 relay appended on a worker Body WM must say it is stranded.

THE DEFECT (guard-6181). On a Body, `load_bearing: true` is the DELIVERY predicate
for a capture, not only an eviction priority: the fast lane (capture_fast_lane /
body_capture_carrier) mirrors FLAGGED entries only, and a box holding no RUNNING
claim never pushes agents/<a>/sessions/ (guard-1579). An unflagged capture reaches
the reducer when the Body CLOSES, which on a parked or active worker can be never.
MEASURED 2026-09-26 (g-115-11077): 6 of 31 sq-013 relays sat unflagged at a /stop
and appeared in no carrier row. The writer sees rc=0 and nothing else, so the
append itself is the last moment it can still flag the entry.

THE FIX IS THREE-COMPONENT, which is why the wiring tests are the load-bearing ones:
  * PRODUCER - wm_write.py::append_slot, the LIVE path (the wrappers are
    daemon-only), returns `warning` for exactly that shape: spark_capture,
    sq_trigger == "sq-013", not load_bearing, routed to a Body WM.
  * CONSUMER - wm-append.sh's existing `warning` branch prints it. "A fix is not
    shipped when the producer emits it; it is shipped when a consumer displays
    it" (g-115-6541), so the REAL daemon response is fed through the REAL wrapper.
  * TWIN - wm.py cmd_append prints the same bytes to stderr, for parity.

Every "no warning" case runs beside a positive control in the same daemon, so a
producer that never warns at all cannot pass them (guard-2421).

Run:
  STORAGE_BACKEND=local python3 -m pytest core/scripts/tests/test_wm_append_unflagged_relay_warning.py -q
"""
from __future__ import annotations

import datetime
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

TESTS_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = TESTS_DIR.parent
REPO = CORE_SCRIPTS.parent.parent
for _p in (str(CORE_SCRIPTS), str(TESTS_DIR), str(REPO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import wm  # noqa: E402
from _bash_helpers import BASH  # noqa: E402

WRAPPER = CORE_SCRIPTS / "wm-append.sh"
WM_FILE = "working-memory.yaml"
SID = "5a5a5a5a-1107-4c02-8a00-000000000077"
RELAY = {"goal_id": "g-115-11077", "category": "framework",
         "observation": "FILING-SHAPED RELAY: a worker relay used as a test body.",
         "sq_trigger": "sq-013"}
PRUNING = {"working_memory_pruning": {
    "stale_threshold_minutes": 30, "evict_threshold_minutes": 120,
    "array_limits": {"spark_capture": 50, "exp_capture": 20}, "item_stale_minutes": {},
    "protected_slots": ["known_blockers", "knowledge_debt"]}}


def _daemon_mod():
    from mind_api.src.endpoints import wm_write
    return wm_write


def _post_append(port: int, slot: str, item: dict, sid: str | None) -> dict:
    url = f"http://127.0.0.1:{port}/v1/wm/append?" + urllib.parse.urlencode({"slot": slot})
    req = urllib.request.Request(url, data=json.dumps(item).encode("utf-8"), method="POST")
    req.add_header("X-Mind-Agent", "alpha")
    if sid:
        req.add_header("X-Mind-Sid", sid)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _seed(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now().isoformat()
    slots = {"spark_capture": [], "exp_capture": []}
    path.write_text(yaml.safe_dump({
        "session_start": now, "slots": slots,
        "slot_meta": {s: {"updated_at": now, "accessed_at": now, "update_count": 1}
                      for s in slots}}), encoding="utf-8")


def _daemon_responses(cases):
    """_daemon_responses_raw, then put sys.modules["body_capture_carrier"] back.

    wm_write._carrier_mod registers that module BEFORE exec_module, and this
    fixture's project root is a bare skeleton with no core/scripts, so the load
    fails and an EMPTY module is left cached for the rest of the process."""
    prev = sys.modules.get("body_capture_carrier")
    try:
        return _daemon_responses_raw(cases)
    finally:
        if prev is None:
            sys.modules.pop("body_capture_carrier", None)
        else:
            sys.modules["body_capture_carrier"] = prev


def _daemon_responses_raw(cases):
    """One fresh in-process daemon over a tmp world (rb-659); returns the response
    for each (slot, item, sid-or-None) case, appended in order."""
    from _daemon_fixture import DaemonFixture

    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd) / "world"
        world.mkdir()
        with DaemonFixture(world, agent="alpha") as df:
            cfg = df.project_root / "core" / "config"
            cfg.mkdir(parents=True, exist_ok=True)
            (cfg / "memory-pipeline.yaml").write_text(yaml.safe_dump(PRUNING), encoding="utf-8")
            agent_dir = df.project_root / "agents" / "alpha"
            _seed(agent_dir / "sessions" / SID / WM_FILE)
            _seed(agent_dir / "session" / WM_FILE)
            return [_post_append(df.port, slot, dict(item), sid) for slot, item, sid in cases]


# --- (a) PRODUCER: the live daemon ------------------------------------------

def test_daemon_warns_for_an_unflagged_sq013_relay_on_a_body_wm_and_for_nothing_else():
    lesson = {k: v for k, v in RELAY.items() if k != "sq_trigger"}
    cases = [
        ("spark_capture", dict(RELAY), SID),                                 # 0 POSITIVE CONTROL
        ("spark_capture", {**RELAY, "load_bearing": False}, SID),            # 1 explicit false is unflagged too
        ("spark_capture", {**RELAY, "load_bearing": True}, SID),             # 2 flagged: delivered by the fast lane
        ("spark_capture", lesson, SID),                                      # 3 a lesson, not a relay
        ("spark_capture", {**RELAY, "sq_trigger": "sq-012"}, SID),           # 4 another trigger
        ("exp_capture", dict(RELAY), SID),                                   # 5 other lane
        ("spark_capture", dict(RELAY), None),                                # 6 agent-wide WM: the reducer reads it itself
    ]
    r = _daemon_responses(cases)
    warn = _daemon_mod().UNFLAGGED_RELAY_WARNING
    # An error response has no `warning` either, so every append must have LANDED
    # for the silent cases below to mean anything.
    assert all(x.get("ok") is True for x in r), r
    assert r[0].get("warning") == warn, f"positive control: {r[0]!r}"
    assert list(r[0])[-1] == "warning", (
        f"`warning` must be the LAST key: wm-append.sh's greedy sed reads to the final quote: {list(r[0])}")
    assert r[1].get("warning") == warn, f"load_bearing false is unflagged: {r[1]!r}"
    for i, why in ((2, "flagged"), (3, "no sq_trigger"), (4, "sq-012"),
                   (5, "exp_capture"), (6, "agent-wide WM")):
        assert "warning" not in r[i], f"case {i} ({why}) must be silent, got {r[i]!r}"


# --- (b) CONSUMER: the real wrapper reads the real daemon response -----------

def _stub(response: str) -> str:
    return (
        "rt_url_encode() { printf '%s' \"$1\"; }\n"
        "rt_call() {\n"
        "    while [ $# -gt 0 ]; do shift; done\n"
        "    printf '%s' " + json.dumps(response) + "\n"
        "    return 0\n"
        "}\n"
        "rt_try_autospawn() { return 1; }\n"
        "rt_no_daemon_error() { echo 'stub: no daemon' >&2; exit 1; }\n"
    )


def _wrapper_stderr(tmp_path: Path, response: str, *, disable_warning_branch: bool = False) -> str:
    scripts = tmp_path / "core" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    src = WRAPPER.read_text(encoding="utf-8")
    if disable_warning_branch:
        marker = "*'\"warning\"'*)"
        assert src.count(marker) == 1, "the wrapper's warning branch moved: update this control"
        src = src.replace(marker, "*'\"warning-disabled-by-test\"'*)")
    (scripts / "wm-append.sh").write_text(src, encoding="utf-8")
    (scripts / "_runtime.sh").write_text(_stub(response), encoding="utf-8")
    proc = subprocess.run(
        [BASH, (scripts / "wm-append.sh").as_posix(), "spark_capture"],
        input='{"observation": "x"}', text=True, capture_output=True, timeout=60,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path)})
    assert proc.returncode == 0, proc.stderr
    return proc.stderr


def test_wrapper_prints_the_warning_from_a_real_daemon_response(tmp_path):
    real = _daemon_responses([("spark_capture", dict(RELAY), SID)])[0]
    body = json.dumps(real)
    line = f"[wm-append] {_daemon_mod().UNFLAGGED_RELAY_WARNING}"
    shown = _wrapper_stderr(tmp_path / "on", body)
    assert line in shown.splitlines(), (
        f"the operator must see the warning verbatim, one line: {shown!r}")
    # The control that lets the assertion above fail: with the wrapper's warning
    # branch disabled the same response prints no such line (guard-2421).
    muted = _wrapper_stderr(tmp_path / "off", body, disable_warning_branch=True)
    assert line not in muted.splitlines(), f"the control did not mute the branch: {muted!r}"
    quiet = _wrapper_stderr(tmp_path / "quiet", json.dumps({k: v for k, v in real.items() if k != "warning"}))
    assert "sq-013 relay is UNFLAGGED" not in quiet, quiet


# --- (c) TWIN: wm.py cmd_append, the CLI copy ---------------------------------

class _BodyWM:
    """Point the CLI WM at a Body-SHAPED (sessions/<sid>/) or agent-wide (session/)
    path through BODY_WM_PATH (guard-862: patching wm.WM_PATH would hit the live WM)."""

    def __init__(self, body_shaped: bool):
        self.body_shaped = body_shaped

    def __enter__(self):
        self._orig = os.environ.get("BODY_WM_PATH")
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name) / "agents" / "alpha"
        d = root / "sessions" / SID if self.body_shaped else root / "session"
        d.mkdir(parents=True)
        os.environ["BODY_WM_PATH"] = str(d / WM_FILE)
        wm.cmd_init(SimpleNamespace())
        return self

    def __exit__(self, *exc):
        if self._orig is None:
            os.environ.pop("BODY_WM_PATH", None)
        else:
            os.environ["BODY_WM_PATH"] = self._orig
        self._tmp.cleanup()
        return False


def _cli_stderr(item: dict, capsys) -> str:
    saved = sys.stdin
    sys.stdin = io.StringIO(json.dumps(item))
    try:
        capsys.readouterr()
        wm.cmd_append(SimpleNamespace(slot="spark_capture"))
    finally:
        sys.stdin = saved
    return capsys.readouterr().err


def _real_carrier_with_noop_mirror(monkeypatch):
    """body_capture_carrier loaded from its real file, with the mirror stubbed.

    The classifier (split_body_wm_path) stays real. A flagged append would
    otherwise mirror into the REAL world carrier, and a daemon fixture earlier in
    the same process can leave an empty module under this name (see
    _daemon_responses), so the entry is replaced rather than imported."""
    spec = importlib.util.spec_from_file_location(
        "bcc_under_test", CORE_SCRIPTS / "body_capture_carrier.py")
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    monkeypatch.setattr(real, "record_local", lambda *a, **k: None)
    monkeypatch.setattr(real, "push", lambda *a, **k: True)
    monkeypatch.setitem(sys.modules, "body_capture_carrier", real)


def test_cli_twin_warns_only_on_a_body_wm_for_an_unflagged_sq013_relay(capsys, monkeypatch):
    _real_carrier_with_noop_mirror(monkeypatch)
    line = f"[wm] {wm.UNFLAGGED_RELAY_WARNING}"
    with _BodyWM(body_shaped=True):
        positive = _cli_stderr(dict(RELAY), capsys)
        flagged = _cli_stderr({**RELAY, "load_bearing": True}, capsys)
        lesson = _cli_stderr({k: v for k, v in RELAY.items() if k != "sq_trigger"}, capsys)
    with _BodyWM(body_shaped=False):
        agent_wide = _cli_stderr(dict(RELAY), capsys)
    assert positive.splitlines().count(line) == 1, f"positive control: {positive!r}"
    for name, err in (("flagged", flagged), ("lesson", lesson), ("agent-wide", agent_wide)):
        assert "sq-013 relay is UNFLAGGED" not in err, f"{name} must be silent: {err!r}"


# --- (d) the two copies stay one string, and the string stays extractor-safe --

def test_the_two_copies_are_byte_identical_and_safe_for_the_wrapper_sed():
    d, c = _daemon_mod().UNFLAGGED_RELAY_WARNING, wm.UNFLAGGED_RELAY_WARNING
    assert d == c, "daemon and CLI copies drifted (guard-1189: same bytes on both transports)"
    assert d.isascii(), "non-ASCII text can be mangled by the wrapper's sed under a C locale"
    assert not any(ch in d for ch in '"\\\n\r\t'), (
        "a quote, backslash or control character would cut the line the wrapper prints")
    assert "load_bearing" in d and "closes" in d and "guard-6181" in d
