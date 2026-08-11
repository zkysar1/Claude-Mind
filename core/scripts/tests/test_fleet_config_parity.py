"""test_fleet_config_parity.py — .

Two jobs:

1. HISTORICAL TRACEABILITY. g-115-3071 was filed because the last six fleet
   incidents were all CONFIG drift, none were liveness failures, and every one was
   found by a human tripping over it. Its VERIFY clause asks for confirmation that
   the checker would have caught 5 of the 6. Each incident below is replayed as the
   node-shape it actually presented, and the detection is asserted. Incident 5 is
   asserted NOT caught — recording the honest boundary of what this checker sees is
   worth more than a silent 5/6.

2. THE SECRETS CONTRACT. Env vars are compared by key NAME only. The collector is an
   allowlist emitter and a secret VALUE must never reach any output path.

NOTE ON FIXTURE VALIDITY (the lesson from checker-input-assumption-defects): a test
whose fixture is built to the checker's own assumptions proves only self-consistency.
These fixtures are built to the shape the INCIDENTS presented, and the anti-vacuity
test below asserts a healthy node produces NO drift — so a checker that returned
"drift" unconditionally, or one that returned nothing at all, fails here.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from _bash_helpers import BASH  # : bare "bash" hits the System32 WSL launcher outside pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "fleet_config_parity", CORE_SCRIPTS / "fleet_config_parity.py")
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(CORE_SCRIPTS))
    spec.loader.exec_module(mod)
    return mod


fcp = _load()

MANIFEST = {
    "required_env_keys": [
        "STORAGE_BACKEND", "STORAGE_S3_BUCKET", "STORAGE_DDB_LOCK_TABLE",
        "STORAGE_DDB_SESSIONS_TABLE", "ENVIRONMENT_ID", "MACHINE_ID",
        "OWNERSHIP_MODE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
        "AWS_DEFAULT_REGION", "ANTHROPIC_API_KEY", "USER_EMAIL",
    ],
    "optional_env_keys": ["GROQ_API_KEY"],
    "expected_resolved": {"STORAGE_BACKEND": "own-cloud", "ENVIRONMENT_ID": "ayoai-mind"},
    "toolchain": {"node_major_min": 22, "claude_min": "2.1.0"},
    "required_paths_keys": ["WORLD_PATH", "META_PATH", "AGENT_WRITE_PATH"],
    "deploy_key": {"repo": "owner/repo", "require_write": True},
}

NODE = {"agent": "testagent", "host": "test-box", "addr": "10.0.0.1",
        "user": "root", "root": "/opt/ayoai-mind"}

GOOD_KEY = "ssh-ed25519 AAAAgoodkeybody"
RO_KEY = "ssh-ed25519 AAAAreadonlykeybody"
DEPLOY_KEYS = {
    GOOD_KEY: {"read_only": False, "title": "test-box-deploy", "id": 1},
    RO_KEY: {"read_only": True, "title": "stale-readonly", "id": 2},
}


def _healthy(**over):
    f = {
        "hostname": "test-box",
        "root_exists": "yes",
        "env_file": "no_read_error",
        "env_keys": ",".join(MANIFEST["required_env_keys"]),
        "node_version": "v22.23.1",
        "claude_version": "2.1.220",
        "kernel": "6.8.0-136-generic",
        "daemon_pid": "1234",
        "daemon_environ_readable": "yes",
        "resolved_STORAGE_BACKEND": "own-cloud",
        "resolved_ENVIRONMENT_ID": "ayoai-mind",
        "resolved_MACHINE_ID": "test-box",
        "file_STORAGE_BACKEND": "own-cloud",
        "file_ENVIRONMENT_ID": "ayoai-mind",
        "file_MACHINE_ID": "test-box",
        "cli_lane_readable": "yes",
        "cli_STORAGE_BACKEND": "own-cloud",
        "cli_ENVIRONMENT_ID": "ayoai-mind",
        "cli_MACHINE_ID": "test-box",
        "paths_conf": "found",
        "paths_keys": "WORLD_PATH,META_PATH,AGENT_WRITE_PATH",
        "agent_write_roots": "2",
        "pubkeys": [GOOD_KEY],
        "configured_pubkeys": [GOOD_KEY],
    }
    f.update(over)
    # A HEALTHY node is one whose two lanes AGREE, so the CLI lane mirrors the daemon
    # lane by default — including when a caller overrode only the daemon side (the
    # blackout fixtures pass resolved_MACHINE_ID=<host>). Without this, every such
    # override silently manufactured a LANE DRIFT MACHINE_ID false positive and the
    # fixture stopped meaning "healthy". Tests that WANT lane disagreement say so by
    # passing cli_* explicitly, which this leaves untouched.
    for k in ("STORAGE_BACKEND", "ENVIRONMENT_ID", "MACHINE_ID"):
        if ("cli_%s" % k) not in over:
            f["cli_%s" % k] = f["resolved_%s" % k]
    return f


def _real(fields):
    return fcp._is_real_drift(fcp._check_node(NODE, fields, MANIFEST, DEPLOY_KEYS))


# ── anti-vacuity: the checker must be silent on a healthy node ────────────────

def test_healthy_node_produces_no_drift():
    """If this fails, every 'caught' assertion below is meaningless — a checker
    that always reports drift 'catches' everything."""
    assert _real(_healthy()) == [], "healthy node must produce zero real drift"


# ── the six incidents ─────────────────────────────────────────────────────────

def test_incident_1_lost_storage_keys_is_caught():
    """cc-02 lost 5 storage keys 2026-07-14, found by accident 11 days later."""
    survivors = [k for k in MANIFEST["required_env_keys"]
                 if not k.startswith(("STORAGE_", "AWS_"))]
    drift = _real(_healthy(env_keys=",".join(survivors)))
    assert drift, "missing storage keys must be caught"
    joined = " ".join(drift)
    assert "missing required env key" in joined
    assert "STORAGE_S3_BUCKET" in joined


def test_incident_2_claude_version_behind_is_caught():
    """Fleet running 13 Claude Code versions behind."""
    drift = _real(_healthy(claude_version="1.0.9"))
    assert any("claude" in d and "below floor" in d for d in drift)


def test_incident_3_node_below_engine_floor_is_caught():
    """Node 20 against the claude-code engines >=22 floor."""
    drift = _real(_healthy(node_version="v20.19.0"))
    assert any("below engine floor" in d for d in drift)


def test_incident_4_readonly_deploy_key_is_caught():
    """echo/zeta/bravo stranded on read-only deploy keys — caught ONLY when the
    read-only key is the CONFIGURED git identity."""
    drift = _real(_healthy(pubkeys=[RO_KEY], configured_pubkeys=[RO_KEY]))
    assert any("READ-ONLY" in d and "pushes will fail" in d for d in drift)


def test_incident_4_stale_readonly_key_on_disk_is_NOT_drift():
    """The false positive found on the first live run (2026-07-25): echo and zeta
    each carry a stale registered read-only key alongside their real read-write
    identity. Git pushes fine. Flagging that as broken is a checker whose input does
    not mean what the check assumes — the distinction is configured vs merely present."""
    fields = _healthy(pubkeys=[GOOD_KEY, RO_KEY], configured_pubkeys=[GOOD_KEY])
    assert _real(fields) == [], "a non-configured stale key must not be real drift"
    info = fcp._check_node(NODE, fields, MANIFEST, DEPLOY_KEYS)
    assert any(d.startswith("INFO") and "stale registered READ-ONLY" in d for d in info), \
        "it must still be SURFACED as INFO — invisible is the other failure mode"


def test_incident_5_tree_mirror_gap_is_NOT_caught_honest_boundary():
    """ five-file knowledge/tree mirror gap — the 1 of 6 this checker does NOT
    see, asserted STRUCTURALLY rather than by restating the healthy case.

    The boundary is not a judgement call: the collector never reads knowledge/tree or
    world content at all, so no per-node check could fire regardless of node state. Two
    facts pin it — the collector emits no tree/content field, and the manifest declares
    no tree expectation. If someone later adds mirror checking, this test fails and the
    5-of-6 claim in the goal record must be updated rather than silently drifting."""
    src = (CORE_SCRIPTS / "fleet_config_parity.py").read_text(encoding="utf-8")
    collector = src.split("_COLLECTOR = r\"\"\"", 1)[1].split("\"\"\"", 1)[0]
    for probe in ("knowledge", "/tree", "_tree.yaml", "md5sum", "sha256sum"):
        assert probe not in collector, (
            "collector now reads %r — it may be able to see content divergence. "
            "Re-evaluate the 5-of-6 traceability claim in g-115-3071." % probe)
    emitted = set(re.findall(r"^\s*say ([a-z_]+)", collector, re.M))
    assert not any("tree" in f or "content" in f or "hash" in f for f in emitted), \
        "collector emits a tree/content/hash field: %s" % sorted(emitted)
    assert "tree" not in " ".join(MANIFEST.keys()).lower(), \
        "manifest now declares a tree expectation — the boundary moved"


def test_incident_6_daemon_missing_resolved_config_is_caught():
    """Claude-Mind pointing at the shared PRODUCTION bucket with no ENVIRONMENT_ID
    and no MACHINE_ID. Reproduced LIVE on foxtrot 2026-07-25 on the first run."""
    drift = _real(_healthy(resolved_STORAGE_BACKEND="unset",
                           resolved_ENVIRONMENT_ID="unset"))
    joined = " ".join(drift)
    assert "no STORAGE_BACKEND" in joined
    assert "no ENVIRONMENT_ID" in joined


def test_file_is_not_the_authority_daemon_wins():
    """The (c) design decision, asserted: a node whose FILE says own-cloud but whose
    RUNNING daemon resolved something else must DRIFT. cc-02 would have passed a naive
    file check for four days while its live daemon disagreed."""
    drift = _real(_healthy(resolved_STORAGE_BACKEND="local",
                           file_STORAGE_BACKEND="own-cloud"))
    joined = " ".join(drift)
    assert "daemon-resolved STORAGE_BACKEND=local" in joined
    assert "file is stale" in joined


# ── lane parity () ──────────────────────────────────────────────────

def test_incident_7_lanes_disagree_is_DRIFT():
    """THE UNCOVERED MECHANISM. cc-02, 11 days: daemon environ resolved own-cloud
    (correct) while every bare CLI subprocess resolved 'local' (broken), because the
    ENVIRONMENT_ID -> environments/<id>.yaml -> STORAGE_* derivation lived only in the
    daemon's main(). Both keys were PRESENT and each lane was self-consistent, so the
    required_env_keys NAME check passed AND the single-lane resolved-value check
    passed. Only lane-vs-lane comparison sees it."""
    drift = _real(_healthy(cli_STORAGE_BACKEND="local"))
    joined = " ".join(drift)
    assert "LANE DRIFT STORAGE_BACKEND" in joined, joined
    assert "daemon=own-cloud" in joined and "cli=local" in joined, joined


def test_lane_disagreement_is_invisible_to_every_single_lane_check():
    """The claim that motivates (c2), asserted rather than argued: on the exact cc-02
    fixture, DELETING the lane-parity evidence makes the node look clean. If this ever
    fails, the incident is being caught by some other check and (c2) is redundant."""
    cc02 = _healthy(cli_STORAGE_BACKEND="local")
    assert _real(cc02), "fixture must be drifty with both lanes sampled"
    # same node, but the CLI lane was never sampled -> only INFO, no real drift
    blind = {k: v for k, v in cc02.items() if not k.startswith("cli_")}
    blind["cli_lane_readable"] = "no"
    assert _real(blind) == [], (
        "a single-lane probe reported DRIFT on the cc-02 shape — (c2) may be "
        "redundant: %s" % _real(blind))


def test_both_lanes_agree_is_PASS():
    """Direction 2 of the pin. The CLI lane was fixed in 1be1d0313, so a probe written
    today sees both lanes agree — the checker must stay SILENT on that, or the
    regression detector is a permanent false positive on a healthy fleet."""
    assert _real(_healthy()) == []


def test_cli_lane_disagreeing_with_manifest_is_DRIFT():
    """Condition (a) on the new lane: both lanes self-consistent with each other but
    both wrong is still drift — the check must not degrade into a pure equality test
    that passes when the lanes agree on the WRONG value."""
    drift = _real(_healthy(resolved_STORAGE_BACKEND="local", cli_STORAGE_BACKEND="local"))
    joined = " ".join(drift)
    assert "cli-resolved STORAGE_BACKEND=local" in joined, joined
    assert "LANE DRIFT" not in joined, "lanes agree here — only (a) should fire: %s" % joined


def test_unsampled_cli_lane_is_INFO_not_drift():
    """Fail-open: a node with no usable python3 must not be reported as misconfigured.
    The check reports that it could not run, and that is INFO, not DRIFT."""
    fields = {k: v for k, v in _healthy().items() if not k.startswith("cli_")}
    fields["cli_lane_readable"] = "no"
    assert _real(fields) == []
    info = fcp._check_node(NODE, fields, MANIFEST, DEPLOY_KEYS)
    assert any("CLI lane unsampled" in d for d in info), info


def test_missing_cli_value_is_reported_once_not_twice():
    """An unset CLI value is a per-lane fault, already named by the (a) check. It must
    not ALSO emit a LANE DRIFT line — one fault, one diagnostic."""
    drift = _real(_healthy(cli_STORAGE_BACKEND="unset"))
    joined = " ".join(drift)
    assert "CLI lane has no STORAGE_BACKEND" in joined, joined
    assert "LANE DRIFT" not in joined, joined


# ── collector execution (the gap the fresh-eyes pass named) ───────────────────
# Every test above feeds fixture fields straight into _check_node, so NONE of them
# executes the collector. That is exactly how the Windows CLI-lane defect got
# shipped: the six lane-parity tests were green while the collector could not
# sample the lane at all on one of five fleet nodes. These two run the real thing.

def _run_collector(extra_path=None, shape_spec=""):
    """Execute the real _COLLECTOR against this repo. Returns parsed key=value dict.

    shape_spec defaults to "" so the g-115-3344 value-shape block stays inert for the
    lane-parity callers below; pass one to exercise it against the live environment.
    """
    import shlex
    script = fcp._COLLECTOR % {"root": shlex.quote(str(CORE_SCRIPTS.parent.parent)),
                               "shape_spec": shape_spec}
    env = dict(os.environ)
    if extra_path:
        env["PATH"] = "%s%s%s" % (extra_path, os.pathsep, env.get("PATH", ""))
    p = subprocess.run([BASH, "-s"], input=script, capture_output=True,
                       text=True, timeout=180, env=env)
    out = {}
    for line in p.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return p, out


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash unavailable")
def test_collector_actually_samples_the_cli_lane():
    """POSITIVE CONTROL. If the collector cannot sample the lane on the box running
    the suite, every lane-parity test above is green against a field the collector
    never produces — the check would be inert in production while fully tested."""
    p, out = _run_collector()
    assert p.returncode == 0, p.stderr[:500]
    assert out.get("cli_lane_readable") == "yes", (
        "collector could not sample the CLI lane on this box — the (c2) check is "
        "inert here regardless of what the fixture-driven tests report. "
        "cli_lane_error=%s" % out.get("cli_lane_error"))
    for k in ("cli_STORAGE_BACKEND", "cli_ENVIRONMENT_ID", "cli_MACHINE_ID"):
        assert k in out, "collector did not emit %s: %s" % (k, sorted(out))


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash unavailable")
def test_noise_emitting_interpreter_is_rejected_not_parsed_as_unset(tmp_path):
    """THE WINDOWS REGRESSION. A Microsoft Store python stub prints 'Python was not
    found...' and exits nonzero. Accepting output on NON-EMPTINESS would let that
    text through as a readable lane whose three values all parse to 'unset', firing
    a FALSE DRIFT on a healthy node every sweep. The shape gate must reject it and
    say so."""
    fake = tmp_path / "fakebin"
    fake.mkdir()
    for name in ("py", "python3", "python"):
        stub = fake / name
        stub.write_text(
            "#!/usr/bin/env bash\n"
            "echo 'Python was not found; run without arguments to install from the "
            "Microsoft Store'\nexit 9009\n",
            encoding="utf-8")
        stub.chmod(0o755)

    p, out = _run_collector(extra_path=str(fake))
    assert p.returncode == 0, p.stderr[:500]
    assert out.get("cli_lane_readable") == "no", (
        "a noise-emitting interpreter was accepted as a readable lane — this is the "
        "false-DRIFT path: %s" % {k: v for k, v in out.items() if k.startswith("cli_")})
    assert out.get("cli_lane_error") == "bad_output_shape", (
        "the failure CLASS must distinguish 'something ran and misbehaved' from "
        "'nothing to run'; got %r" % out.get("cli_lane_error"))
    # And the captured interpreter text must never be emitted — it is untrusted.
    assert not any("Microsoft Store" in v for v in out.values()), (
        "interpreter output leaked into collector stdout: %s" % out)


# ── secrets contract ──────────────────────────────────────────────────────────

def test_secrets_contract_self_test_passes():
    """The shipped --self-test must stay green: no secret value in collector output,
    key NAMES still present (non-vacuous), allowlisted config values readable."""
    p = subprocess.run([sys.executable, str(CORE_SCRIPTS / "fleet_config_parity.py"),
                        "--self-test"], capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, "secrets-contract self-test failed:\n%s\n%s" % (p.stdout, p.stderr)


def test_collector_emits_only_allowlisted_value_keys():
    """Structural: the collector's `say resolved_/file_` lines must cover exactly the
    three non-secret identifiers. A fourth would mean a value-bearing read crept in."""
    src = (CORE_SCRIPTS / "fleet_config_parity.py").read_text(encoding="utf-8")
    collector = src.split("_COLLECTOR = r\"\"\"", 1)[1].split("\"\"\"", 1)[0]
    loops = re.findall(r"for K in ([A-Z_ ]+); do", collector)
    assert loops, "value-read loop not found — collector shape changed"
    for grp in loops:
        keys = set(grp.split())
        assert keys == {"STORAGE_BACKEND", "ENVIRONMENT_ID", "MACHINE_ID"}, (
            "collector reads VALUES for %s — only the three non-secret config "
            "identifiers are allowlisted (fleet-manifest.yaml value_visible_env_keys)"
            % sorted(keys))


def test_self_detection_prevents_ssh_to_self():
    """The local node must be collected locally. Probing self over ssh failed
    `Permission denied (publickey)` on the first live run, so the ONE node whose
    config is always readable reported UNREACHABLE."""
    # platform.node(), not os.uname(): os.uname does NOT exist on Windows, so
    # this raised AttributeError before ever reaching the assertion — the test
    # could not run at all on a win32 fleet box. platform.node() is the
    # portable equivalent and returns the same nodename on POSIX.
    import platform
    me = {"host": platform.node(), "addr": "127.0.0.1"}
    assert fcp._is_self(me) is True
    assert fcp._is_self({"host": "definitely-not-this-box", "addr": "10.0.0.9"}) is False


# A carrier timestamp that is always inside the live window, computed rather than
# hardcoded: a literal date would silently age past _BOX_LIVE_WINDOW_HOURS and flip
# every box fixture from DRIFT to INFO, turning these tests green for the wrong
# reason at an unpredictable future date.
_FRESH_TS = datetime.now().isoformat(timespec="seconds")


def _run_with_fleet(monkeypatch, unreachable_agents, blackout_cfg=None, peers=4,
                    nodes_filter=None, self_unreachable=False, roster=None,
                    boxes=None):
    """Drive run() over a synthetic fleet: one self node plus `peers` peers.

    Self always collects (it is read locally), which is the real-world shape — during
    an outage the self node still reports while every peer goes dark. Returns
    (rc, filed) where filed is the list of (kind, agent-names) the filer was called
    with, so a test can assert on invocation rather than on log text.
    """
    nodes = [{"agent": "selfagent", "host": "self-box", "addr": "10.0.0.1"}]
    nodes += [{"agent": "peer%d" % i, "host": "peer%d-box" % i, "addr": "10.0.0.%d" % (i + 2)}
              for i in range(peers)]
    manifest = dict(MANIFEST, nodes=nodes)
    if blackout_cfg is not None:
        manifest["blackout_escalation"] = blackout_cfg

    monkeypatch.setattr(fcp, "_load_manifest", lambda *a, **k: (manifest, None))
    monkeypatch.setattr(fcp, "_gh_deploy_keys", lambda repo: (DEPLOY_KEYS, None))
    monkeypatch.setattr(fcp, "_is_self", lambda n: n.get("agent") == "selfagent")
    # Roster parity () reads the LIVE world from inside run(), so without
    # this stub every test here would depend on the real fleet: the synthetic
    # manifest names selfagent/peerN, the live roster names alpha/bravo/..., and
    # all five real agents would report as unmeasured -> rc 1. That is not a
    # fixture inconvenience, it is the correct alarm — these tests measure the
    # BLACKOUT path, so the roster is pinned to agree with the synthetic manifest
    # and roster parity is exercised by its own tests below.
    _roster = {n["agent"] for n in nodes} if roster is None else set(roster)
    monkeypatch.setattr(fcp, "_live_roster", lambda world_dir=None: (_roster, None))
    # Box parity () reads the live BOX set from inside run(), so it needs
    # the same pin as the roster directly above, for the same reason AND one more.
    # The same reason: unpinned, the synthetic manifest names self-box/peerN-box
    # while the store names the real boxes, so every real box reports unmeasured
    # -> rc 1.
    # The extra reason, which is why a pin is required rather than merely tidy:
    # conftest forces STORAGE_BACKEND=local for every test, under which
    # enumerate_carriers cannot reach the authoritative store and _box_parity
    # correctly returns checked=False. That is the right runtime behaviour and it
    # makes the DRIFT and exit-code paths structurally unreachable under pytest —
    # so without this pin the box checks would be untestable while LOOKING tested,
    # which is the exact vacuous shape this whole checker exists to refuse.
    _boxes = ({n["host"]: _FRESH_TS for n in nodes} if boxes is None
              else dict(boxes))
    monkeypatch.setattr(fcp, "_live_boxes",
                        lambda agents_root_dir=None: (
                            _boxes, {"complete": True, "read_via": "authoritative",
                                     "reason": None}))

    # **kw, not a pinned arity: run() passes shape_spec= () and any future
    # collector input the same way. A double narrower than its production call shape
    # fails on the CALL rather than on the behaviour under test (guard-920).
    def fake_collect(node, timeout=45, **kw):
        if self_unreachable and node.get("agent") == "selfagent":
            return {}, "local collect rc=2"
        if node.get("agent") in unreachable_agents:
            return {}, "ssh: connect timed out"
        # Host-match the fixture to THIS node, else every reachable node reports
        # DRIFT on MACHINE_ID and the drift path masks what these tests measure.
        host = node["host"]
        return _healthy(hostname=host, resolved_MACHINE_ID=host,
                        file_MACHINE_ID=host), None
    monkeypatch.setattr(fcp, "_collect", fake_collect)

    filed = []
    monkeypatch.setattr(fcp, "_file_investigate",
                        lambda nodes_, payload, kind="drift":
                        filed.append((kind, sorted(n["agent"] for n in nodes_))))

    rc = fcp.run(file_investigate=True, nodes_filter=nodes_filter)
    return rc, filed


def test_total_blackout_exits_1_and_files_investigate(monkeypatch):
    """Every non-self node UNREACHABLE is a systemic blackout, not a blip. Measured
    before the fix: 0 PASS / 0 DRIFT / 5 UNREACHABLE ... EXIT CODE 0 — the 12h sweep
    measured nothing, filed nothing, and reported success (g-115-3162)."""
    rc, filed = _run_with_fleet(monkeypatch, {"peer0", "peer1", "peer2", "peer3"})
    assert rc == 1, "a total blackout must fail the sweep, not exit clean"
    assert [k for k, _ in filed] == ["blackout"], (
        "blackout must file its own Investigate, and must NOT be reported as drift — "
        "nothing was compared, so 'drift' would name the wrong cause")
    assert filed[0][1] == ["peer0", "peer1", "peer2", "peer3"]


def test_minority_unreachable_still_exits_0_and_files_nothing(monkeypatch):
    """Pins the tolerance the fix must NOT break: one node behind a dropped tunnel is
    a blip. Failing the sweep on it is the noise the rc=0 behaviour deliberately
    avoids, so the fix must not simply become --strict."""
    rc, filed = _run_with_fleet(monkeypatch, {"peer2"})
    assert rc == 0, "a single unreachable peer must not fail the sweep"
    assert filed == [], "a blip must not file an Investigate"


def test_single_peer_fleet_does_not_trip_blackout(monkeypatch):
    """min_non_self_nodes guard: with one peer, '100% of peers unreachable' IS the
    blip case, so a fraction test alone would reintroduce blip-fails-the-sweep on a
    small fleet."""
    rc, filed = _run_with_fleet(monkeypatch, {"peer0"}, peers=1)
    assert rc == 0
    assert filed == []


def test_blackout_escalation_can_be_disabled(monkeypatch):
    """Reversibility: the manifest switch restores the exact pre-fix behaviour."""
    rc, filed = _run_with_fleet(monkeypatch, {"peer0", "peer1", "peer2", "peer3"},
                                blackout_cfg={"enabled": False})
    assert rc == 0
    assert filed == []


def test_no_peers_does_not_divide_by_zero():
    """Found in pre-completion review, not by a failing test: a configured
    min_non_self_nodes of 0 passes the threshold check, so an empty peer list would
    reach the fraction and raise ZeroDivisionError. A single-node manifest is a real
    shape (a fresh fleet, or --nodes filtered down to self)."""
    self_only = [{"agent": "selfagent", "verdict": "PASS", "is_self": True}]
    cfg = {"blackout_escalation": {"min_non_self_nodes": 0}}
    assert fcp._is_blackout(self_only, cfg, fleet_complete=True) is False
    assert fcp._is_blackout([], cfg, fleet_complete=True) is False


def test_malformed_blackout_config_fails_open(monkeypatch):
    """A bad threshold must never turn a working sweep into a failing one."""
    rc, filed = _run_with_fleet(monkeypatch, {"peer0", "peer1", "peer2", "peer3"},
                                blackout_cfg={"unreachable_fraction": "not-a-number"})
    assert rc == 0
    assert filed == []


def test_filtered_run_does_not_trip_blackout(monkeypatch):
    """LOCATION 1 of the  fix (guard-322 — one test per location).

    run() applies nodes_filter BEFORE building results, so a deliberately-narrowed
    run used to hand _is_blackout a 2-node sample that it read as the whole fleet.
    Probed pre-fix: [zeta UNREACHABLE, echo UNREACHABLE] -> _is_blackout True, i.e.
    `--node peer0 --node peer1 --file-investigate` returned rc=1 and filed a
    'fleet blackout' Investigate while 3 unchecked nodes may have been healthy.
    A filtered run is a diagnostic, never a fleet measurement.
    """
    rc, filed = _run_with_fleet(monkeypatch, {"peer0", "peer1"},
                                nodes_filter=["peer0", "peer1"])
    assert rc == 0, (
        "a --node-filtered run must not trip the FLEET-wide blackout escalation — "
        "it never looked at the rest of the fleet")
    assert filed == [], "no fleet-blackout Investigate may be filed from a subset"

    # Discrimination control: the SAME two unreachable peers WITHOUT the filter are a
    # genuine 2-peer-of-2 blackout, so the guard must gate on the filter and nothing
    # else. Without this the test would also pass if the fix simply broke escalation.
    rc_unfiltered, filed_unfiltered = _run_with_fleet(
        monkeypatch, {"peer0", "peer1"}, peers=2)
    assert rc_unfiltered == 1 and [k for k, _ in filed_unfiltered] == ["blackout"], (
        "same failures, whole fleet -> must STILL escalate; otherwise the filter "
        "guard is just disabling the feature")


def test_blackout_report_excludes_self_from_peer_list(monkeypatch):
    """LOCATION 2 of the  fix (guard-322 — one test per location).

    _collect runs LOCALLY for self and can fail ("local collect rc=N"), so self can be
    UNREACHABLE too. The blackout branch calls its nodes "peer nodes"; passing the full
    unreachable list made a 1-self + 4-peer outage report "all 5 peer nodes UNREACHABLE"
    with the local failure buried among the peers — while that same description tells
    the reader to check THIS box before suspecting five peers.
    """
    rc, filed = _run_with_fleet(monkeypatch, {"peer0", "peer1", "peer2", "peer3"},
                                self_unreachable=True)
    assert rc == 1, "peers all down is still a blackout even if self also failed"
    kinds = [k for k, _ in filed]
    assert kinds == ["blackout"], kinds
    assert filed[0][1] == ["peer0", "peer1", "peer2", "peer3"], (
        "the filer must receive ONLY non-self nodes — 'selfagent' in this list is the "
        "count inflation and the mislabel")
    assert "selfagent" not in filed[0][1]


# ── the _daemon_val logline-precedence branch () ───────────────────
#
#  added _daemon_val (fleet_config_parity.py ~L466-482) and shipped it with
# ZERO coverage: a grep of this file for `_daemon_val` returned 0 as late as
# 2026-07-31. The two tests that LOOK like they cover it
# (test_incident_6_daemon_missing_resolved_config_is_caught,
# test_file_is_not_the_authority_daemon_wins) pass only because their fixtures carry
# no logline field at all — they exercise the FALLBACK arm, so precedence itself was
# untested in both directions.
#
# That matters more than ordinary coverage debt because this checker AUTO-FILES: a
# regression spends a whole agent iteration per false positive, and "fixing" phantom
# drift can break a healthy box. It cost 31h of false HIGH config-drift goals on
# foxtrot once already.
#
# The first two below are a PAIR and are worthless apart. Invariant 1 alone is
# satisfied by a blanket suppressor that never reports STORAGE_BACKEND drift at all;
# invariant 2 is the negative control that rules that out. Assert both or neither.


def _logline(**over):
    """A node whose daemon derives config IN-PROCESS — the  shape.

    `resolved_*` comes from /proc/<pid>/environ, which exposes only the EXEC-TIME
    block, so a daemon that derives its backend after exec reports "unset" there for
    its whole life. The startup logline states what is actually in force. `cli_*` is
    pinned explicitly (not left to _healthy's mirror-of-resolved default) so these
    fixtures cannot manufacture an unrelated LANE DRIFT line and pass for the wrong
    reason.
    """
    f = {"daemon_logline_readable": "yes", "resolved_STORAGE_BACKEND": "unset",
         "cli_STORAGE_BACKEND": "own-cloud"}
    f.update(over)
    return _healthy(**f)


def test_logline_wins_over_unset_environ():
    """INVARIANT 1 — the literal false-positive shape, which must be PASS.

    environ says "unset", the daemon's own startup line says own-cloud, the manifest
    expects own-cloud. Reading `resolved_STORAGE_BACKEND` directly yields "unset" and
    emits "daemon has no STORAGE_BACKEND (expected own-cloud)" against a node that is
    demonstrably writing to own-cloud. Deleting the logline arm of _daemon_val fails
    exactly here.
    """
    drift = _real(_logline(logline_STORAGE_BACKEND="own-cloud"))
    assert drift == [], (
        "a daemon whose startup logline declares the expected backend must produce "
        "ZERO drift even when /proc environ reports it unset — this is the g-115-3157 "
        "false positive verbatim: %r" % (drift,))


def test_local_only_daemon_is_still_caught_through_the_logline():
    """INVARIANT 2 — the negative control, and the one that can embarrass the fix.

    A genuinely local-only daemon logs "<unset->local>", which _daemon_val normalises
    to "local". Against an expected own-cloud that MUST still be drift. Without this,
    invariant 1 is indistinguishable from a blanket suppressor and the failure the
    checker exists for (2026-07-26: 28 of 49 daemon starts local-only, ~8 encodings
    stranded) goes silent while the suite stays green.
    """
    drift = _real(_logline(logline_STORAGE_BACKEND="<unset->local>",
                           cli_STORAGE_BACKEND="local"))
    assert drift, "a local-only daemon against an own-cloud manifest must be caught"
    # The EXACT expected-mismatch line, not a substring sweep. Measured 2026-07-31:
    # a `any("STORAGE_BACKEND" in d and "local" in d)` form passed against a mutant
    # that normalised <unset->local> to "own-cloud" — it was matching the unrelated
    # LANE DRIFT line the fixture's cli_/daemon disagreement produces, so the test
    # was green while the detection it exists for was gone (guard-385).
    assert any("daemon-resolved STORAGE_BACKEND=local expected own-cloud" in d
               for d in drift), (
        "the manifest-mismatch line itself must be present — matching any line that "
        "merely mentions the key passes on a lane-drift message instead: %r"
        % (drift,))


def test_unset_normalisation_is_storage_backend_only():
    """The `else "unset"` half of the same two-line normalisation.

    `<unset->local>` collapses to "local" ONLY for STORAGE_BACKEND, because that key
    genuinely defaults to local; for any other key the same prefix means "absent" and
    must fall through to the has-no-value branch. Widening the normalisation to all
    keys would silently invent an ENVIRONMENT_ID of "local" and pass invariants 1-2.
    """
    drift = _real(_logline(logline_STORAGE_BACKEND="own-cloud",
                           logline_ENVIRONMENT_ID="<unset->local>"))
    # ABSENT branch specifically ("daemon has no X"), not the MISMATCH branch
    # ("daemon-resolved X=local expected ..."). Both mention the key, so a bare
    # `"ENVIRONMENT_ID" in d` sweep passed against a mutant that returned "local"
    # for every key — the exact widening this test exists to forbid (guard-385).
    assert any("daemon has no ENVIRONMENT_ID" in d for d in drift), (
        "a <unset...> ENVIRONMENT_ID must read as ABSENT; normalising it to the "
        "literal 'local' produces a mismatch line instead, which names the key just "
        "as loudly and is a different, wrong verdict: %r" % (drift,))


def test_fallback_path_unchanged_when_logline_unreadable():
    """INVARIANT 3 — backward compatibility, so a refactor cannot make the logline
    mandatory.

    With daemon_logline_readable="no" the verdict must derive from `resolved_*`
    exactly as it did pre-g-115-3157: a good environ passes, an unset one is caught.
    Both directions are asserted because a refactor that ignored `resolved_*`
    entirely would satisfy either one alone.
    """
    ok = _real(_healthy(daemon_logline_readable="no"))
    assert ok == [], "logline unreadable + healthy environ must still pass: %r" % (ok,)

    caught = _real(_healthy(daemon_logline_readable="no",
                            resolved_STORAGE_BACKEND="unset",
                            cli_STORAGE_BACKEND="own-cloud"))
    # The ABSENT-branch line specifically. This fixture emits TWO lines naming the
    # key — the detection under test, and a lane-disagreement line
    # ("STORAGE_BACKEND disagrees: daemon=unset file=own-cloud"). A bare
    # `"STORAGE_BACKEND" in d` sweep accepts the second, so it stays green even if
    # the absent-branch detection is deleted outright. Same defect as invariant 2
    # and the normalisation test above; found by fresh-eyes review AFTER the
    # 5-mutant run passed, because M1-M5 all happened to break the surviving line
    # too (guard-385, guard-1099).
    assert any("daemon has no STORAGE_BACKEND" in d for d in caught), (
        "with no logline to consult, an unset environ must still raise the "
        "absent-branch line; matching any line that mentions the key passes on "
        "the lane-disagreement message instead: %r" % (caught,))


# ── Roster parity: the manifest node list vs the LIVE fleet () ──────
# The vacuous pass being closed: the node list is HARDCODED, so a 6th agent is
# never iterated and the tool prints "5 PASS / 0 DRIFT" while one node was never
# measured. Without these the fix is unfalsifiable — which is why the goal
# demanded them by name.

_MANI5 = dict(MANIFEST, nodes=[{"agent": a, "host": "%s-box" % a}
                               for a in ("alpha", "bravo", "echo", "zeta", "foxtrot")])


def _parity(monkeypatch, roster, *, identities=None, fleet_complete=True, err=None):
    monkeypatch.setattr(fcp, "_live_roster",
                        lambda world_dir=None: (None, err) if err else (set(roster), None))
    if identities is not None:
        monkeypatch.setattr(fcp, "_has_agent_identity",
                            lambda name, agents_root_dir=None: identities.get(name))
    return fcp._roster_parity(_MANI5, fleet_complete=fleet_complete)


def test_live_agent_missing_from_manifest_is_roster_DRIFT(monkeypatch):
    """A 6th agent that is REAL (carries self.md) and unlisted must be DRIFT.

    This is the goal's headline requirement. Asserted on the drift list
    specifically, not on the union of drift+info: routing it to info would leave
    the vacuous pass fully intact while still 'mentioning' the agent, and a
    combined assertion would accept that.
    """
    ids = {a: True for a in ("alpha", "bravo", "echo", "zeta", "foxtrot", "sigma")}
    rp = _parity(monkeypatch, ["alpha", "bravo", "echo", "zeta", "foxtrot", "sigma"],
                 identities=ids)
    assert rp["checked"] is True
    # Match the IDENTITY-CONFIRMED wording, not the bare IN-ROSTER-NOT-IN-MANIFEST
    # marker: the uncheckable-identity branch emits that marker too, so the loose
    # form stays green even if a confirmed live agent is misrouted into the
    # "could not be checked" branch. Caught by the mutation run, not by review
    # (guard-1099 / guard-385 — the same defect this file documents twice above).
    assert any("carries an agent identity" in d and "sigma" in d for d in rp["drift"]), (
        "an unlisted live agent must be reported as DRIFT via the identity-confirmed "
        "branch: %r" % (rp["drift"],))
    assert not any("sigma" in i for i in rp["info"]), (
        "sigma must NOT be downgraded to INFO: %r" % (rp["info"],))


def test_roster_row_with_no_agent_identity_is_INFO_not_drift(monkeypatch):
    """A heartbeat-table row that is not an agent must NOT raise a false DRIFT.

    Measured on the live fleet 2026-08-02 (bravo, hostname cc-05): the composed
    roster carried `test-rb671` — no agent dir, no self.md, no team-state shard,
    last_active three months stale. A bare set-difference implementation emits a
    false unmeasured-node on its FIRST run, which is exactly the "visible gap
    traded for invisible wrong data" the goal warned against in the other
    direction. rb-4246 / guard-1574: resolve a fleet member by evidence, never
    by name.
    """
    ids = {a: True for a in ("alpha", "bravo", "echo", "zeta", "foxtrot")}
    ids["test-rb671"] = False
    rp = _parity(monkeypatch, ["alpha", "bravo", "echo", "zeta", "foxtrot", "test-rb671"],
                 identities=ids)
    assert rp["drift"] == [], "roster residue must not be DRIFT: %r" % (rp["drift"],)
    assert any("test-rb671" in i and "no agent identity" in i for i in rp["info"]), (
        "residue must still be REPORTED as INFO, never silently dropped: %r" % (rp["info"],))


def test_uncheckable_identity_is_DRIFT_not_assumed_benign(monkeypatch):
    """Identity unknown -> report as unmeasured. Fail LOUD, not quiet.

    The whole point of the checker is to refuse to imply coverage it does not
    have, so the unknown case must default to the visible-gap direction.
    """
    ids = {a: True for a in ("alpha", "bravo", "echo", "zeta", "foxtrot")}
    ids["mystery"] = None
    rp = _parity(monkeypatch, ["alpha", "bravo", "echo", "zeta", "foxtrot", "mystery"],
                 identities=ids)
    assert any("mystery" in d for d in rp["drift"]), (
        "an uncheckable row must be reported as unmeasured: %r" % (rp,))


def test_has_agent_identity_reads_real_self_md(tmp_path):
    """The identity discriminator itself, unstubbed.

    Every classification test above monkeypatches _has_agent_identity to drive the
    branches, so without this the function they all depend on is never executed —
    it could return a constant and the suite would stay green.
    """
    (tmp_path / "realagent").mkdir()
    (tmp_path / "realagent" / "self.md").write_text("# realagent\n", encoding="utf-8")
    (tmp_path / "dironly").mkdir()   # a dir with no identity file
    assert fcp._has_agent_identity("realagent", tmp_path) is True
    assert fcp._has_agent_identity("dironly", tmp_path) is False
    assert fcp._has_agent_identity("absent", tmp_path) is False


def test_missing_agents_root_is_UNCHECKABLE_not_a_confident_negative(tmp_path):
    """A missing/misresolved agents root must yield None, never False.

    False routes to INFO; None routes to DRIFT. So if a wholesale-absent root
    returned False, EVERY roster row would be silently downgraded and the check
    would report "no unmeasured nodes" while having been unable to measure any
    — the exact vacuous pass this whole feature exists to close, reproduced
    inside its own evidence probe.

    Found by the guard-343 fresh-eyes pass on the first commit of this
    function, not by review: alpha/bravo/sigma all returned False against a
    nonexistent root. Asserted with `is None` rather than a falsy check,
    because False is falsy and would satisfy the loose form.
    """
    missing = tmp_path / "no-such-agents-root"
    for name in ("alpha", "bravo", "sigma"):
        assert fcp._has_agent_identity(name, missing) is None, (
            "a missing agents root must read UNCHECKABLE, not 'no identity'")

    # Positive control: with a real root present, a genuinely absent agent is
    # still a confident False. Without this the fix could be "always return
    # None", which would make every unlisted row DRIFT and destroy the
    # residue-vs-real distinction the classification depends on.
    (tmp_path / "realroot").mkdir()
    assert fcp._has_agent_identity("nobody", tmp_path / "realroot") is False


def test_live_roster_reads_real_team_state_and_drops_retired(tmp_path):
    """_live_roster against a real on-disk team-state — the INTEGRATION path.

    Every classification test stubs _live_roster, so without this the actual
    read (core team-state.yaml + per-agent shards -> _team_state.compose_agent_status)
    is never executed: the function could return a constant and the suite would
    stay green. Surfaced by the sq-019 integration-path-coverage spark on this
    goal's own close.

    Also pins the RETIREMENT delegation, which is the reason _live_roster does
    not filter rows itself: a tombstoned agent must never reach the caller, or
    it would be reported as an unmeasured node forever.
    """
    (tmp_path / "team-state.yaml").write_text(
        "agent_status:\n"
        "  alpha:\n    last_active: '2026-08-02T10:00:00'\n"
        "  legacyonly:\n    last_active: '2026-08-01T10:00:00'\n",
        encoding="utf-8")
    rows = tmp_path / "team-state" / "agents"
    rows.mkdir(parents=True)
    (rows / "bravo.yaml").write_text("last_active: '2026-08-02T11:00:00'\n", encoding="utf-8")
    (rows / "ghost.yaml").write_text(
        "last_active: '2026-07-01T09:00:00'\n"
        "retired: true\n"
        "retired_at: '2026-07-20T09:00:00'\n", encoding="utf-8")

    roster, err = fcp._live_roster(tmp_path)
    assert err is None, "a well-formed world must read cleanly: %r" % (err,)
    # union of core-file rows and shard rows...
    assert {"alpha", "bravo", "legacyonly"} <= roster
    # ...minus the tombstone. Asserted by NAME, not by len(): a count-only check
    # passes if some other row is dropped instead.
    assert "ghost" not in roster, (
        "a retired row must not reach the caller — it would be reported as an "
        "unmeasured node forever: %r" % (sorted(roster),))


def test_live_roster_reports_unreadable_world_instead_of_empty(tmp_path):
    """A world with no team-state must not silently look like an empty roster.

    Returning an empty set with err=None here would make _roster_parity report
    every manifest node as "absent from the live roster" — a confident verdict
    built on nothing read.
    """
    roster, err = fcp._live_roster(tmp_path / "does-not-exist")
    assert roster == set() or roster is None
    # No core file and no rows dir is a legitimately empty world, not an error;
    # what must NOT happen is a crash. The unreadable-FILE path is covered by
    # test_unreadable_roster_is_reported_never_silent.
    assert err is None or isinstance(err, str)


def test_manifest_node_absent_from_roster_is_INFO_not_drift(monkeypatch):
    """A retired node still listed IS measured, so it is INFO — but not silent."""
    ids = {a: True for a in ("alpha", "bravo", "echo", "zeta")}
    rp = _parity(monkeypatch, ["alpha", "bravo", "echo", "zeta"], identities=ids)
    assert rp["drift"] == [], "a listed-but-absent node is not a coverage gap: %r" % (rp["drift"],)
    assert any("foxtrot" in i for i in rp["info"]), (
        "a manifest node absent from the roster must still be surfaced: %r" % (rp["info"],))


def test_node_filtered_run_does_not_claim_roster_parity(monkeypatch):
    """--node runs measure a SUBSET, so roster parity must decline, not report.

    Same contract _is_blackout required for the same reason (g-115-3198): a
    filtered result set is indistinguishable from a whole-fleet one, so comparing
    it against the full roster would report every unselected agent as unmeasured.
    """
    rp = _parity(monkeypatch, ["alpha", "bravo", "echo", "zeta", "foxtrot", "sigma"],
                 identities={"sigma": True}, fleet_complete=False)
    assert rp["checked"] is False
    assert rp["drift"] == [] and rp["reason"], (
        "a filtered run must decline with a stated reason, not emit drift: %r" % (rp,))


def test_roster_parity_requires_fleet_complete_keyword():
    """No default: a caller that cannot state completeness must not get a verdict."""
    with pytest.raises(TypeError):
        fcp._roster_parity(_MANI5)


def test_unreadable_roster_is_reported_never_silent(monkeypatch):
    """A roster we could not read is NOT a roster that agrees.

    Reporting checked=False with the reason is the honest outcome; returning an
    empty drift list with no explanation would read as parity.
    """
    rp = _parity(monkeypatch, [], err="team-state.yaml unreadable (boom)")
    assert rp["checked"] is False
    assert "boom" in (rp["reason"] or ""), (
        "the read failure must be surfaced verbatim: %r" % (rp["reason"],))


def test_roster_drift_alone_sets_exit_1(monkeypatch):
    """Every manifest node PASSES and the run still fails, because one live
    agent was never measured.

    The exit-code half is load-bearing and separable: reporting roster drift on
    stdout while exiting 0 would leave the vacuous pass fully intact for every
    automated caller that reads only the status code — which is most of them.
    """
    monkeypatch.setattr(fcp, "_has_agent_identity",
                        lambda name, agents_root_dir=None: True)
    rc, filed = _run_with_fleet(
        monkeypatch, set(), roster={"selfagent", "peer0", "peer1", "peer2", "peer3", "sigma"})
    assert rc == 1, "an unmeasured live agent must fail the run, not just print"
    assert filed == [], "roster drift is reported, not auto-filed (out of scope here)"


def test_all_nodes_pass_and_roster_agrees_still_exits_0(monkeypatch):
    """The positive control: the fix must not make a healthy fleet fail.

    Without this, every assertion above is satisfied by a function that always
    reports drift.
    """
    rc, filed = _run_with_fleet(monkeypatch, set())
    assert rc == 0, "a healthy fleet with a matching roster must still pass"
    assert filed == []


# ── Box parity: the manifest HOST list vs the live BOX set () ──────
# The vacuous pass being closed here is one level subtler than the roster one
# above. Roster parity EXISTS, RAN, and reported nothing — because both of its
# sides are AGENT-keyed while the surface it guards is BOX-keyed. Once one agent
# can occupy two boxes (Mind/Body split) and a box can host no agent at all
# (the agent-agnostic EXTRA box), agent-set stops equalling box-set and a box
# becomes structurally invisible to a correct agent-keyed check.
#
# Measured 2026-08-07 from cc-08: three live boxes outside the manifest, two of
# them running a node major the manifest pins against — found by a human sweep,
# never by this tool.

_FRESH = _FRESH_TS
_STALE = (datetime.now() - timedelta(hours=fcp._BOX_LIVE_WINDOW_HOURS + 6)).isoformat(
    timespec="seconds")

# Hosts deliberately NOT equal to the agent names: the whole defect is that these
# two axes were conflated, so a fixture reusing one set for both could not fail.
_MANI_BOXES = dict(MANIFEST, nodes=[
    {"agent": a, "host": "%s-box" % a}
    for a in ("alpha", "bravo", "echo", "zeta", "foxtrot")])


def _bparity(monkeypatch, boxes, *, fleet_complete=True, meta=None):
    monkeypatch.setattr(
        fcp, "_live_boxes",
        lambda agents_root_dir=None: (
            dict(boxes),
            meta or {"complete": True, "read_via": "authoritative", "reason": None}))
    return fcp._box_parity(_MANI_BOXES, fleet_complete=fleet_complete)


def test_live_box_missing_from_manifest_is_box_DRIFT(monkeypatch):
    """The headline requirement: a live box with no manifest node is DRIFT.

    Asserted on the drift list specifically, never on drift+info — routing an
    unmeasured live box to INFO would leave the vacuous pass fully intact while
    still 'mentioning' the box, and a combined assertion would accept that. Same
    discipline as the roster test above, for the same reason.
    """
    boxes = {"%s-box" % a: _FRESH
             for a in ("alpha", "bravo", "echo", "zeta", "foxtrot")}
    boxes["extra-box"] = _FRESH
    bp = _bparity(monkeypatch, boxes)
    assert bp["checked"] is True
    assert any("extra-box" in d and "LIVE-BOX-NOT-IN-MANIFEST" in d
               for d in bp["drift"]), (
        "an unmeasured live box must be DRIFT: %r" % (bp["drift"],))
    assert not any("extra-box" in i for i in bp["info"]), (
        "a live box must NOT be downgraded to INFO: %r" % (bp["info"],))


def test_two_boxes_one_agent_is_invisible_to_roster_parity_but_caught_here(monkeypatch):
    """THE STRUCTURAL TEST — the one that fails against the pre-fix tree.

    This replays the actual defect rather than a convenient proxy. One agent
    (alpha) occupies TWO boxes. Roster parity is handed a roster that agrees with
    the manifest PERFECTLY — because a second body contributes no new
    agent_status row — so it reports zero drift and is CORRECT to. The second box
    is nonetheless unmeasured.

    Both halves are asserted together on purpose: proving box parity fires is
    only half the claim. The other half is that the existing check could not have
    fired, which is what makes this a new axis rather than a duplicate of
    g-115-3160. If a future change lets roster parity catch this, this test
    should be revisited rather than deleted — the assertion below would then be
    recording something that stopped being true.
    """
    agents = ("alpha", "bravo", "echo", "zeta", "foxtrot")
    monkeypatch.setattr(fcp, "_has_agent_identity",
                        lambda name, agents_root_dir=None: True)
    monkeypatch.setattr(fcp, "_live_roster",
                        lambda world_dir=None: (set(agents), None))
    rp = fcp._roster_parity(_MANI_BOXES, fleet_complete=True)
    assert rp["checked"] is True
    assert rp["drift"] == [], (
        "the agent roster genuinely agrees — if this fails the fixture no longer "
        "reproduces the defect: %r" % (rp["drift"],))

    boxes = {"%s-box" % a: _FRESH for a in agents}
    boxes["alpha-box-2"] = _FRESH          # alpha's second body, same agent
    bp = _bparity(monkeypatch, boxes)
    assert any("alpha-box-2" in d for d in bp["drift"]), (
        "a second body's box is invisible to the agent axis and MUST be caught on "
        "the box axis: %r" % (bp,))


def test_stale_box_is_INFO_not_drift(monkeypatch):
    """A box that has stopped is reported, but is not a coverage gap.

    Without this the check would report every box that ever ran a Mind as an
    unmeasured node forever, and a permanently-red checker gets ignored — the
    same end state as a permanently-green one, reached from the other side.
    """
    boxes = {"%s-box" % a: _FRESH
             for a in ("alpha", "bravo", "echo", "zeta", "foxtrot")}
    boxes["retired-box"] = _STALE
    bp = _bparity(monkeypatch, boxes)
    assert bp["drift"] == [], "a stopped box must not be DRIFT: %r" % (bp["drift"],)
    assert any("retired-box" in i for i in bp["info"]), (
        "a stopped box must still be REPORTED, never silently dropped: %r" % (bp["info"],))


def test_manifest_host_with_no_carrier_is_INFO_not_drift(monkeypatch):
    """A manifest node with no carrier IS measured by the per-node sweep.

    Mirrors the roster check's symmetric INFO branch: this direction is not a
    coverage gap, it just means no Mind has run there lately.
    """
    boxes = {"%s-box" % a: _FRESH for a in ("alpha", "bravo", "echo", "zeta")}
    bp = _bparity(monkeypatch, boxes)
    assert bp["drift"] == [], "a quiet manifest node is not DRIFT: %r" % (bp["drift"],)
    assert any("foxtrot-box" in i for i in bp["info"])


def test_unparseable_carrier_timestamp_is_DRIFT_not_silently_fresh(monkeypatch):
    """A malformed stamp must fail toward the visible gap.

    _age_hours returns None rather than 0 or infinity precisely so this case
    cannot be silently classified. Defaulting either way picks a verdict from a
    value that was never read.
    """
    boxes = {"%s-box" % a: _FRESH
             for a in ("alpha", "bravo", "echo", "zeta", "foxtrot")}
    boxes["weird-box"] = "not-a-timestamp"
    bp = _bparity(monkeypatch, boxes)
    assert any("weird-box" in d and "unparseable" in d for d in bp["drift"]), (
        "an unparseable carrier must be reported as unmeasured: %r" % (bp,))


def test_incomplete_enumeration_is_NOT_a_pass(monkeypatch):
    """An enumeration that could not bound the fleet must not read as agreement.

    This is the failure mode the whole goal is about, reproduced inside the fix:
    if a blind read returned checked=True with an empty drift list, the checker
    would print a clean box line while seeing nothing. It must decline out loud.
    """
    bp = _bparity(monkeypatch, {},
                  meta={"complete": False, "read_via": "local-mirror",
                        "reason": "authoritative unavailable"})
    assert bp["checked"] is False
    assert bp["drift"] == []
    assert "authoritative unavailable" in (bp["reason"] or ""), (
        "a blind enumeration must name its cause: %r" % (bp["reason"],))


def test_healthy_fleet_produces_no_box_drift(monkeypatch):
    """POSITIVE CONTROL (guard-1220).

    Without this, every assertion above is satisfied by a _box_parity that
    reports DRIFT unconditionally — which would be a strictly worse checker than
    the silent one it replaced.
    """
    boxes = {"%s-box" % a: _FRESH
             for a in ("alpha", "bravo", "echo", "zeta", "foxtrot")}
    bp = _bparity(monkeypatch, boxes)
    assert bp["checked"] is True
    assert bp["drift"] == [], "a fully-enumerated fleet must not drift: %r" % (bp["drift"],)


def test_node_filtered_run_does_not_claim_box_parity(monkeypatch):
    """--node runs measure a subset by design; comparing it to the whole box set
    would report every unselected box as unmeasured."""
    bp = _bparity(monkeypatch, {"alpha-box": _FRESH}, fleet_complete=False)
    assert bp["checked"] is False
    assert "node-filtered" in (bp["reason"] or "")


def test_box_parity_requires_fleet_complete_keyword():
    """Same contract as _roster_parity / _is_blackout: the caller MUST state
    completeness, because this function cannot determine it and a default would
    silently restore the failure."""
    with pytest.raises(TypeError):
        fcp._box_parity(_MANI_BOXES)


def test_unmeasured_live_box_makes_the_whole_run_exit_1(monkeypatch):
    """The exit-code half, which is separable and load-bearing.

    Printing BOX DRIFT while exiting 0 would leave the vacuous pass intact for
    every automated caller that reads only the status code — which is what the
    sweep wiring reads. This is also the path that is structurally unreachable
    under pytest without the _live_boxes pin in _run_with_fleet.
    """
    boxes = {"self-box": _FRESH}
    boxes.update({"peer%d-box" % i: _FRESH for i in range(4)})
    boxes["unlisted-box"] = _FRESH
    rc, filed = _run_with_fleet(monkeypatch, set(), boxes=boxes)
    assert rc == 1, "an unmeasured live box must fail the run, not just print"


def test_healthy_fleet_with_matching_boxes_still_exits_0(monkeypatch):
    """Positive control at the run() level — the fix must not fail a clean fleet."""
    rc, filed = _run_with_fleet(monkeypatch, set())
    assert rc == 0, "a healthy fleet whose boxes all have manifest nodes must pass"
    assert filed == []


def test_age_hours_returns_None_for_garbage_not_a_number():
    """The helper's own contract, unstubbed — every classification test above
    depends on it, so without this it is never executed directly."""
    now = datetime.now()
    assert fcp._age_hours("", now) is None
    assert fcp._age_hours(None, now) is None
    assert fcp._age_hours("not-a-timestamp", now) is None
    assert fcp._age_hours((now - timedelta(hours=3)).isoformat(), now) == pytest.approx(3, abs=0.1)


# ── check (h): credential VALUE SHAPE —  ────────────────────────────
# (a) reads key NAMES only, so a key that is PRESENT but holds garbage passes every
# other dimension. Live incident 2026-07-26: a ~/.bashrc bug exported the 158-char
# ERROR TEXT of a failed wslvar call as OPENAI_API_KEY and nothing looked at it.
#
# Note MANIFEST above carries NO value_shape_expectations, so all 57 tests before
# this point exercise check (h) as a no-op — the back-compat property is asserted
# explicitly below rather than left implicit.

SHAPE_MANIFEST = dict(MANIFEST, value_shape_expectations={
    "ANTHROPIC_API_KEY": {"min_len": 32, "charset": "token"},
    "AWS_SECRET_ACCESS_KEY": {"min_len": 32, "charset": "token_punct"},
})


def _shaped(**over):
    """A healthy node whose credential values all pass their shape predicate."""
    base = {
        "cshape_ANTHROPIC_API_KEY": "ok", "clen_ANTHROPIC_API_KEY": "108",
        "dshape_ANTHROPIC_API_KEY": "ok", "dlen_ANTHROPIC_API_KEY": "108",
        "cshape_AWS_SECRET_ACCESS_KEY": "ok", "clen_AWS_SECRET_ACCESS_KEY": "40",
        "dshape_AWS_SECRET_ACCESS_KEY": "ok", "dlen_AWS_SECRET_ACCESS_KEY": "40",
    }
    base.update(over)
    return _healthy(**base)


def test_shape_spec_builds_key_min_class_triples():
    spec = fcp._shape_spec(SHAPE_MANIFEST)
    assert "ANTHROPIC_API_KEY:32:token" in spec
    assert "AWS_SECRET_ACCESS_KEY:32:token_punct" in spec


def test_shape_spec_empty_when_manifest_has_no_block():
    """Back-compat: a manifest predating this feature yields an empty spec, which
    makes the whole collector-side block inert rather than erroring."""
    assert fcp._shape_spec(MANIFEST) == ""


@pytest.mark.parametrize("bad", [
    {"min_len": 32, "charset": "regex"},        # class not in the allowlist
    {"min_len": "lots", "charset": "token"},    # min_len not an int
    {"min_len": -1, "charset": "token"},        # negative length
    {"charset": "token"},                       # min_len absent
    "not-a-dict",                               # spec is not a mapping
])
def test_shape_spec_drops_malformed_entries(bad):
    spec = fcp._shape_spec({"value_shape_expectations": {"SOME_KEY": bad}})
    assert spec == "", "malformed entry must be dropped, not passed to the shell"


def test_shape_spec_rejects_key_that_could_break_out_of_shell_quoting():
    """The spec is interpolated into a SINGLE-QUOTED string on the remote node, so a
    key carrying a quote would escape it and run as code. Injection-shaped names are
    dropped by the key regex, never quoted-and-passed."""
    for hostile in ("A'; rm -rf /; echo '", "KEY WITH SPACE", "KEY:WITH:COLON", "2LEADING_DIGIT"):
        spec = fcp._shape_spec({
            "value_shape_expectations": {hostile: {"min_len": 8, "charset": "token"}}})
        assert spec == "", "hostile key %r reached the spec" % hostile


def test_healthy_node_with_shapes_produces_no_drift():
    """Anti-vacuity: a checker that flagged unconditionally would fail here."""
    assert fcp._is_real_drift(
        fcp._check_node(NODE, _shaped(), SHAPE_MANIFEST, DEPLOY_KEYS)) == []


def test_present_but_garbage_value_is_real_drift():
    """THE incident shape: key present, name-only checks all pass, value is nonsense."""
    drift = fcp._check_node(
        NODE, _shaped(cshape_ANTHROPIC_API_KEY="charset", clen_ANTHROPIC_API_KEY="158"),
        SHAPE_MANIFEST, DEPLOY_KEYS)
    real = fcp._is_real_drift(drift)
    assert any("ANTHROPIC_API_KEY" in d and "charset" in d for d in real)
    assert any("158" in d for d in real), "length must be reported"


def test_too_short_value_is_real_drift():
    real = fcp._is_real_drift(fcp._check_node(
        NODE, _shaped(cshape_AWS_SECRET_ACCESS_KEY="too_short",
                      clen_AWS_SECRET_ACCESS_KEY="3"),
        SHAPE_MANIFEST, DEPLOY_KEYS))
    assert any("AWS_SECRET_ACCESS_KEY" in d and "too_short" in d for d in real)


def test_absent_in_daemon_lane_is_NOT_drift():
    """Regression guard for the  mechanism, measured live on cc-05
    2026-08-09: ANTHROPIC_API_KEY is set IN-PROCESS after exec, so it is absent from
    /proc/environ for the daemon's whole life while the CLI lane reads it fine.
    Flagging that reproduces 31h of false HIGH goals. Presence is check (a)'s job."""
    real = fcp._is_real_drift(fcp._check_node(
        NODE, _shaped(dshape_ANTHROPIC_API_KEY="absent", dlen_ANTHROPIC_API_KEY="0"),
        SHAPE_MANIFEST, DEPLOY_KEYS))
    assert real == [], "absent must never be drift: %r" % real


def test_declared_key_with_no_verdict_is_reported_not_silently_passed():
    """A manifest typo drops the entry from the spec. The check must SAY the key was
    unverified rather than count it as passing (guard-1760)."""
    f = _shaped()
    for k in list(f):
        if k.endswith("AWS_SECRET_ACCESS_KEY") and k.split("_")[0] in ("cshape", "dshape", "clen", "dlen"):
            f.pop(k)
    drift = fcp._check_node(NODE, f, SHAPE_MANIFEST, DEPLOY_KEYS)
    assert any("AWS_SECRET_ACCESS_KEY" in d and "unverified" in d for d in drift)
    assert fcp._is_real_drift(drift) == [], "an unverified key is INFO, not drift"


def test_unsampled_lane_reports_once_not_once_per_key():
    """When the evaluating lane never ran, one line — not N identical ones burying
    the reason the (c2) block already gave."""
    f = _shaped(cli_lane_readable="no", cli_lane_error="no_interpreter")
    drift = fcp._check_node(NODE, f, SHAPE_MANIFEST, DEPLOY_KEYS)
    unchecked = [d for d in drift if "value-shape unchecked" in d]
    assert len(unchecked) == 1, unchecked
    assert "2 key(s)" in unchecked[0]


def test_manifest_without_shape_block_emits_no_shape_output():
    """The 57 tests above use MANIFEST (no shape block); assert that is genuinely
    inert rather than accidentally passing."""
    drift = fcp._check_node(NODE, _healthy(), MANIFEST, DEPLOY_KEYS)
    assert not any("SHAPE" in d or "value-shape" in d or "unverified" in d for d in drift)


def test_collector_template_formats_with_both_substitutions():
    """The collector is a %-format template and a single stray percent kills
    collection for EVERY node. Cheap arity/escaping guard."""
    script = fcp._COLLECTOR % {"root": "'/tmp/x'", "shape_spec": "K:8:token"}
    assert "SHAPE_SPEC='K:8:token'" in script
    assert "%(" not in script


def test_real_collector_evaluates_shape_end_to_end(monkeypatch):
    """INTEGRATION PATH (sq-019), not a unit: spec -> real _COLLECTOR -> real remote
    snippet -> parsed verdict. The unit tests above drive _shape_spec and _check_node
    in isolation and would all still pass if the collector never emitted a shape line
    at all. This reproduces the 2026-07-26 incident shape — an error string exported
    over a credential key name — and asserts BOTH halves at once: the poison is
    CAUGHT, and the poison does not APPEAR."""
    poison = "wslvar: command not found - error text, not a credential"
    monkeypatch.setenv("FCP_ITEST_KEY", poison)          # _run_collector copies os.environ
    monkeypatch.setenv("FCP_ITEST_OK", "abcd-1234-EFGH")
    p, fields = _run_collector(shape_spec="FCP_ITEST_KEY:8:token FCP_ITEST_OK:8:token")
    assert p.returncode == 0, p.stderr[:500]
    assert fields.get("cshape_FCP_ITEST_KEY") == "charset"
    assert fields.get("cshape_FCP_ITEST_OK") == "ok"
    assert fields.get("clen_FCP_ITEST_OK") == "14"
    assert poison not in p.stdout


def test_drift_line_carries_verdict_and_length_but_no_value():
    """The secrets contract at the check layer: even a caller who stuffed a raw value
    into the fields dict cannot get it into a drift string."""
    f = _shaped(cshape_ANTHROPIC_API_KEY="charset", clen_ANTHROPIC_API_KEY="158")
    f["cvalue_ANTHROPIC_API_KEY"] = "sk-MUST-NEVER-APPEAR"
    for d in fcp._check_node(NODE, f, SHAPE_MANIFEST, DEPLOY_KEYS):
        assert "sk-MUST-NEVER-APPEAR" not in d


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
