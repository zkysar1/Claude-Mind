#!/usr/bin/env python3
"""Extract the /verify-learning check corpus out of SKILL.md into a data registry.

WHY THIS EXISTS: `.claude/skills/verify-learning/SKILL.md` reached 1,208,153 B in
one file — 7x the next-largest skill. Measured skill injections average ~63.5 KB
and every one arrives with a truncation marker, so a 1.21 MB skill reaches the
model as roughly its first 5%: ~2,236 checks are written down and ~95% of them
are never delivered, while the skill still reports success. That is a silent
verification failure, not a size complaint. (g-115-6689.)

═══ WHY BYTE-EXACT ROUND-TRIP, AND NOT A SEMANTIC RE-SERIALISATION ═══

The obvious design — parse each check into {id, text, why} and re-emit — is
WRONG here, and measurably so. The corpus has three record shapes, and they do
not agree on line-boundedness:

  * `Check: ...`          1,865 — always one line.
  * `Bash (name): ...`      371 — always one line (0 end in a continuation).
  * `Bash: ...`             652 — **CAN SPAN MANY LINES.** Verbatim from Section
                                  MAC1, a bare `Bash:` opens `python3 -c "` and
                                  runs 8 further lines before its closing quote.

A parser built on the first two shapes silently truncates the third at its first
newline, producing a registry that looks complete (the count is right, because
bare `Bash:` is not one of the 2,236) while the commands inside it are cut in
half. The corruption is invisible to a count-based losslessness check — which is
exactly the check the goal asks for.

So this module never re-serialises. It stores each block's VERBATIM bytes in
`raw` and preserves order in `seq`; typed fields (`kind`, `id`, `section`) are
ADDITIVE metadata layered on top for querying. `verify` regenerates the region
by concatenating `raw` in `seq` order and asserts equality with the original,
byte for byte. Losslessness is then a property of construction rather than a
claim about the parser's coverage.

READ-ONLY BY DEFAULT: `extract` prints a report and writes nothing unless
`--write` is passed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

SKILL = Path(".claude/skills/verify-learning/SKILL.md")
REGISTRY = Path("core/config/verify-learning-checks.jsonl")

# A `## ` step header. The corpus is not confined to one step: measured
# Step 1 = 1 check, Step 3 = 1,902, Step 4 = 333. A Step-3-only extraction
# would leave 333 checks behind and still pass a "Step 3 is now small" check.
STEP_RE = re.compile(r"^## (.+)$")
CHECK_RE = re.compile(r"^(\s*)Check:\s?(.*)$")
BASH_NAMED_RE = re.compile(r"^(\s*)Bash\s*\(([^)]+)\):\s?(.*)$")
BASH_BARE_RE = re.compile(r"^(\s*)Bash\s*:\s?(.*)$")
COMMENT_RE = re.compile(r"^\s*#")
SECTION_RE = re.compile(r"\(Section ([A-Z0-9]{1,6})\)")

# Steps whose bodies hold the check corpus. Step 2 and Chaining carry none.
CORPUS_STEPS = ("Step 3: Evidence Check", "Step 4: Summary Report")


# ── how a multi-line command body is recognised, and why NOT by quotes ──────
#
# A bare `Bash:` can open a shell string that runs for many lines (Section MAC1
# opens `python3 -c "` and runs 8 more). The intuitive fix is to track the open
# quote and consume until it closes. That was built, measured, and ABANDONED —
# it fails in both directions on this corpus:
#
#   double-quote only : `Bash: grep -c '\.compact-agent"' ...` carries a lone `"`
#                       inside a SINGLE-quoted string, so the block never closes
#                       and swallows 142 lines — taking 126 real records with it.
#                       Corpus count 2,109 against a known 2,236.
#   both quotes       : apostrophes in ordinary prose ("banner.sh's format") open
#                       a state that never closes. WORSE: 1,712.
#
# Byte-exact round-trip PASSES under both, because the bytes are all present and
# merely mis-attributed — so the round-trip cannot see this class at all, and
# only the independently-known corpus count exposes it.
#
# The structural signal is INDENTATION, and it is unambiguous: every record line
# sits at indent >= 3 (measured 2,799 at 3, plus 7/81/1 at 5/7/10 — and ZERO at
# 0), while multi-line command bodies sit at indent 0. An indent-0 line therefore
# can never be a record, which removes the need to "consume until" anything.
# Each line is classified independently and body lines carry `parent_seq` back to
# the record they belong to. No state machine, nothing to get out of sync.
RECORD_MIN_INDENT = 1


def _indent(line: str) -> int | None:
    """None for a blank line; otherwise the leading-whitespace width."""
    if not line.strip():
        return None
    return len(line) - len(line.lstrip())


def _classify_record(line: str):
    """-> (kind, id) for a RECORD line, or (None, None) if it is not one.

    The check/bash cascade, extracted so `parse` and `cmd_add` cannot disagree
    about what a line IS (g-115-7804). cmd_add used to hardcode kind="check",
    so a bash check added through the tool landed as k=check with i=None and
    was registered but never executed — false coverage, which is worse than no
    coverage because the registry count rises while nothing runs.

    Order is load-bearing and matches the parser's original cascade: CHECK_RE
    is tried BEFORE BASH_NAMED_RE, so a line that literally starts `Check:` is
    a check even when the rest of it mentions Bash.
    """
    if CHECK_RE.match(line):
        return "check", None
    m = BASH_NAMED_RE.match(line)
    if m:
        return "bash_named", m.group(2)
    if BASH_BARE_RE.match(line):
        return "bash_bare", None
    return None, None


def parse(text: str):
    """-> (blocks, meta). Blocks are verbatim; nothing is re-serialised."""
    lines = text.splitlines(keepends=True)
    blocks = []
    step = None
    section = None
    seq = 0
    last_record = None
    i = 0
    in_corpus = False

    while i < len(lines):
        line = lines[i]
        m = STEP_RE.match(line.rstrip("\n"))
        if m:
            step = m.group(1).strip()
            in_corpus = step in CORPUS_STEPS
            section = None
            blocks.append({"seq": seq, "kind": "step_header", "step": step,
                           "section": None, "id": None, "parent_seq": None, "raw": line})
            seq += 1
            i += 1
            continue

        if not in_corpus:
            blocks.append({"seq": seq, "kind": "outside", "step": step,
                           "section": section, "id": None, "parent_seq": None, "raw": line})
            seq += 1
            i += 1
            continue

        sm = SECTION_RE.search(line)
        if sm:
            section = sm.group(1)

        raw = line
        kind, cid, parent = "prose", None, None
        ind = _indent(line)

        if ind is not None and ind < RECORD_MIN_INDENT:
            # Indent 0: a continuation of the preceding record's command body.
            # Never a record — measured, zero records live at indent 0.
            kind = "body"
            parent = last_record
        elif COMMENT_RE.match(line):
            kind = "why"
        else:
            k, c = _classify_record(line)
            if k:
                kind, cid = k, c

        if kind in ("check", "bash_named", "bash_bare"):
            last_record = seq

        blocks.append({"seq": seq, "kind": kind, "step": step,
                       "section": section, "id": cid, "parent_seq": parent,
                       "raw": raw})
        seq += 1
        i += 1

    meta = {
        "checks": sum(1 for b in blocks if b["kind"] == "check"),
        "bash_named": sum(1 for b in blocks if b["kind"] == "bash_named"),
        "bash_bare": sum(1 for b in blocks if b["kind"] == "bash_bare"),
        "body": sum(1 for b in blocks if b["kind"] == "body"),
        "why": sum(1 for b in blocks if b["kind"] == "why"),
        "prose": sum(1 for b in blocks if b["kind"] == "prose"),
        "outside": sum(1 for b in blocks if b["kind"] == "outside"),
        "blocks": len(blocks),
    }
    meta["corpus_checks"] = meta["checks"] + meta["bash_named"]
    return blocks, meta


# The corpus is ~10.7k blocks, so anything stored per-block is paid 10.7k times.
# A first pass wrote every field on every line and produced a 2.58 MB registry —
# LARGER than the 1.21 MB file it replaces, which would have made the change hard
# to defend on its own terms. Nulls are dropped and `step` is emitted as a short
# code, both of which are pure encoding: `_fat` restores the full record on read,
# so the round-trip guarantee is untouched.
_STEP_CODE = {s: c for c, s in enumerate(CORPUS_STEPS)}
_CODE_STEP = {c: s for s, c in _STEP_CODE.items()}


def _slim(b: dict) -> dict:
    out = {"q": b["seq"], "k": b["kind"], "raw": b["raw"]}
    if b.get("step") in _STEP_CODE:
        out["s"] = _STEP_CODE[b["step"]]
    elif b.get("step"):
        out["S"] = b["step"]
    for src, dst in (("section", "x"), ("id", "i"), ("parent_seq", "p")):
        if b.get(src) is not None:
            out[dst] = b[src]
    return out


def _fat(o: dict) -> dict:
    return {
        "seq": o["q"], "kind": o["k"], "raw": o["raw"],
        "step": _CODE_STEP.get(o["s"]) if "s" in o else o.get("S"),
        "section": o.get("x"), "id": o.get("i"), "parent_seq": o.get("p"),
    }


def load_registry(path: Path = None):
    # Resolve REGISTRY at CALL time, not as a default-arg binding. A default
    # argument freezes the module constant at import, so every attempt to point
    # this at another file — a test, a positive control, an archived copy —
    # silently reads the real registry and reports a pass. Measured: four
    # deliberate corruptions all "verified OK" through that path.
    return [_fat(json.loads(l)) for l in
            (path or REGISTRY).read_text(encoding="utf-8").splitlines() if l.strip()]


def regenerate(blocks) -> str:
    """Byte-exact reconstruction. Losslessness is a property of construction."""
    return "".join(b["raw"] for b in sorted(blocks, key=lambda b: b["seq"]))


def cmd_extract(args) -> int:
    src_path = Path(args.source) if args.source else SKILL
    text = src_path.read_text(encoding="utf-8")
    blocks, meta = parse(text)
    regen = regenerate(blocks)

    ok = regen == text
    print(f"source      : {src_path} ({len(text.encode()):,} B, {len(text.splitlines()):,} lines)")
    for k, v in meta.items():
        print(f"  {k:14} {v:>6,}")
    print(f"round-trip  : {'BYTE-EXACT' if ok else 'MISMATCH'}")
    if not ok:
        a, b = text, regen
        for n, (x, y) in enumerate(zip(a, b)):
            if x != y:
                print(f"  first diff at byte {n}: {a[max(0,n-60):n+60]!r} != {b[max(0,n-60):n+60]!r}")
                break
        print(f"  len {len(a):,} vs {len(b):,}")
        return 1

    if args.write:
        # SHRINK GUARD. After the 2026-08-18 cutover the corpus lives in the
        # registry and NOT in SKILL.md, so a bare `extract --write` re-parses a
        # thin skill and would overwrite ~2,235 checks with the handful that
        # remain inline. That is a bulk store rewrite that DROPS records
        # (archive-before-delete.md scope), and it is the single most likely
        # way this registry gets destroyed — by the very command that built it.
        if REGISTRY.exists() and not args.allow_shrink:
            prior = len(load_registry())
            if len(blocks) < prior * 0.9:
                print(
                    f"REFUSING to write: parse of {src_path} yields {len(blocks):,} blocks "
                    f"but {REGISTRY} already holds {prior:,}.\n"
                    f"  A >10% shrink means the source no longer carries the corpus.\n"
                    f"  Since the 2026-08-18 cutover SKILL.md is THIN by design — re-extracting\n"
                    f"  from it is not a rebuild, it is a deletion.\n"
                    f"  To rebuild from an archived full copy: --source <path> --write\n"
                    f"  To overwrite anyway (archive the registry FIRST): --allow-shrink",
                    file=sys.stderr,
                )
                return 1
        REGISTRY.parent.mkdir(parents=True, exist_ok=True)
        with REGISTRY.open("w", encoding="utf-8") as f:
            for b in blocks:
                f.write(json.dumps(_slim(b), ensure_ascii=False) + "\n")
        print(f"wrote       : {REGISTRY} ({REGISTRY.stat().st_size:,} B)")
    else:
        print("(dry run — pass --write to emit the registry)")
    return 0


def cmd_verify(args) -> int:
    """Round-trip the REGISTRY against the current SKILL.md corpus region."""
    if not REGISTRY.exists():
        print(f"FAIL: {REGISTRY} does not exist", file=sys.stderr)
        return 1
    blocks = load_registry()
    regen = regenerate(blocks)
    digest = hashlib.sha256(regen.encode()).hexdigest()
    meta_checks = sum(1 for b in blocks if b["kind"] == "check")
    meta_bash = sum(1 for b in blocks if b["kind"] == "bash_named")
    print(f"registry    : {len(blocks):,} blocks, {meta_checks:,} Check + {meta_bash:,} Bash(name) = {meta_checks+meta_bash:,}")
    print(f"regen sha256: {digest}")
    rc = 0

    # SELF-CONTAINED ROUND TRIP. Since the cutover there is no live file to
    # diff against, so `verify` with no --against used to print a digest and
    # exit 0 — a check that could not fail. The real invariant needs no
    # external file: regenerate the source text, re-parse it, and require the
    # block list to come back identical. Anything that corrupts a `raw`, a
    # `seq`, or the indentation that classifies a line breaks the fixed point.
    reparsed, _ = parse(regen)
    if reparsed == blocks:
        print("fixed point : OK (regenerate → parse reproduces every block)")
    else:
        rc = 1
        if len(reparsed) != len(blocks):
            print(f"fixed point : BROKEN — {len(reparsed):,} blocks back, expected {len(blocks):,}")
        else:
            print(f"fixed point : BROKEN — {len(blocks):,} blocks back but content diverged")
        for a, b in zip(blocks, reparsed):
            if a != b:
                keys = [k for k in a if a.get(k) != b.get(k)]
                print(f"  first divergence at seq {a['seq']}, field(s) {keys}:")
                for k in keys:
                    print(f"    registry {k!s:11} = {a.get(k)!r}")
                    print(f"    reparsed {k!s:11} = {b.get(k)!r}")
                break

    seqs = [b["seq"] for b in blocks]
    if seqs != sorted(set(seqs)):
        rc = 1
        print("seq         : BROKEN (not strictly increasing, or duplicated)")
    known = set(seqs)
    orphans = [b["seq"] for b in blocks
               if b.get("parent_seq") is not None and b["parent_seq"] not in known]
    if orphans:
        rc = 1
        print(f"parent_seq  : {len(orphans)} orphan(s), first at seq {orphans[0]}")

    if args.against:
        src = Path(args.against).read_text(encoding="utf-8")
        ok = regen == src
        print(f"vs {args.against}: {'BYTE-EXACT' if ok else 'MISMATCH'}")
        if not ok:
            rc = 1
    return rc


def cmd_count(args) -> int:
    blocks = load_registry()
    c = sum(1 for b in blocks if b["kind"] == "check")
    n = sum(1 for b in blocks if b["kind"] == "bash_named")
    print(json.dumps({"check": c, "bash_named": n, "corpus_checks": c + n,
                      "blocks": len(blocks)}, indent=1))
    return 0


def cmd_sections(args) -> int:
    """The index a run reads FIRST — small enough to always load whole."""
    blocks = load_registry()
    order, agg = [], {}
    for b in blocks:
        if b["kind"] not in ("check", "bash_named"):
            continue
        key = (b["step"], b["section"])
        if key not in agg:
            agg[key] = {"step": b["step"], "section": b["section"],
                        "check": 0, "bash": 0, "first_seq": b["seq"]}
            order.append(key)
        agg[key]["check" if b["kind"] == "check" else "bash"] += 1
    rows = [agg[k] for k in order]
    if args.output == "json":
        print(json.dumps({"sections": rows,
                          "total_checks": sum(r["check"] + r["bash"] for r in rows)}, indent=1))
        return 0
    print(f"{'section':<10} {'check':>6} {'bash':>5}  step")
    for r in rows:
        print(f"{str(r['section']):<10} {r['check']:>6} {r['bash']:>5}  {r['step']}")
    print(f"\n{len(rows)} slices, {sum(r['check']+r['bash'] for r in rows):,} checks total")
    return 0


def cmd_show(args) -> int:
    """Emit ONE slice verbatim — this is what makes the corpus loadable at all.

    The whole point of the extraction: a run loads the sections it needs instead
    of a 1.21 MB file it can only receive 5% of.
    """
    blocks = load_registry()
    sel = [b for b in blocks
           if (args.section is None or b["section"] == args.section)
           and (args.step is None or (b["step"] or "").startswith(args.step))]
    if not sel:
        print(f"no blocks match section={args.section!r} step={args.step!r}", file=sys.stderr)
        return 1
    if args.checks_only:
        sel = [b for b in sel if b["kind"] in ("check", "bash_named", "bash_bare", "body")]

    # PAGING. Measured 2026-08-18: section 4T alone regenerates to 224,352 B
    # against a 63,515 B skill-injection ceiling — 3.5x over. Section-level
    # granularity is NOT small enough on its own, and --checks-only only gets
    # 4T to 110,180 B (the `why` comments are 40% of the corpus and are the
    # part that explains why a check exists). So a slice has to be windowed, or
    # the extraction just relocates the truncation instead of removing it.
    total = len(sel)
    window, size = [], 0
    for b in sel[args.offset:]:
        n = len(b["raw"].encode()) + 1
        if window and size + n > args.max_bytes:
            break
        window.append(b)
        size += n

    sys.stdout.write(regenerate(window))

    # Continuation goes to STDERR so stdout stays byte-exact source text.
    nxt = args.offset + len(window)
    checks = sum(1 for b in window if b["kind"] in ("check", "bash_named"))
    if nxt < total:
        print(f"-- emitted blocks {args.offset}..{nxt - 1} of {total} ({size:,} B, "
              f"{checks} checks). MORE REMAIN: re-run with --offset {nxt}",
              file=sys.stderr)
    elif args.offset:
        print(f"-- emitted blocks {args.offset}..{nxt - 1} of {total} ({size:,} B, "
              f"{checks} checks). Slice complete.", file=sys.stderr)
    return 0


def cmd_add(args) -> int:
    """Insert a check into a section — the documented add path.

    `seq` is positional and `verify` requires it strictly increasing, so an
    insertion in the middle has to renumber everything after it. That is not
    something to ask a human to do by hand against a 1.8 MB JSONL, which is
    exactly how an "append a record with these fields" instruction turns into
    people appending at the end, or giving up and inlining the check again.
    """
    blocks = load_registry()
    idx = [i for i, b in enumerate(blocks) if b["section"] == args.section]
    if not idx:
        known = sorted({b["section"] for b in blocks if b["section"]})
        print(f"unknown section {args.section!r}. known: {', '.join(known)}", file=sys.stderr)
        return 1
    at = idx[-1] + 1  # insert after the section's last block

    # `raw` carries its OWN trailing newline — regenerate() joins the values
    # directly, which is what makes reconstruction byte-exact. Omitting it
    # concatenates the new lines into their predecessor; `verify` catches it,
    # but only because the newline is part of the record rather than the join.
    new = []
    if args.why:
        new.append({"kind": "why", "raw": "   # " + args.why + "\n"})

    # A row must land as the PARSER would have read it (). Two
    # failures compounded here: `Check: ` was prepended unconditionally, so
    # `Bash (name): cmd` became `Check: Bash (name): cmd` — a doubled prefix
    # CHECK_RE then matched — and `kind` was hardcoded "check", so even an
    # unprefixed bash line registered as k=check with i=None. Registered but
    # never executed is FALSE COVERAGE: corpus_checks rises while nothing runs,
    # which is worse than no check, because the instrument reports more
    # protection than exists.
    body = args.check.strip()
    if BASH_NAMED_RE.match(body) or BASH_BARE_RE.match(body):
        raw = "   " + body + "\n"   # already a record line — do not re-prefix
    else:
        raw = "   Check: " + body + "\n"
    kind, cid = _classify_record(raw)
    new.append({"kind": kind, "raw": raw, "id": cid})

    shift = len(new)
    base = blocks[at - 1]["seq"]
    for i, n in enumerate(new):
        # `id` is the bash_named capture from _classify_record — preserve it.
        # This used to hardcode None, which erased the id even when the kind
        # was right, so `verify`'s round trip could not reconstruct the line.
        n.update({"seq": base + 1 + i, "step": base and blocks[at - 1]["step"],
                  "section": args.section, "id": n.get("id"), "parent_seq": None})

    tail = blocks[at:]
    for b in tail:
        b["seq"] += shift
        if b.get("parent_seq") is not None:
            b["parent_seq"] += shift

    out = blocks[:at] + new + tail
    if args.dry_run:
        print(f"DRY RUN — would insert {shift} block(s) after seq {base} in section "
              f"{args.section}, shifting {len(tail):,} later block(s) by {shift}")
        for n in new:
            print(f"  + {n['raw']}")
        return 0

    REGISTRY.write_text(
        "".join(json.dumps(_slim(b), ensure_ascii=False) + "\n" for b in out),
        encoding="utf-8")
    c = sum(1 for b in out if b["kind"] in ("check", "bash_named"))
    print(f"added {shift} block(s) to section {args.section}; corpus_checks now {c:,} "
          f"({len(out):,} blocks). Run `verify` to confirm the round trip.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("extract", help="parse a full-corpus source; --write emits registry")
    e.add_argument("--write", action="store_true")
    e.add_argument("--source", help="parse this file instead of SKILL.md (an archived "
                                    "full copy — SKILL.md is thin since 2026-08-18)")
    e.add_argument("--allow-shrink", action="store_true",
                   help="bypass the shrink guard; ARCHIVE THE REGISTRY FIRST")
    e.set_defaults(fn=cmd_extract)

    v = sub.add_parser("verify", help="round-trip the registry")
    v.add_argument("--against", help="path to compare the regeneration against")
    v.set_defaults(fn=cmd_verify)

    c = sub.add_parser("count", help="registry counts as json")
    c.set_defaults(fn=cmd_count)

    a = sub.add_parser("add", help="insert a check into a section (the add path)")
    a.add_argument("--section", required=True, help="section code, e.g. C4")
    a.add_argument("--check", required=True, help="the check text, without the 'Check: ' prefix")
    a.add_argument("--why", help="optional '#' rationale comment placed above it")
    a.add_argument("--dry-run", action="store_true")
    a.set_defaults(fn=cmd_add)

    s = sub.add_parser("sections", help="the slice index a run reads first")
    s.add_argument("--output", choices=["text", "json"], default="text")
    s.set_defaults(fn=cmd_sections)

    sh = sub.add_parser("show", help="emit one slice verbatim, ready to execute")
    sh.add_argument("--section", help="section code, e.g. C4")
    sh.add_argument("--step", help="step-title prefix, e.g. 'Step 4'")
    sh.add_argument("--checks-only", action="store_true",
                    help="drop narrative/why lines, keep checks + commands")
    sh.add_argument("--offset", type=int, default=0,
                    help="start at this block index (paging; see stderr footer)")
    sh.add_argument("--max-bytes", type=int, default=40000,
                    help="cap the emitted window (default 40000, under the "
                         "63,515 B skill-injection ceiling)")
    sh.set_defaults(fn=cmd_show)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
