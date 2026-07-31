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

def _run_collector(extra_path=None):
    """Execute the real _COLLECTOR against this repo. Returns parsed key=value dict."""
    import shlex
    script = fcp._COLLECTOR % {"root": shlex.quote(str(CORE_SCRIPTS.parent.parent))}
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


def _run_with_fleet(monkeypatch, unreachable_agents, blackout_cfg=None, peers=4,
                    nodes_filter=None, self_unreachable=False):
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

    def fake_collect(node, timeout=45):
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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
