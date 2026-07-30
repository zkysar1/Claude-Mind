#!/usr/bin/env python3
"""Two-way diff of a mirrored pair of sets, with extraction positive-controls.

Forged from gap-036 (g-115-3876). Companion script for the
`diff-mirrored-sets` skill.

WHY THIS EXISTS
---------------
When two sets are supposed to mirror each other -- a machine condition and the
human-readable list that documents it, a registry and the reality it indexes, an
index and a filesystem -- an author diffs them, names the divergence classes they
expect in advance, and measures those. The direction they were NOT worried about
goes unnamed and unmeasured. Both recorded encounters of this gap had that exact
shape, and in BOTH the unexamined direction held the larger finding:

  g-335-298 (2026-07-27) startup gate vs timeout message -- the both-directions
    diff found 20 gated-but-unprinted flags the goal premise did not anticipate,
    and the extraction positive-control caught a phantom block comment that would
    have silently dropped one flag.
  g-335-493 (2026-07-29) product-account rows vs an API-side walk -- the reverse
    direction found a class the premise did not name (API-visible with no row),
    and it was the LARGEST at 4 of 12.

Reasoning-side statement of the same lesson: rb-985 (generalized 2026-07-29 from
rubric-vs-demand to ANY two-set reconciliation).

THE FOUR MECHANIZED CHECKS
--------------------------
1. BOTH DIRECTIONS. a_only and b_only are reported separately and always. Neither
   can be omitted by an author who only worried about one of them.

2. POSITIVE CONTROL PER SIDE. An empty extraction and a wrong-path/wrong-filter
   probe look identical -- both yield zero. Each side therefore declares a control
   that must hold for its own count to be trustworthy. A failed control makes the
   whole verdict `control_failed` (exit 2), NOT `ok`: the counts are not evidence
   of anything. This is rb-245 ("verify the field is in the schema before
   believing a zero count") mechanized at the extraction boundary, and the
   every-instance discipline of guard-1617.

3. PARTITION SUM. |a_only| + |b_only| + |both| MUST equal |a union b|. If a
   future edit adds a classification branch that drops or double-counts a member,
   this fails loudly instead of silently under-reporting. A member that belongs to
   no class is the failure this catches.

4. LABEL/VALUE AGREEMENT. An entry whose label embeds a value that disagrees with
   the value it actually renders (`timeout=30s` rendering 60) is reported. A row
   can be present on BOTH sides -- passing every set check above -- and still be
   wrong in this way, so it is a distinct axis, not a sub-case of the diff.

EXIT CODES (the load-bearing distinction)
-----------------------------------------
  0  sets agree, controls passed, partition holds, no label/value mismatch
  1  a real divergence was found (this is a FINDING, not an error)
  2  a control failed, the partition broke, or the input was malformed --
     the counts are UNTRUSTWORTHY and must not be read as a result

2 is deliberately NOT folded into 1. "The sets differ" and "I cannot tell whether
the sets differ" are different states, and collapsing them is how a broken probe
gets read as a clean audit.
"""

import argparse
import json
import re
import sys

# A label like "timeout=30" / "retries: 5" / "mode='fast'" embeds the value it
# claims to render. Captures the trailing token after the separator.
_LABEL_VALUE_RE = re.compile(
    r"""[=:]\s*(?P<val>'[^']*'|"[^"]*"|[^\s,;)]+)\s*$"""
)


def _norm_member(raw):
    """Return (key, label, value) for a member given as a string or an object.

    A plain string is its own key and carries no separate rendered value, so it
    is exempt from the label/value check (there is nothing to disagree with).
    An object {"label":..., "value":...} keys on label and participates.
    """
    if isinstance(raw, dict):
        if "label" not in raw:
            raise ValueError(f"member object missing 'label': {raw!r}")
        label = str(raw["label"])
        return label, label, raw.get("value", None)
    return str(raw), str(raw), None


