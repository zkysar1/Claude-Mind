"""tree_write_fence vs Layer A, and the channel the fence speaks on ().

Two defects, measured together (zeta, cc-02, 2026-09-15, g-364-177):

  1. FALSE CONFLICT. tree-sync-check.sh (Layer A) and the fence's `record` fire
     on the SAME PostToolUse event and run concurrently. `record` hashed the
     node in ~0.1 s; Layer A then rewrote last_updated and
     last_update_trigger.session seconds later. So the session's NEXT edit to
     the node was told "Another writer landed in between" and a DIVERGED row
     went to the ledger, with no other writer anywhere.
  2. SILENT CHANNEL. The wrapper sent stdout to /dev/null and the banners went
     to stderr, which an exit-0 hook never delivers to the model (guard-1680,
     guard-6752). So neither CONFLICT nor OVER-CAP reached the only reader
     that could act on it.

The order of fixes mattered: opening the channel over (1) would have delivered
a false "re-Read and re-apply" order on routine multi-edit touches.

Four families, each against REAL code, never a re-implementation:

  A. LAYER A PIN -- run the real tree-front-matter-sync.py over every
     front-matter shape the live tree holds, and assert the fence hash does not
     move while the raw bytes DO. The fence carries a copy of Layer A's field
     list, so if Layer A starts writing another field, this fails.
  B. DISCRIMINATING CONTROLS -- the mask must hide ONLY Layer A's lines. A
     mutant that hashes nothing (or only the body) must fail here.
  C. HOOK BATCH (the goal's outcome 1) -- the real wrappers in the incident
     order, plus the positive control where a genuine foreign write between the
     two edits still reports CONFLICT and still ledgers.
  D. CHANNEL (the hermetic half of outcome 2) -- the wrapper's stdout is
     exactly one hook payload when a banner fires and EMPTY otherwise. Whether
     Claude Code DELIVERS that payload cannot be tested here. It is measured by
     a fresh-session probe and recorded on g-306-488.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _bash_helpers import BASH  # noqa: E402
import tree_write_fence as F  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent.parent
LAYER_A = SCRIPTS / "tree-front-matter-sync.py"
FENCE_SH = SCRIPTS / "tree-write-fence.sh"
SYNC_SH = SCRIPTS / "tree-sync-check.sh"

AGENT = "fenceprobe"
GOAL = "g-999-01"
SID = "11111111-2222-3333-4444-555555555555"
NODE_REL = "knowledge/tree/system/demo-node.md"
VIRTUAL = "world/" + NODE_REL

BLOCK_STALE = """---
topic: demo
last_updated: '2020-01-01'
last_update_trigger:
  type: knowledge_reconciliation
  session: 00000000-old-session
---

# Demo Node

