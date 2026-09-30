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
| guardrail retire/revise verdicts | `guardrail-retire.sh` step 3 → `guardrail_retire.truth_event_for` | live |
| hypothesis resolutions that test a tree node | `pipeline-move.sh` / `pipeline-add.sh` `_calibration_capture` → `capture_resolution_response` → `hypothesis_truth_event` | live (g-306-553) |

### guardrail-retire, and the two things a reader must not re-derive

Verdict map (`truth_event_for`, pure and unit-tested — `tests/test_guardrail_retire_truth_event.py`):
`keep`/`refresh` → `survived`, `revise` → `revised`, `retire` → `refuted`.

**`revise` is `revised`, never `refuted`.** `_verdict_mutations` defines that verdict as
"still relevant, just stale-worded" — the CLAIM stood and the wording did not. Collapsing it
into `refuted` scores a surviving entry as a failure and biases the curve pessimistic, which
is the mirror of the g-115-9063 defect on the adjudication surface (an inverted mapping that
wrote `survived` on every row where the entry was wrong, so the curve could only ever report
perfect calibration). Both directions are silent: neither raises, and the ledger accepts any
verdict it recognises.

**Capture is in the WRAPPER, after the mutation loop — not in `apply()`, which is where it
looks like it belongs.** `apply()` computes a mutation PLAN and returns it; the wrapper
executes it via `guardrails-update-field.sh` and the engine never writes the store
(guard-832). A recorder inside `apply()` would log verdicts that were merely COMPUTED,
including plans the wrapper then failed to execute — manufacturing exactly the fiction this
ledger exists to measure. That is guard-4238's adjacent trap. The capture is therefore gated
on `exec_rc=0` after the loop, and is `apply`-only: `restore` un-does a prior verdict rather
than rendering a new one.

### hypothesis resolutions, the one surface that scores the TREE

The two surfaces above judge reasoning-bank entries and guardrails, and those stores barely
declare confidence (the caveat above), so their rows are null on the x-axis. The tree index
does declare it. This surface feeds the tree from the hypothesis lane, which is the truth
surface that fires most often (g-306-553, from rb-12446). The link is the pipeline field
`tests_node` = `{key, stance}`, written at formation; the resolver adds `node_verdict`. Schema:
`pipeline.md` § Tested-Node Link.

Verdict map (`hypothesis_truth_event`, pure and unit-tested —
`tests/test_confidence_ledger_hypothesis.py`):

| resolution | row verdict |
|---|---|
| any outcome with a valid `node_verdict` | that value |
| `CONFIRMED` + `stance: supports` | `survived` |
| `CONFIRMED` + `stance: challenges` | `refuted` |
| `CONFIRMED` with no valid stance, or `CORRECTED` with no `node_verdict` | `unknown` (kept, never guessed) |
| `EXPIRED`, `UNRESOLVABLE`, no outcome, no usable `tests_node` | no row |

`extra` carries `outcome`, `stance`, `verdict_basis` and `node_in_index`. `node_in_index`
separates the two causes of a null `declared_confidence`: a node that declares none, and a
key that names no node.

**A bare CORRECTED is never mapped to `refuted`.** A prediction can miss its criterion while the
mechanism it rested on held (guard-2728), so only the resolver's `node_verdict` scores it.
CONFIRMED is matched exactly (guard-654). Mapping it without the stance would score a confirmed
challenge as a survival: that is the polarity inversion g-115-9063 found on the adjudication
surface.

**Capture is in the WRAPPERS, after the daemon's 200, not in the daemon.** `judge_model` and
`harness` must describe the resolver, and the daemon process cannot know them (guard-742 was
weighed on this point). The daemon answers 200 only after its locked write of the pipeline
store, so a row never outlives a resolution that failed. The wrapper parses `record` only
(guard-4578), second-guesses nothing (guard-582), sends its own stdout to stderr and swallows
every failure, so the record it prints and its exit code cannot change. The move is refused on
an already-resolved record, so each resolution records at most once.

**The review-hypotheses E10 recalibration runs AFTER the move (its Step 4.3).** It lowers the
confidence of the tree nodes a high-surprise CORRECTED record consulted, and the tested node is
usually one of them. Run before the move, it made the row carry a value this verdict had already
lowered. That breaks the at-event-time rule above, and it biases refuted rows toward lower
declared confidence.

Remaining candidate surfaces named by g-306-399 but **not** wired: stale-claim-artifact
sweeps (`/sweep-stale-claim-artifacts` was invoked 2 times ever, both on its 2026-08-01
forge day, measured 2026-09-29 over every agent's `skill-invocations.jsonl` — wiring it adds
a surface that does not fire), and curate-memory RETIRE/REVISE decisions that cite CONTENT
evidence. Hypothesis resolutions that test a reasoning-bank entry are not linked either:
`tests_node` names tree nodes only, because the tree is where declared confidence lives.

Utilization-only retirements are explicitly **excluded** — a popularity signal is not a truth
event, and mixing them pollutes the curve with usage data. This is why a bare
`guardrail-retire.sh apply <id> retire` records NOTHING: that lane's retire verdict is driven
by staleness + `effective_relevance` scoring, so an uncited retire IS the utilization-only
shape. A `retire` carrying `--reason` is a content judgement and IS recorded, with the reason
as `evidence_ref`. The exclusion keys on the EVIDENCE, never on the verdict name.

## Reading it

There is still no reader script, by design. The calibration table (g-306-399 outcome 3) moved
to **g-306-553** when g-306-399 closed on 2026-09-29. The 50+ event window was never the gate:
every row carried `declared_confidence: null` (29 of 29 on 2026-09-29), so every bucket is
empty at any N. g-306-553 calibrates the TREE instead, since it is the store that carries the
field. It does so through the hypothesis lane above, the one tree-facing truth surface that
fires with volume. Before wiring any surface, cross two facts: does the judged store carry the
field, and does the surface actually fire (count its invocations)? Either fact alone still
leaves every row null (rb-12446).

## Cross-references

- `core/config/conventions/skill-quality.md` — judge provenance (`judge_model`, `harness`)
- `.claude/rules/verify-before-assuming.md` — the null-vs-default distinction above
