#!/usr/bin/env python3
"""Audit for the DROPPED-FIELD class ( / hypothesis
2026-08-15_dropped-field-class-is-rare-not-systemic).

THE SHAPE. A producer builds a record carrying key K. An intermediate
hand-written dict literal rebuilds that record FIELD BY FIELD and omits K. A
downstream consumer then reads K off the rebuilt dict and always sees None.
Nothing errors; the feature behind K is simply inert. Canonical instance:
completed-not-committed-sweep.classify_stranded dropped `draft`, so the
g-115-6219 draft fork shipped inert behind 6 green tests.

A LIVE INSTANCE requires ALL THREE parts (the hypothesis' own definition):
  (1) a producer demonstrably sets K,
  (2) a field-by-field rebuild literal omits K,
  (3) a downstream consumer reads K off the rebuilt dict.
Parts 1 and 2 alone are a DELIBERATE PROJECTION -- dropping a field on purpose
is normal and correct. Part (3) is what separates a defect from a design, and
it is applied MECHANICALLY here, never by judgement (confound gate (c)).

WHY AST AND NOT GREP. The defect is the ABSENCE of a key from one literal, so
any file-level grep for the key name finds the PRODUCER's mention and reads as
"present". Measured while writing this: grepping the pre-fix sweep for `draft`
returns 4 hits and looks clean; the defect is invisible without attributing
each site to its owning literal.
"""
import argparse
import ast
import json
import pathlib
import sys

MIN_GET_VALUES = 3   # a literal is a "rebuild" only if >=3 values are src reads
SHAPE_OVERLAP = 3    # absolute floor on shared keys for same-shape-family
MIN_SHAPE_RATIO = 0.6  # ...AND the overlap must cover >=60% of the rebuilt literal


def _str_key(k):
    return k.value if isinstance(k, ast.Constant) and isinstance(k.value, str) else None


def _src_of_value(v):
    """Return the source variable name if v is `src.get("k")` or `src["k"]`."""
    if isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) \
            and v.func.attr == "get" and isinstance(v.func.value, ast.Name):
        return v.func.value.id
    if isinstance(v, ast.Subscript) and isinstance(v.value, ast.Name):
        return v.value.id
    # `(src.get("k") or "")[:80]` and friends -- unwrap one layer
    for sub in ast.walk(v):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) \
                and sub.func.attr == "get" and isinstance(sub.func.value, ast.Name):
            return sub.func.value.id
    return None


def _enclosing_func(tree, node):
    best = None
    for f in ast.walk(tree):
        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if getattr(f, "lineno", 0) <= node.lineno <= getattr(f, "end_lineno", 0):
                if best is None or f.lineno > best.lineno:
                    best = f
    return best.name if best else "<module>"


