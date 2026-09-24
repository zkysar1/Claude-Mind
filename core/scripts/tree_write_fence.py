"""tree_write_fence — lost-update detection for knowledge-tree .md bodies ().

THE GAP THIS CLOSES
-------------------
Every other shared store in this framework fences its writes:

  * JSONL stores  -> daemon read-modify-write with 412/write_conflict (rb-2639, rb-3280)
  * meta YAML     -> locked_rmw + force_fresh (g-115-3177)
  * _tree.yaml    -> _fileops.locked_modify_yaml (the tree INDEX)

The knowledge-tree .md NODE BODIES -- the store this framework exists to
produce -- are written with the raw Edit/Write tool. Measured 2026-07-28 on
both writers:

    grep -cE 'write_conflict|locked_rmw|force_fresh|IfMatch|if_match' \
        core/scripts/tree.py mind_api/src/world/tree_write.py
    -> 0 and 0

No lock, no version check, no conflict signal. Two agents that both Read a node
and then both write it produce a silent last-write-wins loss.

Observed live twice in 48h. In the 2026-07-27T20:56 instance a ~64-line encoding
section was verified on disk, then vanished ~4 minutes later under another
agent's write; it was noticed ONLY because the harness happened to emit a "file
was modified" reminder. Nothing failed: no error, no exit code, no conflict. The
verify gate would have agreed the encoding happened, because it checks that the
agent DID the encoding, not that it SURVIVED.

WHY DETECT AND NOT PREVENT
--------------------------
A fail-closed PreToolUse gate on Edit is the obvious fix and is the wrong one
here: this framework runs an autonomous loop, and a per-edit hard deny that
false-positives wedges it. That risk is already called out in-tree
(aspirations-precheck Phase 0-pre6: "Does NOT hard-block the Edit tool (a
fail-closed per-edit gate can wedge the loop)"). Routing .md bodies through the
daemon RMW is the strongest option and was also rejected for now: rb-3080
records the tree-INDEX optimistic-concurrency path chronically conflicting (9
straight write_conflicts), so adopting that mechanism for the far-more-numerous
node bodies would trade a silent-loss failure for a loud-stall failure.

So this fences by DETECTION: it is loud, durable, and cannot wedge the loop.
The goal's own acceptance is an OR -- "either prevents a concurrent overwrite or
detects and reports one loudly".

Loud here means THREE channels, one per reader. Under `--hook`, a structured
hook payload on stdout (hookSpecificOutput.additionalContext + systemMessage)
is the only one that reaches the MODEL: an exit-0 hook's stderr is stored as a
hook_success transcript record and never enters context (guard-1680,
guard-6752). The stderr banner is for a human at the terminal. The appended
JSONL ledger record is the durable one -- guard-772 measured that stderr alone
vanishes inside a backgrounded subprocess. Until g-306-488 only the last two
existed (the wrapper sent stdout to /dev/null), so neither CONFLICT nor
OVER-CAP could reach the model.

CONTRACT
--------
    record <path>   after a Read (and after our own write) -> store sha256
    check  <path>   before a write -> compare live sha256 against the baseline

A DIVERGED verdict means: the file changed since this session last observed it,
so any edit computed from that observation may silently drop the other writer's
work. The caller re-Reads and re-applies.

Fail-open everywhere. A fence that breaks must never be the outage.
"""
import hashlib
import json
import os
import re
import sys
import threading
from datetime import datetime
from pathlib import Path

# Only node BODIES under the knowledge tree. _tree.yaml (the index) already goes
# through _fileops.locked_modify_yaml and is deliberately out of scope.
_TREE_MARKER = os.path.join("knowledge", "tree")

