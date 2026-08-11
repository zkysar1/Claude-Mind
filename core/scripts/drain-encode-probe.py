#!/usr/bin/env python3
"""Probe whether a temp artifact's intended EFFECT already landed in its store.

WHY THIS EXISTS (g-115-3089, near-miss in g-001-345)
----------------------------------------------------
`/drain-temp` Phase 2 step 3 routes each temp doc to a store OR to DISCARD.
The DISCARD branch had no scripted check: it rested entirely on the LLM reading
the artifact and judging it spent. That is the one branch where a wrong call is
silent and effectively irreversible -- the doc lands in temp/drained/ and nothing
re-examines it.

The motivating near-miss: `poig_batch.json` (100 tree nodes staged for poignancy
scoring) read exactly like spent compute scratch. Probing two target nodes showed
`poignancy=None` -- it was UNAPPLIED work, the staged input for a still-pending
recurring goal. It survived only because someone probed the target store by hand.

DESIGN: PROBE THE EFFECT, NOT THE EXISTENCE
-------------------------------------------
The question is never "does something with this name exist" -- it is "did the
change this payload describes actually land". For an ADD-shaped payload those
coincide (the effect IS the record). For a BATCH/UPDATE payload they do not: the
target node exists either way, and only the FIELD distinguishes applied from
staged. Reading existence there produces a confident false `encoded`, which is
exactly the discard this script is meant to block.

Corollary trap, called out in the goal: `tree-find-node.sh` returns a PROJECTION
(depth/file/key/node_type/score/summary). A field like `poignancy` is absent from
that output for EVERY node, applied or not -- so reading the projection as
"field missing" would be a false confirm in the other direction. The tree lane
here reads the node's own front matter, never the projection.

VERDICTS
--------
  encoded  -- the effect is present in the store. DISCARD is safe.
  absent   -- the payload's effect is NOT in the store. DISCARD MUST BE BLOCKED;
              this is unapplied work.
  unknown  -- shape unrecognised, store unreadable, or the effect field could not
              be identified. Falls back to LLM judgement.

FAIL-OPEN BY CONSTRUCTION. Every failure path returns `unknown`, never `absent`.
An `absent` blocks a drain, so a probe bug that emitted `absent` would wedge the
drain lane; a probe bug that emits `unknown` merely restores today's behaviour.
This is the ARMING asymmetry in the checker-input-assumption-defects tree node:
arming an inert checker inverts the harm rather than fixing it.

SHAPE POPULATIONS ARE MEASURED, NOT ASSUMED (echo, cc-03, 2026-08-01, n=366 json
+ 27 md in one live temp root). Recorded so a later reader can tell a lane that
finds nothing from a lane that scans nothing:
    goal        82     reasoning_bank  40     guardrail  21
    experience  15     trace_md        26     tree_batch  1
    tree single-object (key+summary):  0   <-- the goal's prose proposed this
                                             shape; it has NO live population.
The `tree_batch` discriminator requires `key` AND a tree co-field: 4 arrays in
the corpus carry a bare `key`, and 3 of them are e-mail alert records where the
token is coincidental. Keying on `key` alone would misclassify 75% of that lane.
"""

import argparse
import json
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS = os.path.join(REPO, "core", "scripts")
TIMEOUT = 120

GOAL_ID_RE = re.compile(r"\bg-\d{1,3}-\d{1,4}\b")

# Element keys that are batch INPUTS (identify/locate the target) rather than
# the EFFECT the batch applies. Anything outside this set is a candidate effect.
_TREE_STRUCTURAL = {
    "key", "file", "path", "line_count", "est_tokens", "depth",
    "node_type", "score", "recommended_action", "refresh_sections",
}
# At least one of these must accompany `key` for an array to be a tree batch.
_TREE_CO_FIELDS = {
    "file", "summary", "poignancy", "times_helpful", "retrieval_count",
    "line_count", "est_tokens", "recommended_action",
}


