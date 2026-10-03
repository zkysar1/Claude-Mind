"""landing-decompose.py: per-landing size and time-to-tight from a session transcript ( outcome 5).

The transcript is synthetic and tiny. Its numbers are chosen so each assertion pins ONE rule of the tool's definitions: the
first call is the first MAIN-CHAIN assistant row after a boundary, a sidechain row never counts, a usage row is the SUM of
its three token fields, tight is the sensor's own zone, and a landing that never reaches tight prints no time instead of a
made-up one. The usage rows sit well clear of the zone lines (62.5% and 93.75% of the 480,000 limit) so a threshold tweak
in the sensor does not break the fixture.
"""
import importlib.util
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = PROJECT_ROOT / "core" / "scripts" / "landing-decompose.py"
LIMIT = 480000


def _t(minute, second=0):
    return "2026-01-01T00:%02d:%02d.000Z" % (minute, second)


def _attachment(kind, size, minute, rendered=True):
    e = {"type": "attachment", "timestamp": _t(minute), "attachment": {"type": kind}}
    if rendered:
        e["rendered"] = [{"type": "text", "text": "x" * size}]
    return e


def _assistant(tokens, minute, second=0, sidechain=False):
    # the three usage fields must be SUMMED: 10 input + 20,000 cache_creation + the rest as cache_read
    return {"type": "assistant", "isSidechain": sidechain, "timestamp": _t(minute, second),
            "message": {"role": "assistant", "content": [],
                        "usage": {"input_tokens": 10, "cache_creation_input_tokens": 20000,
                                  "cache_read_input_tokens": tokens - 20010, "output_tokens": 5}}}


def _boundary(minute):
    return {"type": "system", "subtype": "compact_boundary", "timestamp": _t(minute), "version": "9.9.9",
            "compactMetadata": {"trigger": "auto", "preTokens": 470000, "postTokens": 30000}}


TRANSCRIPT = [
    {"type": "user", "timestamp": _t(0), "message": {"role": "user", "content": "before any landing"}},
    _assistant(400000, 0, 30),  # before the first boundary: belongs to no landing
    _boundary(1),
    _attachment("instructions", 1000, 1),
    _attachment("invoked_skills", 500, 1),
    _attachment("file", 200, 1),
    _attachment("compact_file_reference", 50, 1),
    _attachment("hook_success", 40, 1),
    _attachment("session_context", 30, 1),
    _attachment("credential_org", 0, 1, rendered=False),  # no model-visible text: counted in no_text, in no bucket
    {"type": "user", "isCompactSummary": True, "timestamp": _t(1), "message": {"role": "user", "content": "s" * 300}},
    _assistant(120010, 1, 10),                  # landing 1's first call
    _assistant(999999, 3, 0, sidechain=True),   # a sub-agent's row: must count for nothing
    _assistant(300000, 5, 10),                  # 62.5% of the limit: normal
    _assistant(450000, 11, 10),                 # 93.75%: tight, 10.0 minutes after the first call
    _assistant(470000, 13, 0),                  # the cycle's peak
    _boundary(30),
    _attachment("instructions", 2000, 30),
    _assistant(130000, 30, 20),                 # landing 2's first call
    _assistant(200000, 50, 0),                  # never gets near tight
]


def _write(tmp_path, entries, extra_lines=()):
    p = tmp_path / "transcript.jsonl"
    p.write_text("\n".join([json.dumps(e) for e in entries] + list(extra_lines)) + "\n", encoding="utf-8")
    return p


def _run(path, *args, env_drop=(), env_add=None):
    env = dict(os.environ)
    env["STORAGE_BACKEND"] = "local"
    for k in ("MIND_SID", "MIND_AGENT") + tuple(env_drop):
        env.pop(k, None)
    env.update(env_add or {})
    return subprocess.run([sys.executable, str(SCRIPT), str(path), *args], env=env, capture_output=True, text=True,
                          timeout=120)