# --- over-cap write advisory ( remedy, 2026-08-19) -----------------
# PINNED COPIES of the read-cap detector's constants. Importing tree.py here
# would put a multi-thousand-line module (plus yaml) on the hook critical path
# the wrapper's first line declares latency-budgeted; reading tree.yaml per
# hook fire costs a yaml parse for two numbers that change ~never. Drift is
# caught by test_tree_write_fence_overcap.py, which asserts equality against
# tree.py CHARS_PER_TOKEN and tree.yaml pruning.distill_token_cap (the same
# pin-test pattern test_goal_claim_commit_gate.py uses for STALE_GRACE).
#
# The advisory fires at the FULL cap, not the detector's 0.8 proactive
# trigger, deliberately: the sweep owns the proactive band, and a per-touch
# nag that fires on every node in the 80-100% band becomes wallpaper. Every
# banner this emits means "the file you just touched is unreadable NOW".
#
# Banner-only, no ledger row: the DIVERGED path ledgers because a conflict is
# otherwise invisible after the moment passes, but an over-cap node is
# durably visible already — tree-read.sh --distill-candidates lists it
# tier-0 until it is folded. Writing over_cap rows into a file named
# tree-write-conflicts.jsonl would also hand its consumers a second record
# shape for no new information.
CHARS_PER_TOKEN = 2.3     # == tree.py CHARS_PER_TOKEN (measured floor, id-dense)
READ_CAP_TOKENS = 25000   # == tree.yaml pruning.distill_token_cap


def est_tokens(path):
    """~tree.py's estimate via os.path.getsize: bytes / CHARS_PER_TOKEN.

    tree.py's _analyze_node_body divides CHAR count by the same ratio. bytes >=
    chars (UTF-8), so this stat-only estimate runs equal-or-HIGH vs the
    detector's — the fail-safe direction, and a one-directional guarantee:
    every node the sweep lists at full cap, this warns on too. The reverse can
    over-warn slightly on heavily non-ASCII nodes; that is accepted in exchange
    for keeping a file read off the hook path. 0 when unreadable.
    """
    try:
        return int(os.path.getsize(path) / CHARS_PER_TOKEN)
    except Exception:
        return 0


def _now():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def in_scope(path):
    """True for a knowledge-tree node body (.md under knowledge/tree/)."""
    if not path:
        return False
    p = str(path).replace("\\", "/")
    return p.endswith(".md") and (_TREE_MARKER.replace("\\", "/") in p)


# --- What the hash leaves out: Layer A's lines () --------------------
# tree-sync-check.sh (Layer A, via tree-front-matter-sync.py) and this fence's
# `record` fire on the SAME PostToolUse[Edit|Write|MultiEdit] event, and one
# event's hooks run concurrently (), so settings.json order guarantees
# nothing. `record` hashes in ~0.1 s; Layer A rewrites the node's mechanical
# front matter seconds later. A raw-bytes baseline therefore went stale on the
# session's OWN write, and its next edit was told "Another writer landed in
# between" -- with a DIVERGED ledger row -- when there was none ().
#
# The fence asks whether an edit computed from an old view can drop someone's
# WORK. Layer A regenerates its lines on every write, so they are never work
# another writer can lose, and the hash leaves them out: root `last_updated:`,
# and the `session:` / `source:` children of a block-form
# `last_update_trigger:` (plus that parent line when nothing else is left under
# it, since Layer A inserts the whole block when it is absent). The body, the
# topic, the trigger type and every other key still diverge.
#
# Read the way Layer A reads (its _read_file_lf): LF-normalized, so a mixed-
# ending node Layer A rewrites to all-CRLF does not diverge, while the ending
# STYLE stays in the hash, so another writer's LF<->CRLF rewrite still does.
# Blank front-matter lines are ignored (Layer A's end-of-block insert consumes
# a trailing blank), and a node with no front matter hashes from its first
# non-blank line (Layer A's first write prepends a block). Each shape is live:
# census of 1,844 nodes on 2026-09-24 -- 119 trailing-blank, 91 without front
# matter, 2 mixed-ending. This is a second copy of Layer A's field list, so
# test_tree_write_fence_layer_a.py runs the REAL tree-front-matter-sync.py over
# each shape and fails if the hash moves.
_FRONT_MATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---", re.DOTALL)  # == Layer A's
_TRIGGER_BLOCK_RE = re.compile(r"last_update_trigger:[ \t]*$")
_TRIGGER_CHILD_RE = re.compile(r"[ \t]+(?:session|source):")
# Stored with every baseline. A baseline without it hashed raw bytes and is not
# comparable, so check() treats it as never observed rather than DIVERGED.
HASH_SCHEME = "layer-a-masked-v1"


