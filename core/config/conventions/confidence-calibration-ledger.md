# Confidence Calibration Ledger

`world/confidence-calibration-ledger.jsonl` — an append-only record pairing an entry's
**declared confidence** with what later **happened to its claim**.

A confidence value nobody scores against outcomes is a vibe. The hypothesis lane already
scores itself honestly (resolution criteria → measured outcomes → per-category accuracy).
Tree-node, reasoning-bank and guardrail `confidence` is self-declared at encode time and
never joined to any later verdict, so the input a calibration curve needs evaporates at
the exact moment it exists. This ledger is that join. (g-306-399.)

## Schema

One JSON object per line, appended via `_fileops.locked_append_jsonl`.

| field | meaning |
|---|---|
| `ts` | naive UTC timestamp of the truth event |
| `entry_id` | the entry judged (`rb-NNN`, `guard-NNN`, or a tree node key) |
| `store` | `tree` \| `reasoning_bank` \| `guardrails` \| `unknown` |
| `declared_confidence` | the value the entry carried **at event time**, or `null` |
| `verdict` | `survived` \| `refuted` \| `revised` \| `unknown` (closed set) |
| `evidence_ref` | what settled it — a board msg id, goal id, commit, or measurement |
| `source` | the surface that produced the verdict (e.g. `adjudication-lane`) |
| `agent`, `session_id` | who recorded it |
| `judge_model`, `harness` | judge provenance, resolved caller-side (g-306-394/400 rules) |

`declared_confidence` must be captured at the event, never looked up later: once the entry
is edited, the value it was carrying when judged is unrecoverable. A `null` is honest data —
never substitute a default, which would be indistinguishable downstream from a real reading
and would manufacture the very curve this ledger exists to measure.

## Writer

`core/scripts/_confidence_ledger.py`, shaped after `_override_helpers.py` (same store class,
same audit posture):

- `record_truth_event(entry_id, store, verdict, *, source, evidence_ref=None, declared_confidence=<resolved>, world_dir=None, extra=None)`
- `resolve_declared_confidence(entry_id, store, *, world_dir=None) -> float | None`

**Never raises.** A failed audit write prints a stderr WARN and continues — an audit lane
must not break the caller whose work it is recording, and a silent loss is the only outcome
worse than a noisy one.

Note `resolve_declared_confidence` reads a tree node's confidence from the tree **index**
(`_tree.yaml`), not from the node's own front matter — the field lives only in the index.
A resolver written against front matter returns `None` for every node and looks like a
store with no confidence at all.

## The measured caveat — read before wiring a new surface

Declared confidence is nearly absent from the two stores that currently produce verdicts
(measured 2026-09-01; positive-controlled — `"id"` present on 9466/9466 and 5434/5434 lines,
so these are real zeros, not broken greps):

| store | carries declared confidence |
|---|---|
| tree nodes (via the index) | **530 / 1551 — 34%** |
| reasoning_bank | **63 / 9466 — 0.67%** |
| guardrails | **3 / 5434 — 0.06%** |

The only instrumented truth-event surface is the adjudication lane, whose
`SCOPE_STORES = ("reasoning_bank", "guardrails")`. Tree is deliberately **out of scope**
there, for an unrelated and sound reason: tree nodes lack a reliable `encoded_by`, so
reviewer self-exclusion could not be enforced.

So the surface that produces verdicts and the store that carries confidence are **disjoint**,
and each exclusion is individually correct. The consequence is structural, not a bug:
rows from this surface will carry `declared_confidence: null` most of the time.

They are still worth capturing — the verdict and evidence are real, and the nulls are
themselves a finding about which stores declare confidence at all. But anyone producing the
calibration table **must bucket on `declared_confidence is not None` first and report the
null count.** Folding nulls into a denominator produces a confident-looking curve computed
over rows that carry no x-axis.

Closing the gap needs one of: a truth-event surface over tree nodes, or `confidence` becoming
a real field on rb/guardrail entries. Both are larger than this ledger and neither is assumed
here.

## Wired surfaces

| surface | where | status |
|---|---|---|
| adjudication lane resolutions | `adjudication-lane.py::cmd_resolve` → `_capture_confidence_truth_event` | live |

Candidate surfaces named by g-306-399 but **not** wired: stale-claim-artifact sweeps,
curate-memory RETIRE/REVISE decisions that cite CONTENT evidence, and hypothesis resolutions
naming a tree/rb entry. Utilization-only retirements are explicitly **excluded** — a
popularity signal is not a truth event, and mixing them pollutes the curve with usage data.

## Reading it

There is no reader script yet, by design: the first consumer is the one-off calibration table
(g-306-399 outcome 3), which needs a bounded window of events (50+) before it can say
anything. Building a reader before there is anything to read would be a writer-without-reader
of the opposite kind.

## Cross-references

- `core/config/conventions/skill-quality.md` — judge provenance (`judge_model`, `harness`)
- `.claude/rules/verify-before-assuming.md` — the null-vs-default distinction above