def _sh(name, *args):
    """Build an argv for a core/scripts wrapper, guard-580 / guard-581 safe.

    Never a bare ``["bash", ...]``: on win32 that argv[0] resolves through
    System32 to the WSL launcher and can hang forever. ``bash_cmd`` prepends the
    resolved interpreter and passes the script path as a POSIX string, since
    ``str(WindowsPath)`` yields backslashes that bash strips as escapes.
    """
    if SCRIPTS not in sys.path:
        sys.path.insert(0, SCRIPTS)
    from _runtime_bash import bash_cmd
    return bash_cmd(os.path.join(SCRIPTS, name), *args)


def _run(args):
    """Run a wrapper and return parsed JSON, or None on any failure."""
    try:
        p = subprocess.run(
            args, capture_output=True, text=True, timeout=TIMEOUT, cwd=REPO
        )
    except Exception:
        return None
    if p.returncode != 0:
        return None
    out = (p.stdout or "").strip()
    if not out:
        return None
    i = out.find("[")
    j = out.find("{")
    start = min(x for x in (i, j) if x >= 0) if (i >= 0 or j >= 0) else -1
    if start < 0:
        return None
    try:
        return json.loads(out[start:])
    except Exception:
        return None


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def classify(path):
    """Infer artifact type from SHAPE. Returns (type, payload)."""
    if path.endswith(".md"):
        try:
            body = open(path, encoding="utf-8", errors="replace").read(8000)
        except Exception:
            return "unreadable", None
        m = GOAL_ID_RE.search(body)
        return ("trace_md", m.group(0)) if m else ("unknown_shape", None)

    try:
        d = json.load(open(path, encoding="utf-8", errors="replace"))
    except Exception:
        return "unreadable", None

    if isinstance(d, list):
        if d and isinstance(d[0], dict):
            ks = set(d[0].keys())
            if "key" in ks and (ks & _TREE_CO_FIELDS):
                return "tree_batch", d
        return "query_capture", d

    if not isinstance(d, dict):
        return "unknown_shape", None

    ks = set(d.keys())
    # Most specific first -- a guardrail payload also carries `category`.
    if {"title", "type", "category", "content"} <= ks:
        return "reasoning_bank", d
    if "rule" in ks and ({"category", "trigger_condition"} & ks):
        return "guardrail", d
    if {"title", "priority", "participants"} <= ks:
        return "goal", d
    if "id" in ks and ({"verbatim_anchors", "content_path"} & ks):
        return "experience", d
    return "unknown_shape", None


def probe_reasoning_bank(payload):
    title = _norm(payload.get("title"))
    if not title:
        return "unknown", "payload has no title to match"
    rows = _run(_sh("reasoning-bank-read.sh", "--active"))
    if not isinstance(rows, list):
        return "unknown", "reasoning-bank-read unreadable"
    for r in rows:
        if _norm(r.get("title")) == title:
            return "encoded", "rb entry %s carries this exact title" % r.get("id")
    return "absent", "no active rb entry matches title (searched %d)" % len(rows)


def probe_guardrail(payload):
    rule = _norm(payload.get("rule"))
    if not rule:
        return "unknown", "payload has no rule to match"
    probe = rule[:120]
    rows = _run(_sh("guardrails-read.sh", "--active"))
    if not isinstance(rows, list):
        return "unknown", "guardrails-read unreadable"
    for r in rows:
        if probe and probe in _norm(r.get("rule")):
            return "encoded", "guardrail %s carries this rule text" % r.get("id")
    return "absent", "no active guardrail matches rule (searched %d)" % len(rows)


def probe_goal(payload):
    title = (payload.get("title") or "").strip()
    if not title:
        return "unknown", "payload has no title to match"
    # --title-contains is a substring filter; a long title can drift after
    # filing (reword-to-clear-duplication-gate), so match on a stable prefix.
    needle = title[:60]
    rows = _run(
        _sh("aspirations-query.sh", "--title-contains", needle)
    )
    if not isinstance(rows, list):
        return "unknown", "aspirations-query unreadable"
    if rows:
        return "encoded", "goal %s matches title prefix" % rows[0].get("goal_id")
    return "absent", "no goal matches title prefix %r" % needle


