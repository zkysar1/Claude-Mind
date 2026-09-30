"""_confidence_ledger.py — truth-event capture for DECLARED CONFIDENCE ().

A confidence value nobody scores against outcomes is a vibe. The hypothesis lane
already scores itself honestly (resolution criteria → measured outcomes → per-category
accuracy). Tree-node / reasoning-bank / guardrail `confidence` is self-declared at
encode time and never joined to what later happened to the claim, so the data for a
calibration curve evaporates at the moment it exists. This module is the join point:
when an entry's claim MEETS EVIDENCE, append one row pairing the verdict with the
confidence the entry was carrying AT THAT MOMENT.

Shape and contract follow `_override_helpers.py` deliberately (same store class, same
audit posture): build a dict, `locked_append_jsonl`, and NEVER raise — a failed audit
write must not break the caller, so failures print a stderr WARN rather than
propagating. Silent loss is the one outcome worse than a noisy one.

MEASURED CAVEAT — READ BEFORE WIRING A NEW SURFACE (g-306-399, 2026-09-01, alpha cc-08).
`declared_confidence` is NULL for almost every reasoning-bank and guardrail entry,
because those stores do not carry the field:

    tree nodes (via _tree.yaml)   530 / 1551   34%
    reasoning_bank                 63 / 9466   0.67%
    guardrails                      3 / 5434   0.06%

Positive-controlled (`"id"` present on 9466/9466 and 5434/5434 lines respectively), so
those are real zeros, not broken greps. The consequence is structural and is NOT a bug
in this module: the only instrumented truth-event surface (`adjudication-lane.py`) has
`SCOPE_STORES = ("reasoning_bank", "guardrails")` — precisely the two stores without the
field — while the store that HAS the field (tree) has no truth-event surface at all. So
rows captured from the adjudication lane are still worth having (verdict + evidence are
real), but a calibration table built from them alone would have a null x-axis. Anyone
producing that table must bucket by `declared_confidence is not None` FIRST and report
the null count, or the denominator is a fiction.
"""

import datetime as _dt
import json as _json
import os as _os

from _paths import WORLD_DIR
from _fileops import locked_append_jsonl

LEDGER_NAME = "confidence-calibration-ledger.jsonl"

# Closed vocabulary. Consumers group by this field, so an open set would make the
# calibration table ungroupable; anything unrecognised is normalised to "unknown"
# rather than dropped, because hiding a row hides the very mixture we are measuring.
VERDICTS = ("survived", "refuted", "revised", "unknown")

# Stores an entry_id can live in. "tree" carries confidence in the _tree.yaml INDEX,
# not in the node's own front matter — a distinction that cost a wrong resolver once.
STORES = ("tree", "reasoning_bank", "guardrails")

_UNSET = object()


def _now():
    return _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _judge_from_env():
    """Judge identity, resolved CALLER-SIDE ( /  rules).

    Duplicated rather than imported: the only implementations live in the
    skill-evaluate CLI/daemon twins, which are entry points and not shared modules,
    and `core/BOUNDARY.md` forbids reaching across the layer. The RULES are what
    matter and they are copied exactly — in particular `CLAUDE_CODE_SUBAGENT_MODEL`
    is NEVER read: it names the SUBAGENT model while scoring runs on the MAIN loop,
    and a confidently-wrong judge id corrupts exactly the cross-model comparison the
    field exists to enable (guard-1925).
    """
    model = (_os.environ.get("MIND_JUDGE_MODEL") or "").strip() or "unknown"
    if (_os.environ.get("CLAUDECODE") or "").strip():
        harness = "claude-code"
    elif any((_os.environ.get(k) or "").strip()
             for k in ("ZAKCODE_MODEL", "ZAKCODE_SESSION")):
        harness = "zakcode"
    else:
        harness = "unknown"
    return model, harness


def _confidence_from_jsonl(path, entry_id):
    """Scan a JSONL store for one id and return its `confidence`, or None.

    Line-at-a-time on purpose: reasoning-bank.jsonl is ~27MB and this runs on a
    truth event, not in a loop. Only the matching line is parsed.
    """
    needle = '"%s"' % entry_id
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                if needle not in line:
                    continue
                try:
                    rec = _json.loads(line)
                except ValueError:
                    continue
                if rec.get("id") != entry_id:
                    continue
                val = rec.get("confidence")
                return float(val) if isinstance(val, (int, float)) else None
    except OSError:
        return None
    return None