def audit_source(text, path_label):
    """Return (findings, rebuild_count, projection_dropped, parsed_ok).

    `parsed_ok` is False when the file could not be parsed. It is separate from
    the finding count on purpose: this audit's whole product is a ZERO, and a
    zero is only readable against an honest denominator. A file that silently
    fails to parse would otherwise be indistinguishable from one that parsed
    clean and had nothing -- an absent input is unmeasurable, never empty.
    Print the coverage beside the count and announce the skip.
    (tree: empty-reference-corpus-inversion)
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [], 0, 0, False

    all_dicts = [n for n in ast.walk(tree) if isinstance(n, ast.Dict)]
    # Every `X.get("k")` read anywhere in the file -> consumer evidence (part 3).
    # PART (3) IS THE WHOLE DISCRIMINATOR, so it is scoped, not file-wide.
    # A file-wide "does anything read K" test counts reads off the SOURCE as if
    # they were reads off the REBUILT dict -- and the source obviously still has
    # K, that is the premise. Measured: the file-wide form reported 52 live
    # instances over 262 rebuilds in core/scripts (e.g. guardrail-check rec/rule,
    # where the read is off the source in the same function). That is the
    # CORRECTED direction, so a sloppy predicate here does not merely add noise,
    # it inverts the verdict.
    # Scoped test: the read must occur in a DIFFERENT function from the rebuild.
    # A rebuild and a read in the same body is the author handling one record in
    # one place; the defect class is a record that TRAVELS to another consumer
    # and arrives missing a field. This is mechanical (confound gate (c)) --
    # no judgement about intent.
    # container_key: id(dict-literal) -> the key it is stored under, e.g.
    #   entry["pull_request"] = {...}   ->  "pull_request"
    container_key = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Dict):
            for t in n.targets:
                if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant) \
                        and isinstance(t.slice.value, str):
                    container_key[id(n.value)] = t.slice.value

    # rebound_from: container key -> set(variable names bound from it), e.g.
    #   pr = entry.get("pull_request") or {}   ->  "pull_request" -> {"pr"}
    rebound_from = {}
    for n in ast.walk(tree):
        if not isinstance(n, ast.Assign) or len(n.targets) != 1:
            continue
        t = n.targets[0]
        if not isinstance(t, ast.Name):
            continue
        for sub in ast.walk(n.value):
            key = None
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) \
                    and sub.func.attr == "get" and sub.args \
                    and isinstance(sub.args[0], ast.Constant) \
                    and isinstance(sub.args[0].value, str):
                key = sub.args[0].value
            elif isinstance(sub, ast.Subscript) and isinstance(sub.slice, ast.Constant) \
                    and isinstance(sub.slice.value, str):
                key = sub.slice.value
            if key:
                rebound_from.setdefault(key, set()).add(t.id)

    # reads_by_var: dropped-key -> set(variable names it is read off)
    # BOTH ACCESS SHAPES, deliberately. An earlier revision counted only
    # `var.get("k")`, which is a probe that assumes one path shape: it returns a
    # negative true for the shape it tested and false for the question it was
    # asked ("does anything read K off the rebuilt dict?"). A consumer written
    # `pr["draft"]` was invisible, and this gate's error direction is the
    # dangerous one -- it suppresses findings, so its slop hides defects rather
    # than inventing them (the mirror of the gate-(1)/(3) slop documented above,
    # which inflates). Censused before widening, per the node's own remedy:
    # adding subscript reads across core/scripts contributes 5000 additional
    # read-edges and leaves live=0 unchanged, so the  zero survives a
    # materially weaker part-(3) predicate. (tree: probe-path-shape-assumption)
    reads_by_var = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == "get" and n.args \
                and isinstance(n.func.value, ast.Name) \
                and isinstance(n.args[0], ast.Constant) \
                and isinstance(n.args[0].value, str):
            reads_by_var.setdefault(n.args[0].value, set()).add(n.func.value.id)
        elif isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) \
                and isinstance(n.slice, ast.Constant) \
                and isinstance(n.slice.value, str):
            reads_by_var.setdefault(n.slice.value, set()).add(n.value.id)

    consumer_reads = {}   # key -> set(function names reading it)  [diagnostic only]
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == "get" and n.args:
            k = n.args[0]
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                consumer_reads.setdefault(k.value, set()).add(_enclosing_func(tree, n))

    findings, rebuilds, projections = [], 0, 0
    for d in all_dicts:
        keys = [_str_key(k) for k in d.keys]
        if any(k is None for k in keys) or not keys:
            continue
        srcs = [_src_of_value(v) for v in d.values]
        named = [s for s in srcs if s]
        if len(named) < MIN_GET_VALUES:
            continue
        # dominant single source
        src = max(set(named), key=named.count)
        if named.count(src) < MIN_GET_VALUES:
            continue
        rebuilds += 1
        rebuilt = set(keys)

        # (1) PRODUCER keys: any OTHER dict literal in this file of the same
        # shape-family (>=SHAPE_OVERLAP shared keys) is a producer of this record.
        producer_keys = set()
        for o in all_dicts:
            if o is d:
                continue
            okeys = {_str_key(k) for k in o.keys}
            okeys.discard(None)
            # SHAPE-FAMILY TEST. A bare ">=2 shared keys" union was measured
            # (first run of this audit, pre-fix sweep) to report 5 live
            # instances in ONE file by bleeding `number`/`state`/`url` out of
            # the pull_request dicts into an unrelated `goal` rebuild. Generic
            # key names are common across unrelated records, so absolute
            # overlap alone cannot establish that two dicts describe the SAME
            # record. Require the overlap to also cover a MAJORITY of the
            # rebuilt literal -- that is what makes "these are the same record"
            # a claim about the record rather than about vocabulary.
            shared = okeys & rebuilt
            if len(shared) >= SHAPE_OVERLAP and len(shared) >= MIN_SHAPE_RATIO * len(rebuilt):
                producer_keys |= okeys
        missing = producer_keys - rebuilt
        if not missing:
            continue
        # (3) CONSUMER test -- mechanical, not judgement (confound gate (c)).
        here = _enclosing_func(tree, d)
        # TRUE PART (3): the read must be off the REBUILT dict, established by
        # DATAFLOW, not by the "different function" proxy an earlier revision
        # used. That proxy over-matched in the CORRECTED direction -- measured:
        # it reported 43 live, and the highest-prior candidate
        # (completed-not-committed-sweep:869, merge_commit_sha) hand-verified
        # FALSE, because all four of its consumers read off the raw probe
        # record `r`/`p`, never off entry["pull_request"].
        #
        # The real chain, from the canonical instance:
        #   classify_stranded:  entry["pull_request"] = { ...rebuild... }
        #   _file_investigate:  pr = entry.get("pull_request") or {}
        #                       pr.get("draft")
        # So: take the CONTAINER KEY this literal is stored under, find every
        # variable bound from that same container key elsewhere, and require the
        # dropped key to be read off one of THOSE variables.
        ckey = container_key.get(id(d))
        if not ckey:
            continue          # not stored under a named key -> cannot establish travel
        receivers = rebound_from.get(ckey, set())
        if not receivers:
            continue
        live = sorted(k for k in missing
                      if (reads_by_var.get(k, set()) & receivers))
        if not live:
            projections += len(missing)
            continue
        findings.append({
            "file": path_label,
            "line": d.lineno,
            "function": here,
            "source_var": src,
            "rebuilt_keys": sorted(rebuilt),
            "dropped_but_consumed": live,
        })
    return findings, rebuilds, projections, True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*", help="files to audit (default: core/scripts/*.py)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.paths:
        files = [pathlib.Path(p) for p in a.paths]
    else:
        root = pathlib.Path(__file__).resolve().parent
        files = sorted(root.glob("*.py")) + sorted(root.glob("gates/*.py"))

    findings, rebuilds, projections, parsed = [], 0, 0, 0
    unreadable, unparseable = [], []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            unreadable.append("%s: %s" % (f, exc))
            continue
        fi, rb, pj, ok = audit_source(text, str(f))
        if not ok:
            unparseable.append(str(f))
            continue
        parsed += 1
        findings += fi
        rebuilds += rb
        projections += pj

    out = {
        "files_offered": len(files),
        "files_parsed": parsed,                      # the REAL denominator
        "files_unparseable": unparseable,            # announced, never folded into 0
        "files_unreadable": unreadable,
        "field_by_field_rebuilds_found": rebuilds,   # confound gate (b)
        "projections_dropped_at_consumer_test": projections,  # confound gate (c)
        "live_instances": len(findings),
        "findings": findings,
    }
    if a.json:
        print(json.dumps(out, indent=2))
    else:
        print("files_offered=%d files_parsed=%d unparseable=%d unreadable=%d "
              "rebuilds_found=%d projections_dropped=%d live=%d"
              % (len(files), parsed, len(unparseable), len(unreadable),
                 rebuilds, projections, len(findings)))
        for u in unparseable:
            print("  SKIPPED (unparseable, NOT counted as clean): %s" % u)
        for u in unreadable:
            print("  SKIPPED (unreadable, NOT counted as clean): %s" % u)
        for f in findings:
            print("  LIVE %s:%d [%s] src=%s dropped_and_consumed=%s"
                  % (f["file"], f["line"], f["function"], f["source_var"],
                     ",".join(f["dropped_but_consumed"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