## Pre-existing Section
original content
"""


def _world(tmp_path, in_flight=True):
    """A sandbox world + meta + agent dir; returns the env for real scripts."""
    world = tmp_path / "world"
    (world / "knowledge" / "tree" / "system").mkdir(parents=True, exist_ok=True)
    (tmp_path / "meta").mkdir(exist_ok=True)
    (tmp_path / "agent" / "session").mkdir(parents=True, exist_ok=True)
    (world / "knowledge" / "tree" / "_tree.yaml").write_text(
        "last_updated: '2020-01-01'\nnodes:\n  demo-node:\n"
        "    file: %s\n    last_updated: '2020-01-01'\n" % VIRTUAL,
        encoding="utf-8")
    if in_flight:
        # Layer A fills trigger.source from the agent's in-flight goal, so a
        # row makes the source-INSERT path fire too.
        rows = world / "team-state" / "agents"
        rows.mkdir(parents=True, exist_ok=True)
        (rows / (AGENT + ".yaml")).write_text(
            "in_flight:\n  goal_id: %s\n" % GOAL, encoding="utf-8")
    env = dict(os.environ)
    env.update(MIND_WORLD=str(world), MIND_META=str(tmp_path / "meta"),
               MIND_AGENT_DIR=str(tmp_path / "agent"),
               STORAGE_BACKEND="local")          # guard-955: never the real store
    env.pop("MIND_AGENT", None)
    env.pop("MIND_SID", None)
    return world / NODE_REL, env


def _layer_a(node, env):
    """The real Layer A, invoked the way tree-sync-check.sh invokes it."""
    e = dict(env, MIND_SID=SID, MIND_AGENT=AGENT)
    r = subprocess.run([sys.executable, str(LAYER_A), "--file", str(node),
                        "--virtual-path", VIRTUAL],
                       capture_output=True, text=True, env=e, timeout=60)
    assert r.returncode == 0, r.stderr
    return r


# ----------------------------------------------------------- A. Layer A pin

SHAPES = {
    # block trigger, stale session, no source: replace session, INSERT source,
    # replace last_updated (1,578 live nodes are block form)
    "block": BLOCK_STALE,
    # no trigger and no last_updated: Layer A appends both (77 / 73 live)
    "absent": "---\ntopic: demo\n---\n\n# Demo\nbody\n",
    # inline trigger: Layer A leaves it alone, only last_updated moves (98 live)
    "inline": ("---\ntopic: demo\nlast_updated: '2020-01-01'\n"
               "last_update_trigger: {type: x, session: old}\n---\n\n# Demo\nbody\n"),
    # no front matter at all: Layer A PREPENDS a block and a "\n\n" separator
    # (91 live). Two leading-newline counts, neither of them 2: a fixture that
    # happened to start with exactly "\n\n" matched the separator by accident
    # and let a no-lstrip mutant survive (measured while writing this file).
    "no_front_matter": "# Demo\nbody with no front matter\n",
    "no_front_matter_leading_blank": "\n# Demo\nbody with no front matter\n",
    # a blank line before the closing ---: Layer A's insert consumes it (119 live)
    "trailing_blank": "---\ntopic: demo\n\n---\n\n# Demo\nbody\n",
    # CRLF file: Layer A preserves CRLF on every line it writes (22 live)
    "crlf": BLOCK_STALE.replace("\n", "\r\n"),
    # mixed endings: Layer A rewrites the whole file as CRLF (2 live)
    "mixed": BLOCK_STALE.replace("\n", "\r\n", 3),
}


def _pin(tmp_path, text):
    node, env = _world(tmp_path)
    node.write_bytes(text.encode("utf-8"))
    before_raw, before = node.read_bytes(), F.fence_hash(node)
    _layer_a(node, env)
    return node, before_raw, before


def test_layer_a_rewrites_do_not_move_the_fence_hash(tmp_path):
    for name, text in SHAPES.items():
        node, before_raw, before = _pin(tmp_path / name, text)
        after_raw = node.read_bytes()
        # The premise, or this test proves nothing: Layer A really wrote.
        assert after_raw != before_raw, "%s: Layer A did not write" % name
        assert F.fence_hash(node) == before, (
            "%s: Layer A's rewrite moved the fence hash -- the session's next "
            "edit to this node would get a false CONFLICT. Did Layer A start "
            "writing a field _mask_layer_a does not leave out?\n--- after ---\n%s"
            % (name, after_raw.decode("utf-8", "replace")))


def test_pin_exercises_every_layer_a_write(tmp_path):
    """The block shape must hit the replace AND insert paths, or the pin above
    would pass while never covering them (guard-1587: control the zero)."""
    node, _, _ = _pin(tmp_path, BLOCK_STALE)
    text = node.read_text(encoding="utf-8")
    assert "last_updated: '2020-01-01'" not in text      # replaced
    assert "session: %s" % SID in text                   # replaced
    assert "source: %s" % GOAL in text                   # inserted
    assert "00000000-old-session" not in text


# ------------------------------------------------ B. discriminating controls


def _variant(tmp_path, name, text):
    p = tmp_path / name / NODE_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))
    return F.fence_hash(p)


def test_mask_hides_only_layer_a_lines(tmp_path):
    base = _variant(tmp_path, "base", BLOCK_STALE)
    same = {
        "last_updated value": BLOCK_STALE.replace("2020-01-01", "2031-12-31"),
        "session value": BLOCK_STALE.replace("00000000-old-session", "other"),
        "source added": BLOCK_STALE.replace(
            "  session: 00000000-old-session\n",
            "  session: 00000000-old-session\n  source: g-1-1\n"),
    }
    for label, text in same.items():
        assert _variant(tmp_path, label.replace(" ", "_"), text) == base, label
    moved = {
        "body edit": BLOCK_STALE.replace("original content", "changed content"),
        "topic edit": BLOCK_STALE.replace("topic: demo", "topic: other"),
        "trigger type edit": BLOCK_STALE.replace("knowledge_reconciliation",
                                                 "direct_correction"),
        "other trigger child": BLOCK_STALE.replace(
            "  type:", "  note: kept\n  type:"),
        "indented last_updated is not Layer A's": BLOCK_STALE.replace(
            "  type:", "  last_updated: x\n  type:"),
        "LF to CRLF": BLOCK_STALE.replace("\n", "\r\n"),
    }
    for label, text in moved.items():
        assert _variant(tmp_path, label.replace(" ", "_"), text) != base, (
            "%s did not move the fence hash -- the mask hides real work" % label)


def test_legacy_raw_baseline_is_not_compared(tmp_path):
    """A pre- baseline hashed raw bytes. Comparing it to a masked hash
    would report DIVERGED on every node once -- over the now-open channel."""
    node, _ = _world(tmp_path)
    node.write_text(BLOCK_STALE, encoding="utf-8")
    store = tmp_path / "baselines"
    F.record(node, store)
    entry = next(Path(store).glob("*.json"))
    rec = json.loads(entry.read_text(encoding="utf-8"))
    assert rec["scheme"] == F.HASH_SCHEME
    del rec["scheme"]
    entry.write_text(json.dumps(rec), encoding="utf-8")
    node.write_text(BLOCK_STALE + "\nmore\n", encoding="utf-8")
    assert F.check(node, store)["verdict"] == "no_baseline"


# -------------------------------------------------------------- C. hook batch


def _hook(script, node, env, *args, agent=AGENT):
    """A real wrapper, fed the hook JSON Claude Code sends on stdin."""
    e = dict(env)
    if agent:
        e["MIND_AGENT"] = agent   # Read/Edit hooks get no inject; see 
    payload = json.dumps({"session_id": SID, "tool_name": "Edit",
                          "tool_input": {"file_path": str(node)}})
    return subprocess.Popen([BASH, str(script), *args], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=e), payload


def _run(script, node, env, *args, agent=AGENT):
    p, payload = _hook(script, node, env, *args, agent=agent)
    out, err = p.communicate(payload, timeout=120)
    assert p.returncode == 0, err                       # advisory: never blocks
    return out.strip(), err


def _quiet(result):
    """No payload for Claude Code to read, and no fence banner. (stderr may
    carry unrelated noise, e.g. _paths.sh warning about the fixture agent.)"""
    out, err = result
    return out == "" and "[tree-write-fence]" not in err


def _edit1(tmp_path, concurrent=False):
    """Read, then Edit 1 and its PostToolUse batch. Returns (node, env, raw)."""
    node, env = _world(tmp_path, in_flight=False)
    node.write_text(BLOCK_STALE, encoding="utf-8")
    _run(FENCE_SH, node, env, "record")                 # PostToolUse[Read]
    assert _quiet(_run(FENCE_SH, node, env, "check"))       # PreToolUse[Edit] 1
    node.write_text(BLOCK_STALE + "\n## Added by edit 1\nnew\n", encoding="utf-8")
    raw = node.read_bytes()
    if concurrent:
        procs = [_hook(FENCE_SH, node, env, "record"),
                 _hook(SYNC_SH, node, env, agent=None)]
        for p, payload in procs:
            p.stdin.write(payload)
            p.stdin.close()
        for p, _ in procs:
            p.wait(timeout=120)
    else:
        # The incident order: record hashes first, Layer A lands after it.
        _run(FENCE_SH, node, env, "record")
        _run(SYNC_SH, node, env, agent=None)             # PostToolUse: no inject
    return node, env, raw


def _ledger(tmp_path):
    p = tmp_path / "agent" / "session" / "tree-write-conflicts.jsonl"
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


def test_second_edit_after_layer_a_is_not_a_conflict(tmp_path):
    node, env, raw = _edit1(tmp_path)
    # The premise: Layer A really rewrote the node inside Edit 1's batch, AFTER
    # the fence recorded. Without it, this test would pass against the defect.
    assert node.read_bytes() != raw, "Layer A never ran -- the test is vacuous"
    assert "session: %s" % SID in node.read_text(encoding="utf-8")
    out, err = _run(FENCE_SH, node, env, "check")       # PreToolUse[Edit] 2
    assert "CONFLICT" not in err, err
    assert out == "", "a false CONFLICT would now reach the model: %s" % out
    assert _ledger(tmp_path) == [], "false DIVERGED row in the ledger"


def test_second_edit_is_clean_whichever_hook_finishes_first(tmp_path):
    """The real batch runs both hooks at once. The sequential test above pins
    the losing order; this one pins that the order no longer matters."""
    node, env, raw = _edit1(tmp_path, concurrent=True)
    assert node.read_bytes() != raw, "Layer A never ran -- the test is vacuous"
    assert _quiet(_run(FENCE_SH, node, env, "check"))
    assert _ledger(tmp_path) == []


def test_positive_control_a_foreign_write_still_conflicts(tmp_path):
    """guard-3351: narrowing what alerts is the silent direction. A genuine
    second writer between the two edits must still be caught, loudly, on every
    channel."""
    node, env, _ = _edit1(tmp_path)
    node.write_text(node.read_text(encoding="utf-8")
                    + "\n## Section by ANOTHER writer\ntheir work\n", encoding="utf-8")
    out, err = _run(FENCE_SH, node, env, "check")
    assert "CONFLICT" in err and str(node) in err
    payload = json.loads(out)
    assert "CONFLICT" in payload["hookSpecificOutput"]["additionalContext"]
    rows = _ledger(tmp_path)
    assert len(rows) == 1 and json.loads(rows[0])["verdict"] == "DIVERGED"


# ---------------------------------------------------------------- D. channel


def test_check_payload_is_the_measured_pretooluse_shape(tmp_path):
    """All four fields, each pinned separately: narrowing them produces a gate
    that fires and communicates nothing (guard-1680; g-115-3598 owns narrowing)."""
    node, env = _world(tmp_path, in_flight=False)
    node.write_text(BLOCK_STALE, encoding="utf-8")
    _run(FENCE_SH, node, env, "record")
    node.write_text(BLOCK_STALE + "\nforeign\n", encoding="utf-8")
    out, err = _run(FENCE_SH, node, env, "check")
    assert len(out.splitlines()) == 1, "stdout must be ONE payload: %r" % out
    p = json.loads(out)
    spec = p["hookSpecificOutput"]
    assert spec["hookEventName"] == "PreToolUse"
    assert spec["permissionDecision"] == "allow"         # advisory, never a deny
    msg = spec["additionalContext"]
    assert "CONFLICT" in msg and str(node) in msg
    assert spec["permissionDecisionReason"] == msg
    assert p["systemMessage"] == msg
    assert msg in err                                    # the human copy


def test_record_payload_is_posttooluse_and_never_a_decision(tmp_path):
    node, env = _world(tmp_path, in_flight=False)
    node.write_text("---\ntopic: big\n---\n\n" + "x" * 80_000, encoding="utf-8")
    out, err = _run(FENCE_SH, node, env, "record")
    p = json.loads(out)
    spec = p["hookSpecificOutput"]
    assert spec["hookEventName"] == "PostToolUse"
    assert "permissionDecision" not in spec              # not a PreToolUse field
    assert "OVER-CAP" in spec["additionalContext"]
    assert p["systemMessage"] == spec["additionalContext"]
    assert "OVER-CAP" in err


def test_stdout_is_empty_when_nothing_fires(tmp_path):
    """Claude Code parses PreToolUse stdout as a decision. Anything printed on a
    quiet path -- the old JSON verdict, a stray debug line -- would be read as
    one, so the quiet path must print nothing at all."""
    node, env = _world(tmp_path, in_flight=False)
    node.write_text(BLOCK_STALE, encoding="utf-8")
    assert _quiet(_run(FENCE_SH, node, env, "record"))       # under cap
    assert _quiet(_run(FENCE_SH, node, env, "check"))        # clean
    other = tmp_path / "not-a-node.md"
    other.write_text("x", encoding="utf-8")
    assert _quiet(_run(FENCE_SH, other, env, "check"))       # out of scope
