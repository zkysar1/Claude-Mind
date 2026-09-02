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


def _confidence_from_tree_index(index_path, entry_id):
    """Read a node's confidence from the tree INDEX.

    Deliberately a targeted scan rather than a yaml.safe_load of the whole index:
    the index is ~1.7MB and only one node's value is wanted. The node's block opens
    with `<key>:` at some indent and its fields are indented further; `confidence:`
    is taken from the first such block, and `domain_confidence:` is explicitly NOT
    matched (it is a different measurement and conflating them silently shifts the
    curve).
    """
    try:
        with open(index_path, "r", encoding="utf-8") as fh:
            in_block = False
            block_indent = None
            for line in fh:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                indent = len(line) - len(line.lstrip())
                if stripped.startswith("%s:" % entry_id):
                    in_block, block_indent = True, indent
                    continue
                if in_block:
                    if indent <= block_indent:
                        return None          # left the node's block
                    if stripped.startswith("confidence:"):
                        try:
                            return float(stripped.split(":", 1)[1].strip())
                        except ValueError:
                            return None
    except OSError:
        return None
    return None


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
        return _confidence_from_tree_index(
            root / "knowledge" / "tree" / "_tree.yaml", entry_id)
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