def _json(path, *args, **kw):
    r = _run(path, "--limit", str(LIMIT), "--json", *args, **kw)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_buckets_first_call_and_time_to_tight(tmp_path):
    d = _json(_write(tmp_path, TRANSCRIPT))
    one, two = d["landings"]
    assert one["buckets"] == {"instructions": 1000, "invoked_skills": 500, "summary": 300, "files": 250, "hook": 40,
                              "other": 30}
    assert one["total_bytes"] == 2120
    assert one["no_text"] == 1
    assert one["first_tokens"] == 120010
    assert one["of_autocompact_pct"] == 25.0
    assert one["to_tight_min"] == 10.0
    assert one["peak_tokens"] == 470000
    assert (one["trigger"], one["pre_tokens"], one["post_tokens"], one["version"]) == ("auto", 470000, 30000, "9.9.9")
    assert two["buckets"]["instructions"] == 2000 and two["total_bytes"] == 2000
    assert two["first_tokens"] == 130000


def test_a_landing_that_never_reaches_tight_has_no_time(tmp_path):
    d = _json(_write(tmp_path, TRANSCRIPT))
    two = d["landings"][1]
    assert two["to_tight_min"] is None and two["tight_ts"] is None
    assert two["peak_tokens"] == 200000
    assert d["summary"]["reached_tight"] == 1 and d["summary"]["landings"] == 2


def test_summary_runs_and_population(tmp_path):
    d = _json(_write(tmp_path, TRANSCRIPT))
    assert d["summary"]["instruction_runs"] == [[1000, 1, 1], [2000, 2, 2]]
    assert d["summary"]["no_text_entries"] == 1
    assert d["file"] == "transcript.jsonl" and d["limit"] == LIMIT
    assert d["entries"] == len(TRANSCRIPT) and d["unparsable"] == 0


def test_an_unparsable_line_is_counted_not_fatal(tmp_path):
    d = _json(_write(tmp_path, TRANSCRIPT, extra_lines=["{not json"]))
    assert d["unparsable"] == 1 and len(d["landings"]) == 2


def test_table_output_names_the_population_and_honours_last(tmp_path):
    r = _run(_write(tmp_path, TRANSCRIPT), "--limit", str(LIMIT), "--last", "1")
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert lines[0].startswith("population: transcript.jsonl | ") and "2 landings" in lines[0]
    rows = [ln for ln in lines if ln.split()[:1] and ln.split()[0].isdigit()]
    assert len(rows) == 1 and rows[0].split()[0] == "2"
    assert any("reached tight before the next landing: 1 of 2" in ln for ln in lines)
    assert any("instructions bytes by run" in ln and "1000 x1 #1-#1" in ln for ln in lines)


def test_limit_comes_from_the_window_environment_when_not_given(tmp_path):
    p = _write(tmp_path, TRANSCRIPT)
    r = _run(p, "--json", env_add={"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "600000", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "80"})
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["limit"] == LIMIT


def test_no_limit_refuses_instead_of_inventing_one(tmp_path):
    p = _write(tmp_path, TRANSCRIPT)
    r = _run(p, env_drop=("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"))
    assert r.returncode == 2 and "no autocompact limit" in r.stderr and r.stdout == ""


def test_exit_codes_for_a_missing_file_and_a_file_with_no_landing(tmp_path):
    assert _run(tmp_path / "absent.jsonl", "--limit", str(LIMIT)).returncode == 2
    p = _write(tmp_path, [TRANSCRIPT[0], TRANSCRIPT[1]])
    r = _run(p, "--limit", str(LIMIT))
    assert r.returncode == 3 and "nothing measured" in r.stderr


def test_loading_the_sensor_leaves_no_live_exit_timer():
    """guard-2138: importing context-budget-status.py arms an os._exit(0) timer; a long parse would die silently with rc 0."""
    spec = importlib.util.spec_from_file_location("landing_decompose_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    try:
        zone = mod.load_classify_zone()
        live = [t for t in threading.enumerate() if isinstance(t, threading.Timer) and not t.finished.is_set()]
        assert live == [], "the sensor's exit timer is still armed"
        assert zone(0) == "fresh" and zone(100) == "tight"
    finally:
        for t in threading.enumerate():
            if isinstance(t, threading.Timer):
                t.cancel()