def _extract_members(side, side_name):
    """Normalize one side's members, rejecting duplicates loudly.

    Duplicates are refused rather than silently collapsed: a set-diff over a bag
    silently changes the denominator, and the partition check below would then be
    validating a number the caller never supplied.
    """
    raw_members = side.get("members")
    if not isinstance(raw_members, list):
        raise ValueError(f"side '{side_name}': 'members' must be a list")
    keys, meta, seen_dupes = [], {}, []
    for raw in raw_members:
        key, label, value = _norm_member(raw)
        if key in meta:
            seen_dupes.append(key)
            continue
        keys.append(key)
        meta[key] = {"label": label, "value": value}
    return keys, meta, seen_dupes


def _check_control(side, side_name, observed_count):
    """Evaluate one side's extraction positive-control.

    Supported kinds:
      min_count  -- the extraction must have found at least N members. Use when
                    the source is KNOWN non-empty; N=1 is the common case and is
                    exactly the "did my grep/path/filter actually match anything"
                    guard.
      sentinel   -- a token that MUST appear in the raw extraction text. Proves
                    the probe reached the right source and read to the end (the
                    terminating-token check that caught the phantom block comment
                    in g-335-298).
      expect_empty -- the caller asserts, in advance, that empty is the CORRECT
                    answer. This is the only way to pass a control with zero
                    members, and it must be declared BEFORE the count is seen.

    A side with no control at all FAILS. Making the control mandatory is the
    whole point: an optional control is not run on the day it matters.
    """
    control = side.get("control")
    if not isinstance(control, dict):
        return False, "no control declared (a side without a positive control cannot be trusted)"
    kind = control.get("kind")

    if kind == "min_count":
        want = control.get("value", 1)
        if not isinstance(want, int) or want < 1:
            return False, f"min_count value must be a positive int, got {want!r}"
        if observed_count >= want:
            return True, f"min_count: {observed_count} >= {want}"
        return False, (
            f"min_count FAILED: extracted {observed_count}, expected >= {want}. "
            f"The '{side_name}' extraction is not proven to have reached its source -- "
            f"treat its count as unknown, not as zero."
        )

    if kind == "sentinel":
        token = control.get("token")
        raw_text = side.get("raw_text")
        if not token:
            return False, "sentinel control missing 'token'"
        if raw_text is None:
            return False, "sentinel control requires 'raw_text' (the extraction output to search)"
        if token in raw_text:
            return True, f"sentinel: {token!r} present in raw_text"
        return False, (
            f"sentinel FAILED: {token!r} absent from '{side_name}' raw_text. "
            f"The probe did not reach the expected source or was truncated."
        )

    if kind == "expect_empty":
        if observed_count == 0:
            return True, "expect_empty: 0 members, as declared"
        return False, (
            f"expect_empty FAILED: declared empty but extracted {observed_count}. "
            f"The declaration and the source disagree -- one of them is wrong."
        )

    return False, f"unknown control kind {kind!r} (want min_count | sentinel | expect_empty)"


def _label_value_mismatches(meta, side_name):
    """Find entries whose label embeds a value disagreeing with what it renders."""
    out = []
    for key, m in meta.items():
        value = m.get("value")
        if value is None:
            continue  # plain string member: no rendered value to disagree with
        match = _LABEL_VALUE_RE.search(m["label"])
        if not match:
            continue
        claimed = match.group("val").strip("'\"")
        actual = str(value)
        if claimed != actual:
            out.append({
                "side": side_name,
                "label": m["label"],
                "claimed_in_label": claimed,
                "rendered_value": actual,
            })
    return out