def _tree_index_lookup(index_path, entry_id):
    """Find a node in the tree INDEX: (found, confidence).

    `found` is True/False, or None when the index could not be read. It exists
    because a null confidence has two causes a calibration reader must keep apart
    (g-306-553): a node that declares no confidence, and a key that names no node
    at all (a typo, a rename, a deleted node).

    Deliberately a targeted scan rather than a yaml.safe_load of the whole index:
    the index is ~1.9MB and only one node's value is wanted. A node is a mapping
    key directly under the top-level `nodes:`, and `confidence:` is read only at
    that node's own field indent. Matching the key anywhere with a bare prefix test
    also "found" top-level and field names: `nodes` returned the first node's 0.7
    (measured 2026-09-29). `domain_confidence:` is explicitly NOT matched: it is a
    different measurement, and conflating the two silently shifts the curve.
    """
    try:
        with open(index_path, "r", encoding="utf-8") as fh:
            in_nodes = False
            node_indent = field_indent = None
            in_block = False
            for line in fh:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                indent = len(line) - len(line.lstrip())
                if in_block:
                    if indent <= node_indent:
                        return True, None    # left the node's block
                    if field_indent is None:
                        field_indent = indent
                    if indent == field_indent and stripped.startswith("confidence:"):
                        try:
                            return True, float(stripped.split(":", 1)[1].strip())
                        except ValueError:
                            return True, None
                    continue
                if indent == 0:
                    in_nodes = stripped == "nodes:"
                    continue
                if in_nodes:
                    if node_indent is None:
                        node_indent = indent
                    if indent == node_indent and stripped == "%s:" % entry_id:
                        in_block = True
    except OSError:
        return None, None
    return in_block, None


def resolve_declared_confidence(entry_id, store, *, world_dir=None):
    """The entry's declared confidence right now, or None when it carries none.

    None is the HONEST and common answer — see the module docstring's measured
    caveat. Callers must record it as null rather than substituting a default; a
    defaulted confidence is indistinguishable from a real one downstream and would
    silently manufacture the calibration data this ledger exists to measure.
    """
    root = world_dir if world_dir is not None else WORLD_DIR
    if not entry_id or store not in STORES:
        return None
    if store == "tree":
        return _tree_index_lookup(
            root / "knowledge" / "tree" / "_tree.yaml", entry_id)[1]
    fname = "reasoning-bank.jsonl" if store == "reasoning_bank" else "guardrails.jsonl"
    return _confidence_from_jsonl(root / fname, entry_id)


def record_truth_event(entry_id, store, verdict, *, source,
                       evidence_ref=None, declared_confidence=_UNSET,
                       world_dir=None, extra=None):
    """Append one (entry_id, declared_confidence, verdict, evidence_ref, date) row.

    `declared_confidence` is resolved from the store when not supplied, because the
    value must be the one carried AT EVENT TIME — a later reader cannot recover it
    once the entry is edited. Pass it explicitly only when the caller already read it.

    `source` names the surface that produced the verdict (e.g. "adjudication-lane"),
    so a consumer can weight or exclude a surface without guessing its provenance.

    Never raises (contract shared with `_override_helpers.audit_bulk_override`).
    """
    if not entry_id or not source:
        return
    if declared_confidence is _UNSET:
        try:
            declared_confidence = resolve_declared_confidence(
                entry_id, store, world_dir=world_dir)
        except Exception:
            declared_confidence = None
    judge_model, harness = _judge_from_env()
    record = {
        "ts": _now(),
        "entry_id": entry_id,
        "store": store if store in STORES else "unknown",
        "declared_confidence": declared_confidence,
        "verdict": verdict if verdict in VERDICTS else "unknown",
        "evidence_ref": evidence_ref,
        "source": source,
        "agent": _os.environ.get("MIND_AGENT", "") or None,
        "session_id": _os.environ.get("MIND_SID", "") or None,
        "judge_model": judge_model,
        "harness": harness,
    }
    if extra:
        record["extra"] = extra
    root = world_dir if world_dir is not None else WORLD_DIR
    try:
        locked_append_jsonl(root / LEDGER_NAME, record)
    except Exception as e:
        import sys as _sys
        print("[_confidence_ledger] WARN: ledger write failed: %s" % e,
              file=_sys.stderr)


# --- Hypothesis-resolution surface () ------------------------------
# A pipeline record names the tree node whose CLAIM it tests in `tests_node`
# ({"key": <_tree.yaml key>, "stance": "supports"|"challenges"}, written at
# formation); the resolver may add `node_verdict` in the resolve merge. Schema:
# core/config/conventions/pipeline.md § Tested-Node Link.

