# S3 axis 2: why the aspiration id comes from the PARENT

Rationale for `.claude/skills/aspirations-strategic-scan/SKILL.md` Phase S3,
axis 2 (aspiration concentration).

## The defect

The pseudocode read `g.aspiration_id`. That key is present on **ZERO** of the
pending goals in `aspirations-compact.json` — goals are NESTED under their
aspiration, so the id lives on the parent record. `.get()` therefore returned
`None` for every goal, all of them landed in one `None` bucket, and the axis
printed `100.0% FIRES`.

Measured 2026-08-27 (bravo, `hostname` cc-05, `uname -r` 6.8.0-137-generic),
full compact, n=2273 pending: key present on 0/2273. Corrected to the parent's
id — `asp-115 1908/2273 = 83.9%`, matching the roster's standing 82–84% band.

## Why no control caught it

Every guard in this area aims at **zeros** — guard-2421 (positive-control before
believing a zero), guard-2298, guard-2562, guard-2919, rb-245's schema probe.
An absent **grouping key** fails the other way: it collapses the distribution
into a single bucket and reports the **maximum**, which is also the most
alarming and therefore the most believable-looking value the metric can take.
Nothing raises, nothing prints empty, and the axis that legitimately fires just
reports a worse number — so nothing looks wrong. S2a's `opened/total` control
guards a different step and is blind here.

The cheap remedy is the prior this ritual already keeps: 14+ roster readings in
an 80–84% band, against which `100.0%` is impossible. One comparison catches it.

## Cross-references

- `rb-9453` — the generalized lesson (absent grouping key reports 100%, not 0)
- `rb-8962` — same root, opposite end: there being-a-KEY changes a *fix's* scope;
  here it changes the *defect's* direction
- `guard-359` — verify a field the pseudocode names is actually emitted
- `core/config/strategic-scan-readings.md` — the roster row for this pass