def probe_experience(payload):
    eid = (payload.get("id") or "").strip()
    if not eid:
        return "unknown", "payload has no id to match"
    rows = _run(
        _sh("experience-read.sh", "--id", eid)
    )
    # `None` is _run's UNREADABLE signal, not an empty result: it is returned for
    # a subprocess exception, a non-zero rc, empty stdout, and a JSON parse
    # failure alike. Mapping it to `absent` would declare unapplied work during a
    # daemon outage and block the discard of every experience-shaped file --
    # violating this module's fail-open contract. Only a well-formed EMPTY result
    # is evidence of absence. (Found by fresh-eyes review of this file; the
    # sibling probe_trace_md below always had this right, and the unreadable-store
    # test covered only the reasoning_bank lane, so the divergence was invisible.)
    if rows is None:
        return "unknown", "experience-read unreadable for %s" % eid
    if isinstance(rows, list) and not rows:
        return "absent", "experience-read returned empty list for %s" % eid
    return "encoded", "experience record %s exists" % eid


def probe_trace_md(goal_id):
    rows = _run(
        _sh("experience-read.sh", "--goal", goal_id)
    )
    if rows is None:
        return "unknown", "experience-read --goal unreadable for %s" % goal_id
    if isinstance(rows, list) and not rows:
        return "absent", "no experience record references %s" % goal_id
    return "encoded", "an experience record references %s" % goal_id


def _resolve_node_path(node_file):
    """Resolve a tree-node path, honouring the external world/ and meta/ prefixes.

    Tree batches store `file` as a VIRTUAL path (`world/knowledge/tree/...`).
    `world/` is an EXTERNAL, user-configured path -- joining it to the repo root
    yields a path that never exists, so every node reads as unreadable and the
    whole lane degrades to `unknown` (measured on distill.json before this fix).
    `.claude/rules/path-resolution.md` requires routing through the configured
    resolver rather than deriving the location.
    """
    if os.path.isabs(node_file):
        return node_file
    if node_file.startswith(("world/", "meta/")):
        try:
            if SCRIPTS not in sys.path:
                sys.path.insert(0, SCRIPTS)
            from _paths import resolve_file_path
            return str(resolve_file_path(node_file))
        except Exception:
            return None
    return os.path.join(REPO, node_file)


def _read_front_matter(node_file):
    """Parse a tree node's own front matter. Never the tree-find-node projection."""
    p = _resolve_node_path(node_file)
    if not p or not os.path.exists(p):
        return None
    try:
        text = open(p, encoding="utf-8", errors="replace").read(20000)
    except Exception:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    fm = {}
    for line in text[3:end].splitlines():
        if ":" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    return fm


_MIN_FIELD_VOCAB = 5
_VOCAB_CACHE = {}


def _tree_fm_vocabulary():
    """Count how many tree nodes actually USE each front-matter key.

    A batch key that merely shares a name with something is not an observable
    effect. Measured 2026-08-01 over 1309 nodes carrying front matter:
    `poignancy` appears on 55, while `utility_ratio`, `times_helpful` and
    `retrieval_count` appear on exactly 1 each. A boolean "is this a real key"
    test passes all four; only a THRESHOLD separates a field the tree genuinely
    carries from one that occurs once. The gap 1 -> 55 is why the cutoff is 5.

    This matters because it decides a FALSE ABSENT. `distill.json` is a
    distillation RECOMMENDATION whose retrieval_count/times_helpful/utility_ratio
    are telemetry INPUTS, never written into a node -- so "field absent on every
    target" means the probe cannot see this effect, not that the work is
    unapplied. Reporting `absent` there would block that discard forever.
    """
    if _VOCAB_CACHE:
        return _VOCAB_CACHE
    import collections
    import glob
    try:
        if SCRIPTS not in sys.path:
            sys.path.insert(0, SCRIPTS)
        from _paths import WORLD_DIR
        root = os.path.join(str(WORLD_DIR), "knowledge", "tree")
    except Exception:
        return _VOCAB_CACHE
    counts = collections.Counter()
    for p in glob.glob(os.path.join(root, "**", "*.md"), recursive=True):
        try:
            t = open(p, encoding="utf-8", errors="replace").read(3000)
        except Exception:
            continue
        if not t.startswith("---"):
            continue
        e = t.find("\n---", 3)
        if e < 0:
            continue
        for line in t[3:e].splitlines():
            if ":" in line and not line.startswith((" ", "\t", "#")):
                counts[line.split(":", 1)[0].strip()] += 1
    _VOCAB_CACHE.update(counts)
    return _VOCAB_CACHE


