#!/usr/bin/env bash
# mutation-partition-proof.sh (gap-047, guard-1861, guard-2134) — run an
# N-MUTATION matrix and report which cases are actually proven.
#
# WHY THIS EXISTS, given mutation-proof-test.sh already ships. That script takes
# ONE sabotage per invocation and answers "is this test vacuous?". guard-1861
# raised the bar past that shape: a single file-level RED cannot distinguish a
# declared control from a case that asserts nothing, so proving a SET of cases
# needs COMPLEMENTARY mutations that partition them. Three measured encounters
# hand-rolled that loop (,  rounds 9 and 11) and each failed
# a DIFFERENT way — an incomplete matrix whose tally concealed 2 unproven cases,
# a survivor saved by a COMMENT, and a survivor saved by an unrelated CODE site.
#
# This script COMPOSES mutation-proof-test.sh rather than duplicating it: every
# per-mutant baseline/RED/restore/byte-verify step is delegated, so the restore
# guarantee has exactly one implementation. What is added here is only the part
# a single invocation structurally cannot provide:
#   1. the loop, with restore asserted BETWEEN mutants (a matrix that corrupts
#      the tree is worse than no matrix)
#   2. a per-mutation row carrying WHAT it changed and HOW BROADLY (sites/basis)
#   3. a NEGATIVE CONTROL per mutant — the unanchored predicate run alongside
#      the real one, which must stay blind. That is what converts "my check is
#      anchored" from an assertion into a measurement.
#   4. a PARTITION verdict: every declared case must be killed by at least one
#      mutation. A case no mutation reddens is UNPROVEN and is named as such —
#      the k/N tally that concealed it is exactly the  failure.
#
# Domain-free: --plan carries all targets, commands and mutations; this file
# hard-codes no product, service or test-runner name.
set -uo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: mutation-partition-proof.sh --plan <plan.json> [--jobs 1] [--quiet]

plan.json:
{
  "target":   "path/to/file/under/test",     # default target for every mutation
  "workdir":  ".",                           # where test_cmd runs
  "test_cmd": "<command that must go RED under each mutation>",
  "control_cmd": "<unanchored predicate that must stay BLIND>",   # optional
  "mutations": [
    { "name": "m1", "case": "case-a",
      "sabotage_old": "<exact string>", "sabotage_new": "<replacement>" },
    { "name": "m2", "case": "case-b", "sabotage_sed": "0,/re/s//new/",
      "target": "other/file", "test_cmd": "<override>", "control_cmd": "<override>" }
  ]
}

Per mutation: "case" defaults to "name". target/test_cmd/control_cmd/workdir
fall back to the plan-level value. Provide exactly one sabotage form per entry.

Prefer a SINGLE-SITE sabotage. A mutation that changes N>1 sites makes its RED
uninformative (any predicate touching the token anywhere goes red), and the row
is flagged `broad: true` — see mutation-proof-test.sh's sabotage_sites.

Exit: 0 all mutations killed and every case proven | 1 survivor or unproven case
      2 usage/operational error | 3 RESTORE FAILED (manual recovery needed)
Emits a JSON report on stdout.
EOF
}

PLAN=""; QUIET=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --plan) PLAN="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
    --quiet) QUIET=1; shift;;
    -h|--help) usage; exit 2;;
    *) echo "ERROR: unknown arg: $1" >&2; usage; exit 2;;
  esac
done
[[ -n "$PLAN" && -f "$PLAN" ]] || { echo "ERROR: --plan <plan.json> is required and must exist" >&2; usage; exit 2; }

if command -v python3 >/dev/null 2>&1; then PY="python3"; else PY="py -3"; fi
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROVER="$HERE/mutation-proof-test.sh"
[[ -f "$PROVER" ]] || { echo "ERROR: companion not found: $PROVER" >&2; exit 2; }

# Normalise the plan into one line per mutation, fields separated by US (0x1f).
# NOT tab: tab is an IFS *whitespace* character, so bash `read` collapses runs of
# them into a single delimiter and an EMPTY field silently disappears, shifting
# every later field left. An omitted control_cmd is exactly that case. Measured
# 2026-07-31 while building this: `printf 'a\tb\t\tc' | IFS=$'\t' read f1 f2 f3 f4`
# yields f3=c f4=empty, and the bug was invisible in the 6-mutation dogfood
# because every entry there HAD a control_cmd. US is not whitespace, so empty
# fields survive. Validation lives in python (real JSON parsing); the loop below
# stays pure bash.
PLAN_TSV="$(PLAN="$PLAN" $PY -c '
import json, os, sys
p = json.load(open(os.environ["PLAN"], encoding="utf-8"))
muts = p.get("mutations") or []
if not muts:
    sys.stderr.write("ERROR: plan has no mutations\n"); sys.exit(2)