def _mask_layer_a(lf_text):
    """The node text minus every line Layer A may write (see the note above)."""
    m = _FRONT_MATTER_RE.match(lf_text)
    if not m:
        return lf_text.lstrip("\n")
    kept = []
    parent = None          # index in `kept` of an open last_update_trigger: block
    has_child = False
    for line in m.group(1).split("\n"):
        if not line.strip():
            continue
        if line[0] not in " \t":           # column 0 closes any open block
            if parent is not None and not has_child:
                del kept[parent]
            parent = None
            if line.startswith("last_updated:"):
                continue
            if _TRIGGER_BLOCK_RE.match(line):
                parent, has_child = len(kept), False
        elif parent is not None:
            if _TRIGGER_CHILD_RE.match(line):
                continue
            has_child = True
        kept.append(line)
    if parent is not None and not has_child:
        del kept[parent]
    body = lf_text[m.end():]
    if not kept:
        return body.lstrip("\n")
    return "\n".join(kept) + "\n---" + body


def fence_hash(path):
    """sha256 of what the fence compares, or None when unreadable/absent.

    Read in bytes and LF-normalized by hand, never in text mode: a text-mode
    read folds CRLF on Windows (tree-front-matter-sync.py carries the same
    warning), and the ending style must stay in the hash.
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except Exception:
        return None
    h = hashlib.sha256()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        h.update(raw)      # Layer A cannot decode it either, so never rewrites it
        return h.hexdigest()
    h.update(b"crlf\n" if "\r\n" in text else b"lf\n")
    h.update(_mask_layer_a(text.replace("\r\n", "\n")).encode("utf-8"))
    return h.hexdigest()


def _key(path):
    """Stable identity for a node across cwd differences."""
    try:
        return str(Path(path).resolve())
    except Exception:
        return str(path)


# --------------------------------------------------------------------------
# Baseline storage: ONE FILE PER NODE, never a shared map.
#
# The first cut of this module kept every baseline in a single JSON object and
# did load -> mutate -> atomic-replace. `os.replace` makes the WRITE atomic; it
# does NOT make the read-modify-write atomic. Hooks fire concurrently (a
# PostToolUse[Read] record can overlap a PostToolUse[Edit] record), so two
# invocations both read the same map, each add their own entry, and the second
# replace silently discards the first.
#
# Measured on the shipped code: 20 nodes recorded concurrently persisted
# **1** baseline. The 19 lost ones degrade to `no_baseline`, which is
# fail-open -- so the fence would have gone quiet on 95% of nodes while
# reporting nothing wrong. That is the exact silent-lost-update class this
# module exists to detect, in the detector itself (found by /fresh-eyes-code
# 20 minutes after it shipped).
#
# The fix is subtraction, not a lock: with one file per node there is no shared
# mutable structure to race on. Each record is an independent atomic replace,
# concurrency-safe by construction, and cheaper (no full-map load per hook
# fire). A lock would have preserved the shared map and added a dependency to
# a hot hook path to defend a structure that did not need to exist.
# --------------------------------------------------------------------------


def _entry_file(store_dir, key):
    return Path(store_dir) / (hashlib.sha1(key.encode("utf-8")).hexdigest()[:16] + ".json")


def load_baselines(store_dir):
    """All baselines as {key: record}. Inspection/testing; not the hot path."""
    out = {}
    try:
        for f in Path(store_dir).glob("*.json"):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    rec = json.load(fh)
                if isinstance(rec, dict) and rec.get("key"):
                    out[rec["key"]] = rec
            except Exception:
                continue  # one corrupt entry must not blind the rest
    except Exception:
        return {}
    return out


def save_baseline(store_dir, key, rec):
    """Atomically write ONE node's baseline. No shared state, no lock needed."""
    try:
        Path(store_dir).mkdir(parents=True, exist_ok=True)
        target = _entry_file(store_dir, key)
        # Unique tmp name per WRITER, not per target. The original code used one
        # shared `<store>.tmp`, so concurrent writers raced on the tmp itself and
        # os.replace hit FileNotFoundError when a peer moved it first. pid alone
        # is insufficient -- threads in one process share it -- so include the
        # thread id: two concurrent records of the SAME node must not collide.
        tmp = "%s.%d.%d.tmp" % (target, os.getpid(), threading.get_ident())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, sort_keys=True)
        os.replace(tmp, target)
        return True
    except Exception:
        return False