def probe_tree_batch(rows):
    """Probe the FIELD the batch applies, on the batch's own target nodes."""
    if not rows:
        return "unknown", "empty batch"
    candidates = [
        k for k in rows[0].keys()
        if k not in _TREE_STRUCTURAL and not isinstance(rows[0].get(k), (dict, list))
    ]
    if not candidates:
        return "unknown", "no candidate effect field outside the structural set"
    vocab = _tree_fm_vocabulary()
    effect_fields = [k for k in candidates if vocab.get(k, 0) >= _MIN_FIELD_VOCAB]
    if not effect_fields:
        return "unknown", (
            "no candidate field %s is an established tree front-matter key "
            "(<%d nodes use it) -- this batch's effect is not observable in "
            "front matter" % (candidates[:4], _MIN_FIELD_VOCAB)
        )
    checked = applied = 0
    for el in rows[:5]:
        nf = el.get("file")
        if not nf:
            continue
        fm = _read_front_matter(nf)
        if fm is None:
            continue
        checked += 1
        if any(f in fm and fm[f] not in ("", "null", "None") for f in effect_fields):
            applied += 1
    if checked == 0:
        return "unknown", "no target node front matter was readable"
    if applied == checked:
        return "encoded", "effect field(s) %s present on %d/%d sampled nodes" % (
            effect_fields[:3], applied, checked)
    if applied == 0:
        return "absent", "effect field(s) %s absent on all %d sampled nodes" % (
            effect_fields[:3], checked)
    return "unknown", "effect field(s) present on %d of %d sampled nodes (partial)" % (
        applied, checked)


_PROBES = {
    "reasoning_bank": probe_reasoning_bank,
    "guardrail": probe_guardrail,
    "goal": probe_goal,
    "experience": probe_experience,
    "tree_batch": probe_tree_batch,
    "trace_md": probe_trace_md,
}


def probe_file(path):
    result = {"file": path, "artifact_type": None, "verdict": "unknown", "evidence": ""}
    if not os.path.exists(path):
        result["evidence"] = "file does not exist"
        return result
    try:
        atype, payload = classify(path)
        result["artifact_type"] = atype
        fn = _PROBES.get(atype)
        if fn is None:
            result["evidence"] = "no probe for shape %r -- LLM judgement" % atype
            return result
        verdict, evidence = fn(payload)
        result["verdict"] = verdict
        result["evidence"] = evidence
    except Exception as e:  # fail-open: never emit `absent` from a bug
        result["verdict"] = "unknown"
        result["evidence"] = "probe error (fail-open): %s: %s" % (type(e).__name__, e)
    return result


def main():
    ap = argparse.ArgumentParser(
        description="Probe whether a temp artifact's effect already landed in its store."
    )
    ap.add_argument("files", nargs="+", help="temp file path(s) to probe")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args()

    results = [probe_file(f) for f in args.files]
    if args.json:
        print(json.dumps(results if len(results) > 1 else results[0], indent=2))
    else:
        for r in results:
            print("%-9s %-15s %s  -- %s" % (
                r["verdict"], r["artifact_type"], os.path.basename(r["file"]),
                r["evidence"]))
    # Exit 0 always: this is advisory. The caller reads the verdict, not the rc.
    return 0


if __name__ == "__main__":
    sys.exit(main())