rows = []
for i, m in enumerate(muts):
    name = m.get("name") or f"m{i+1}"
    have_old = "sabotage_old" in m
    have_sed = "sabotage_sed" in m
    if have_old == have_sed:
        sys.stderr.write(f"ERROR: mutation {name}: provide exactly one of sabotage_old / sabotage_sed\n")
        sys.exit(2)
    # REFUSE a multi-line sabotage rather than silently flattening it (rb-6196).
    # The row below is a \x1f-separated TSV so the driving loop can stay pure
    # bash, and `read` is line-based -- so newlines structurally CANNOT survive
    # that bridge. Every field is newline-stripped, sabotage_old included.
    # Measured (zeta, cc-02): a 7-mutation plan returned 4 survivors, all reading
    # "sabotage-old string not found in target". The split was a perfect
    # partition on LINE COUNT -- every single-line sabotage killed, every
    # multi-line one "not found". That message is honest about the OUTCOME and
    # MISLEADING about the CAUSE: it reads as "your anchor is stale", so the next
    # move is re-deriving an anchor that was never wrong. The worse branch is
    # reading N survivors as N real gaps and "fixing" correct tests.
    # sabotage_sed is the working multi-line escape hatch -- the EXPRESSION stays
    # single-line while sed does the multi-line work (`/anchor/{N;d}` for a
    # two-line delete). All four measured failures re-expressed successfully.
    for _f in ("sabotage_old", "sabotage_new", "sabotage_sed"):
        if "\n" in (m.get(_f) or ""):
            sys.stderr.write(
                f"ERROR: mutation {name}: {_f} contains a newline. This plan is "
                "flattened into a newline-separated TSV, so a multi-line value "
                "cannot survive and would fail later as a misleading "
                "sabotage-old-string-not-found-in-target error. Use sabotage_sed, "
                "whose expression stays single-line while sed does the "
                "multi-line work: /<anchor>/{{N;d}} deletes two lines, or chain "
                "single-line s/// commands. Escape bracket literals; BRE reads "
                "a bare [1] as a bracket expression.\n")
            sys.exit(2)
    target = m.get("target") or p.get("target")
    test_cmd = m.get("test_cmd") or p.get("test_cmd")
    if not target or not test_cmd:
        sys.stderr.write(f"ERROR: mutation {name}: target and test_cmd are required\n"); sys.exit(2)
    rows.append("\x1f".join(x.replace("\x1f", " ").replace("\n", " ") for x in [
        name, m.get("case") or name, target,
        m.get("workdir") or p.get("workdir") or ".",
        test_cmd, m.get("control_cmd") or p.get("control_cmd") or "",
        "old" if have_old else "sed",
        m.get("sabotage_old", "") if have_old else m.get("sabotage_sed", ""),
        m.get("sabotage_new", ""),
    ]))
print("\n".join(rows))
')" || exit 2

ROWS_JSON="$(mktemp)"; : > "$ROWS_JSON"
cleanup() { rm -f "$ROWS_JSON"; }
trap cleanup EXIT

say() { [[ $QUIET -eq 1 ]] || echo "$@" >&2; }

FAILED=0; RESTORE_FAILED=0
while IFS=$'\x1f' read -r NAME CASE TARGET WORKDIR TEST_CMD CONTROL_CMD FORM SAB1 SAB2; do
  [[ -n "$NAME" ]] || continue
  if [[ "$FORM" == "old" ]]; then
    SAB_ARGS=(--sabotage-old "$SAB1" --sabotage-new "$SAB2")
  else
    SAB_ARGS=(--sabotage-sed "$SAB1")
  fi

  # --- the mutation must be KILLED by the real predicate ---
  MAIN="$(bash "$PROVER" --target "$TARGET" --workdir "$WORKDIR" \
            --test-cmd "$TEST_CMD" "${SAB_ARGS[@]}" 2>/dev/null)"

  # --- negative control: the unanchored predicate must stay BLIND ---
  # "Blind" is mutation-proof-test.sh's VACUOUS verdict: the control PASSED
  # against sabotaged code, i.e. it did NOT detect the mutation. A control that
  # goes red is contaminated (it is not unanchored after all) and the anchoring
  # claim is unproven, so it is reported rather than silently ignored.
  CONTROL="null"
  if [[ -n "$CONTROL_CMD" ]]; then
    CONTROL="$(bash "$PROVER" --target "$TARGET" --workdir "$WORKDIR" \
                 --test-cmd "$CONTROL_CMD" "${SAB_ARGS[@]}" 2>/dev/null)"
    [[ -n "$CONTROL" ]] || CONTROL="null"
  fi

  ROW="$(NAME="$NAME" CASE="$CASE" TARGET="$TARGET" FORM="$FORM" \
         SAB1="$SAB1" SAB2="$SAB2" MAIN="${MAIN:-}" CONTROL="$CONTROL" $PY -c '