def diff(payload):
    """Core computation. Returns (result_dict, exit_code)."""
    for side_name in ("a", "b"):
        if not isinstance(payload.get(side_name), dict):
            raise ValueError(f"payload must contain object '{side_name}'")

    a_side, b_side = payload["a"], payload["b"]
    a_label = a_side.get("label", "a")
    b_label = b_side.get("label", "b")

    a_keys, a_meta, a_dupes = _extract_members(a_side, a_label)
    b_keys, b_meta, b_dupes = _extract_members(b_side, b_label)

    a_set, b_set = set(a_keys), set(b_keys)
    a_only = sorted(a_set - b_set)
    b_only = sorted(b_set - a_set)
    both = sorted(a_set & b_set)
    union_size = len(a_set | b_set)

    # Check 3: no member may be silently unclassified.
    partition_sum = len(a_only) + len(b_only) + len(both)
    partition_ok = (partition_sum == union_size)

    # Check 2: per-side positive controls.
    a_ok, a_reason = _check_control(a_side, a_label, len(a_keys))
    b_ok, b_reason = _check_control(b_side, b_label, len(b_keys))

    # Check 4: label/value agreement.
    mismatches = (_label_value_mismatches(a_meta, a_label)
                  + _label_value_mismatches(b_meta, b_label))

    result = {
        "a_label": a_label,
        "b_label": b_label,
        "a_only": a_only,
        "b_only": b_only,
        "both": both,
        "counts": {
            "a_total": len(a_keys),
            "b_total": len(b_keys),
            "a_only": len(a_only),
            "b_only": len(b_only),
            "both": len(both),
            "union": union_size,
        },
        "partition": {
            "ok": partition_ok,
            "sum_of_classes": partition_sum,
            "union_size": union_size,
        },
        "controls": {
            "a": {"passed": a_ok, "detail": a_reason},
            "b": {"passed": b_ok, "detail": b_reason},
        },
        "duplicates_dropped": {"a": a_dupes, "b": b_dupes},
        "label_value_mismatches": mismatches,
    }

    # Verdict precedence: untrustworthy beats divergent beats ok. A control
    # failure must never be reportable as a clean diff.
    if not (a_ok and b_ok) or not partition_ok:
        result["verdict"] = "control_failed"
        return result, 2
    if a_only or b_only or mismatches:
        result["verdict"] = "divergent"
        return result, 1
    result["verdict"] = "ok"
    return result, 0


def _render_text(r):
    lines = []
    lines.append(f"verdict: {r['verdict']}")
    c = r["counts"]
    lines.append(
        f"  {r['a_label']}: {c['a_total']}   {r['b_label']}: {c['b_total']}   "
        f"union: {c['union']}"
    )
    lines.append(f"  controls: {r['a_label']}={'PASS' if r['controls']['a']['passed'] else 'FAIL'}"
                 f"  {r['b_label']}={'PASS' if r['controls']['b']['passed'] else 'FAIL'}")
    for side in ("a", "b"):
        if not r["controls"][side]["passed"]:
            lines.append(f"    {side}: {r['controls'][side]['detail']}")
    if not r["partition"]["ok"]:
        lines.append(f"  PARTITION BROKEN: classes sum to {r['partition']['sum_of_classes']} "
                     f"but union is {r['partition']['union_size']}")
    lines.append(f"  only in {r['a_label']} ({c['a_only']}): {r['a_only'] or '-'}")
    lines.append(f"  only in {r['b_label']} ({c['b_only']}): {r['b_only'] or '-'}")
    if r["label_value_mismatches"]:
        lines.append(f"  label/value mismatches ({len(r['label_value_mismatches'])}):")
        for m in r["label_value_mismatches"]:
            lines.append(f"    [{m['side']}] {m['label']!r} claims {m['claimed_in_label']!r} "
                         f"but renders {m['rendered_value']!r}")
    for side in ("a", "b"):
        if r["duplicates_dropped"][side]:
            lines.append(f"  NOTE duplicates dropped from {side}: {r['duplicates_dropped'][side]}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(
        description="Two-way diff of mirrored sets with extraction positive-controls."
    )
    ap.add_argument("--input", help="path to payload JSON (default: stdin)")
    ap.add_argument("--output", choices=("json", "text"), default="json")
    args = ap.parse_args()

    try:
        raw = open(args.input, encoding="utf-8").read() if args.input else sys.stdin.read()
        payload = json.loads(raw)
    except Exception as e:
        print(json.dumps({"verdict": "control_failed",
                          "error": f"unreadable payload: {type(e).__name__}: {e}"}))
        return 2

    try:
        result, code = diff(payload)
    except Exception as e:
        print(json.dumps({"verdict": "control_failed",
                          "error": f"{type(e).__name__}: {e}"}))
        return 2

    print(_render_text(result) if args.output == "text" else json.dumps(result, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