def read_baseline(store_dir, key):
    try:
        with open(_entry_file(store_dir, key), "r", encoding="utf-8") as f:
            rec = json.load(f)
        return rec if isinstance(rec, dict) else None
    except Exception:
        return None


def record(path, store_path):
    """Snapshot the current hash as this session's baseline for `path`."""
    if not in_scope(path):
        return {"op": "record", "scoped": False, "path": str(path)}
    h = fence_hash(path)
    if h is None:
        return {"op": "record", "scoped": True, "recorded": False,
                "reason": "unreadable", "path": str(path)}
    k = _key(path)
    rec = {"key": k, "sha256": h, "scheme": HASH_SCHEME, "at": _now()}
    out = {"op": "record", "scoped": True,
           "recorded": save_baseline(store_path, k, rec),
           "sha256": h[:12], "path": str(path)}
    # Over-cap advisory (): record fires PostToolUse on BOTH Read and
    # Edit/Write of a node, and both moments are action-relevant — a Read of an
    # over-cap node came back TRUNCATED (rb-2077), and a write to one grew a
    # file no future Read returns whole. The banner rides the same verdict so
    # main() can emit it (this function stays pure/testable).
    et = est_tokens(path)
    if et > READ_CAP_TOKENS:
        out["over_cap"] = {"est_tokens": et, "cap": READ_CAP_TOKENS}
    return out


def check(path, store_path):
    """Compare the live file against this session's baseline.

    Verdicts:
      not_scoped  -- not a tree node body
      no_baseline -- never observed this session, or observed under an older
                     HASH_SCHEME (nothing comparable; allow)
      unreadable  -- cannot hash right now (allow)
      clean       -- unchanged since last observation
      DIVERGED    -- changed underneath us; an edit from the old view may drop work
    """
    if not in_scope(path):
        return {"op": "check", "verdict": "not_scoped", "path": str(path)}
    rec = read_baseline(store_path, _key(path))
    if not rec or rec.get("scheme") != HASH_SCHEME:
        return {"op": "check", "verdict": "no_baseline", "path": str(path)}
    live = fence_hash(path)
    if live is None:
        return {"op": "check", "verdict": "unreadable", "path": str(path)}
    if live == rec.get("sha256"):
        return {"op": "check", "verdict": "clean", "path": str(path)}
    return {
        "op": "check",
        "verdict": "DIVERGED",
        "path": str(path),
        "baseline_sha256": str(rec.get("sha256"))[:12],
        "live_sha256": live[:12],
        "observed_at": rec.get("at"),
    }


def append_ledger(ledger_path, record_obj):
    """Durable half of 'loud' (guard-772: stderr alone vanishes under backgrounding).

    Single-line JSON under PIPE_BUF is single-write atomic in O_APPEND mode --
    the same tradeoff retrieve.py:2048 documents for its trace, and appropriate
    for the same reason: this is observability-grade, not durable-state-grade.
    """
    try:
        Path(ledger_path).parent.mkdir(parents=True, exist_ok=True)
        with open(ledger_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record_obj, ensure_ascii=True) + "\n")
        return True
    except Exception:
        return False