NODE_STANCES = ("supports", "challenges")
HYPOTHESIS_SOURCE = "hypothesis-resolution"


def hypothesis_truth_event(record):
    """Map a resolved pipeline record to (node_key, verdict, extra), or None.

    None means the record says nothing about a tree node's claim: no usable
    `tests_node`, or an outcome that settled nothing (EXPIRED, UNRESOLVABLE, null).

    An explicit `node_verdict` wins on either outcome: it is the one judgement
    made about the NODE's claim rather than about the prediction.

    Without it, only CONFIRMED is mapped, matched EXACTLY (guard-654: 'CORRECTED'
    contains 'correct'), and only through the declared stance. A prediction that
    supports the claim and came true leaves it standing; one that challenges it and
    came true refutes it. Mapping without the stance would score a confirmed
    challenge as a survival, which is the inverted polarity g-115-9063 found on the
    adjudication surface.

    CORRECTED is never mapped (guard-2728): a prediction can miss its criterion
    while the mechanism it rested on held, so a bare CORRECTED says nothing
    reliable about the node. The row is kept as 'unknown', not dropped, so the
    table can report how often resolvers left the node unjudged.
    """
    if not isinstance(record, dict):
        return None
    link = record.get("tests_node")
    if not isinstance(link, dict):
        return None
    key = link.get("key")
    if not isinstance(key, str) or not key.strip():
        return None
    outcome = record.get("outcome")
    if outcome not in ("CONFIRMED", "CORRECTED"):
        return None
    stance = link.get("stance")
    stance = stance if stance in NODE_STANCES else None
    explicit = record.get("node_verdict")
    if explicit is not None:
        if explicit in VERDICTS:
            verdict, basis = explicit, "node_verdict"
        else:
            verdict, basis = "unknown", "node_verdict_invalid"
    elif outcome == "CONFIRMED":
        if stance == "supports":
            verdict, basis = "survived", "confirmed_supports"
        elif stance == "challenges":
            verdict, basis = "refuted", "confirmed_challenges"
        else:
            verdict, basis = "unknown", "stance_missing"
    else:
        verdict, basis = "unknown", "corrected_unjudged"
    return key.strip(), verdict, {"outcome": outcome, "stance": stance,
                                  "verdict_basis": basis}


def capture_resolution_response(response_text, *, world_dir=None):
    """Append the calibration row for a hypothesis resolution that tests a node.

    The pipeline wrappers call this with the daemon's 200 body, and only after a
    move INTO `resolved` (or an add at stage=resolved) succeeded. So the row is
    written once the resolution is on disk, never where the verdict is computed
    (the guardrail-retire placement lesson). It runs caller-side, so judge_model
    and harness describe the resolver and not the daemon process.

    Reads `record` ONLY and never falls through to the whole response (guard-4578),
    so an error-shaped body can never be taken for a record. The node's
    confidence comes from the tree INDEX at this moment, and `node_in_index`
    records whether the key named a node at all. Never raises.
    """
    import sys as _sys
    try:
        try:
            resp = _json.loads(response_text)
        except ValueError:
            return
        rec = resp.get("record") if isinstance(resp, dict) else None
        if not isinstance(rec, dict) or rec.get("stage") != "resolved":
            return
        if not rec.get("tests_node"):
            return
        event = hypothesis_truth_event(rec)
        if event is None:
            if rec.get("outcome") in ("CONFIRMED", "CORRECTED"):
                print("[_confidence_ledger] WARN: %s carries an unusable "
                      "tests_node %r; no calibration row written (expected "
                      "{\"key\": <tree node key>, \"stance\": \"supports\"|"
                      "\"challenges\"})" % (rec.get("id"), rec.get("tests_node")),
                      file=_sys.stderr)
            return
        key, verdict, extra = event
        root = world_dir if world_dir is not None else WORLD_DIR
        found, confidence = _tree_index_lookup(
            root / "knowledge" / "tree" / "_tree.yaml", key)
        extra["node_in_index"] = found
        record_truth_event(key, "tree", verdict, source=HYPOTHESIS_SOURCE,
                           evidence_ref=rec.get("id"),
                           declared_confidence=confidence,
                           world_dir=world_dir, extra=extra)
    except Exception as e:
        print("[_confidence_ledger] WARN: hypothesis capture failed: %s" % e,
              file=_sys.stderr)