import json, os
def load(s):
    s = (s or "").strip()
    i = s.find("{")
    if i < 0: return None
    try: return json.loads(s[i:])
    except ValueError: return None
main = load(os.environ["MAIN"])
ctl  = load(os.environ["CONTROL"])
# A mutation is KILLED when the prover returns PASS: the predicate went red
# under sabotage and green again after restore.
killed = bool(main and main.get("verdict") == "PASS")
sites  = (main or {}).get("sabotage_sites")
row = {
  "name": os.environ["NAME"], "case": os.environ["CASE"],
  "target": os.environ["TARGET"],
  "changed": (os.environ["SAB1"] + (" -> " + os.environ["SAB2"]
              if os.environ["FORM"] == "old" else ""))[:200],
  "form": os.environ["FORM"],
  "killed": killed,
  "verdict": (main or {}).get("verdict"),
  "reason": (main or {}).get("reason"),
  "sabotage_sites": sites,
  "sabotage_sites_basis": (main or {}).get("sabotage_sites_basis"),
  # A >1-site mutation cannot prove anchoring — the row says so rather than
  # letting a killed tally imply more than it earned (guard-1856).
  "broad": bool(isinstance(sites, int) and sites > 1),
  "restore_status": (main or {}).get("restore_status"),
  # : restore_status answers "target == backup", which is NOT
  # "target is clean" — a backup taken over pre-existing residue matches
  # itself. The RESIDUE case maps onto restore_status "FAILED", so the
  # RESTORE_FAILED detection below already catches it unchanged; this field
  # exists for the OTHER value. "unavailable" (--sabotage-sed, whose injected
  # text is unknowable) means the clean-check DID NOT RUN, and a row that
  # renders that identically to "clean" is how the original defect stayed
  # invisible. Absence of measurement, never a pass.
  "residue_check": (main or {}).get("residue_check"),
  "control": None if ctl is None else {
      "verdict": ctl.get("verdict"),
      # blind == the control did NOT detect the mutation == VACUOUS
      "blind": ctl.get("verdict") == "FAIL" and "VACUOUS" in str(ctl.get("reason")),
  },
}
print(json.dumps(row))
')"
  printf '%s\n' "$ROW" >> "$ROWS_JSON"

  K="$(printf '%s' "$ROW" | $PY -c 'import json,sys; print("KILLED  " if json.load(sys.stdin)["killed"] else "SURVIVOR")')"
  [[ "$K" == "KILLED  " ]] || FAILED=1
  case "$MAIN" in *'"restore_status": "FAILED"'*) RESTORE_FAILED=1;; esac
  say "  $K $NAME (case=$CASE)"
done <<< "$PLAN_TSV"

ROWS_JSON="$ROWS_JSON" $PY -c '
import json, os, sys
rows = [json.loads(l) for l in open(os.environ["ROWS_JSON"], encoding="utf-8") if l.strip()]
cases = {}
for r in rows:
    cases.setdefault(r["case"], []).append(r)
# PARTITION: a case is proven only if some mutation targeting it was killed.
# Reporting k/N alone is what let 2 unproven cases hide behind 5/5 ().
unproven = sorted(c for c, rs in cases.items() if not any(r["killed"] for r in rs))
survivors = [r["name"] for r in rows if not r["killed"]]
broad = [r["name"] for r in rows if r.get("broad")]
contaminated = [r["name"] for r in rows
                if r.get("control") is not None and not r["control"]["blind"]]
uncontrolled = [r["name"] for r in rows if r.get("control") is None]
restore_bad = [r["name"] for r in rows if r.get("restore_status") == "FAILED"]
ok = not survivors and not unproven and not restore_bad
print(json.dumps({
  "verdict": "PASS" if ok else "FAIL",
  "mutations": len(rows), "killed": sum(1 for r in rows if r["killed"]),
  "cases": len(cases), "cases_unproven": unproven,
  "survivors": survivors,
  # Named, never merely counted: a broad mutation still killed the test, but it
  # did not prove anchoring, and a bare tally would let it read as though it had.
  "broad_mutations": broad,
  "controls_contaminated": contaminated,
  "mutations_without_control": uncontrolled,
  "restore_failed": restore_bad,
  "rows": rows,
}, indent=2))
sys.exit(0 if ok else 1)
'
RC=$?
[[ $RESTORE_FAILED -eq 1 ]] && exit 3
exit $(( RC != 0 || FAILED != 0 ? 1 : 0 ))