def _default_paths():
    """Resolve the per-agent baseline + ledger locations. Fail-open to None."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _paths import AGENT_DIR  # type: ignore
        base = Path(AGENT_DIR) / "session"
        # DIRECTORY, not a file: one baseline per node (see the storage note
        # above -- a single shared map lost 19 of 20 concurrent records).
        return base / "tree-write-baselines", base / "tree-write-conflicts.jsonl"
    except Exception:
        return None, None


# --- The model-facing payload () -------------------------------------
# From a non-blocking hook, only a structured payload on stdout reaches the
# model (guard-1680). `record` is wired to PostToolUse and `check` to PreToolUse
# (tree-write-fence.sh), and each payload names its own event. The PreToolUse
# shape is the four-field one measured to arrive (; the same payload
# as trailing-echo-exit-gate.py and pre-edit-context-gate.sh). Do not narrow it
# here -- that question is . It carries permissionDecision "allow"
# because PreToolUse stdout is read as a decision. The edit is never blocked,
# though allow also skips any permission prompt for that one Edit (the same
# trade pre-edit-context-gate makes), and the scope (knowledge/tree .md) never
# holds the constitutional anchor.
_HOOK_EVENT = {"record": "PostToolUse", "check": "PreToolUse"}


def hook_payload(op, message):
    """The hook output that carries `message` into the model's context."""
    spec = {"hookEventName": _HOOK_EVENT[op], "additionalContext": message}
    if op == "check":
        spec["permissionDecision"] = "allow"
        spec["permissionDecisionReason"] = message
    return {"hookSpecificOutput": spec, "systemMessage": message}


def main(argv):
    if len(argv) < 3:
        print("usage: tree_write_fence.py {record|check} <path> [--hook]",
              file=sys.stderr)
        return 0
    op, path = argv[1], argv[2]
    # --hook (the wrapper always passes it): stdout carries ONLY the hook
    # payload, and only when a banner fires. Without it (CLI, tests): the JSON
    # verdict, as before.
    hook = "--hook" in argv[3:]
    if op not in _HOOK_EVENT:
        return 0
    store, ledger = _default_paths()
    if store is None:
        return 0  # no agent bound -> nothing to fence against

    banner = None
    if op == "record":
        out = record(path, store)
        oc = out.get("over_cap")
        if oc:
            # No ledger — the node stays durably visible in
            # tree-read.sh --distill-candidates until folded.
            banner = (
                "[tree-write-fence] ⚠ OVER-CAP: %s is ~%s est_tokens (Read cap "
                "~%s). Reads of this node come back TRUNCATED and appends deepen "
                "a file no one can read whole. You touched it — fold it: apply "
                "the archive+keep-newest rollup (tree-read.sh --distill-candidates "
                "lists it tier-0), or claim its fold goal before adding content."
                % (out["path"], "{:,}".format(oc["est_tokens"]),
                   "{:,}".format(oc["cap"])))
    else:
        out = check(path, store)
        if out.get("verdict") == "DIVERGED":
            out["agent"] = os.environ.get("MIND_AGENT", "unknown")
            out["ts"] = _now()
            append_ledger(ledger, out)
            banner = (
                "[tree-write-fence] ⚠ CONFLICT: %s changed on disk since this "
                "session read it (baseline %s @ %s -> live %s). Another writer "
                "landed in between. An edit computed from the old view can "
                "SILENTLY drop their work -- re-Read the file and re-apply your "
                "change before writing. Logged to %s"
                % (out["path"], out["baseline_sha256"],
                   out.get("observed_at"), out["live_sha256"], ledger))

    if hook:
        if banner:
            print(json.dumps(hook_payload(op, banner)))
    else:
        print(json.dumps(out))
    if banner:
        print(banner, file=sys.stderr)  # the human at the terminal, never the model
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception:
        sys.exit(0)  # fail-open: the fence must never be the outage
