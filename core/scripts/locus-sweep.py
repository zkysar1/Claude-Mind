#!/usr/bin/env python3
# domain-leak-exempt: LOCUS_RE literally enumerates this deployment's box and
# host names (cc-NN, LAPTOP-*, DESKTOP-*, roblox studio) because the whole point
# is to recognise them in free text. The terms are DATA powering the classifier,
# not engine logic.
"""Measure the LOCUS-bound share of the deferred-goal queue — read-only.

WHAT A LOCUS CONSTRAINT IS: a blocker about WHERE work can happen (a named box,
an OS, a live GUI session, a physical device) rather than WHEN. The goal record
has no host/capability field, so a where-constraint has exactly one writable
channel — `defer_reason`, a *when*-construct — and writing it there SUPPRESSES
the goal from every selector on every box, including the one box that could
satisfy it. The goal then waits not for a condition but for a coincidence.
Background: the "fourth axis" section of the routing-is-not-reachability tree
node, rb-8311, guard-4310.

═══ THE HEADLINE, AND THE REASON THIS SCRIPT REPORTS A BRACKET ═══

**The locus share cannot be derived by a matcher, and this script does not
pretend otherwise.** That was measured in BOTH directions, which is what makes
it a finding rather than an excuse:

  * LOOSEN and you admit PROVENANCE. The framework's own measurement discipline
    requires recording `hostname` and `uname -r` VERBATIM, so a well-documented
    defer CITES a box as the place a probe RAN — not as the thing blocking it.
    Measured: 13 goals matched a box name whose only role was provenance
    ("RE-PROBED 2026-08-14 (zeta, cc-02, uname -r 6.8.0-137-generic)"). The
    better the defer is documented, the more likely it false-positives — the
    same perverse gradient as guard-3882, where the best-probed defers are the
    ones most likely to trip the capability gate.

  * TIGHTEN and you drop GENUINE members. Requiring the locus to follow a
    blocking verb within 60 chars cut the count from 62 to 23, and reading the
    dropped set verbatim (guard-4031 — recall is read off what a narrowing
    DROPPED, never off the new predicate's own output) shows real locus blocks
    among the casualties. Verbatim: g-306-232 "genuine win32 Python runtime
    required (sys.platform==win32) — this box cc-04 reports linux". Unambiguous,
    and dropped, because the locus sits in a separate clause from the verb.

So the honest output is a BRACKET plus an ADJUDICATION BAND: a floor nobody can
argue with, a ceiling that is provably too generous, and the rows in between
listed verbatim for a human to label once. A single confident percentage here
would be false precision — and the 56% figure this script replaces was exactly
that: a loose keyword match reported as though it were a count.

THE REAL FIX IS STRUCTURAL, not a better regex: give LOCUS its own field. Once a
goal declares its locus, counting is exact and free, the selector can route on
it, and this script's whole classification layer becomes unnecessary. Twelve
goals have already invented the vocabulary unprompted (`studio_session_required`,
`ppe2_session_required`, `windows_box_required`, `post_deploy_session_required`,
`box-bounded`) — the field is being asked for; it just does not exist yet.

READ-ONLY BY CONSTRUCTION: opens nothing for writing, imports no mutation
helper, has no --apply. Same posture as the scar-tissue cadence — a sweep that
proposes, never acts, because most rows are lane-owned by another agent and
re-routing their work appropriates their queue.
"""

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# The suppression count is what makes a mis-channelled locus expensive, so it
# must agree with the thing doing the suppressing. gates/defer_classifier.py is
# the declared SSOT for the prefix list; a fourth copy here would drift.
from gates.defer_classifier import is_narrative_defer as _is_narrative_defer  # noqa: E402


def _load_population():
    """Reuse audit-deferred-defers.load_deferred() — do NOT re-derive it.

    That function already owns the population definition (world + every agent
    aspirations.jsonl, non-null defer_reason, TERMINAL_STATUSES excluded) and it
    routes through _paths.WORLD_DIR + agents_root(). A second definition here
    would drift from it silently, and the two counts would disagree with nothing
    to say which was right. Hyphenated filename, hence importlib.
    """
    path = SCRIPT_DIR / "audit-deferred-defers.py"
    spec = importlib.util.spec_from_file_location("_audit_deferred_defers", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.load_deferred()


# ── the three predicates, weakest to strongest ──────────────────────────────

# Any token that could NAME a locus. Deliberately generous: this is the CEILING.
LOCUS_RE = re.compile(
    r"(?:cc-\d+|LAPTOP-[A-Z0-9-]+|DESKTOP-[A-Z0-9-]+|roblox[- ]?studio|"
    r"windows(?:\s+box)?|linux box|owning box|the \w+ host|\bbox\b|\bmachine\b)",
    re.I,
)

# A goal that DECLARES its own locus requirement. This is the FLOOR: no reading
# of intent is involved, the author said it.
#
# THE VOCABULARY IS EMERGENT AND THIS LIST WILL GO STALE — which is an argument
# for the structural field, not for maintaining the list. Every token here was
# invented by an agent unprompted, and `host-bound` was found only by grepping
# the OTHER bands for declaration-shaped language after the first run. It had
# put g-326-338 ("the dev bridge is host-bound to the Studio machine") in
# provenance_only purely because the same text also says "MEASURED from cc-08" —
# a self-declared locus block demoted by its own probe citation. Re-run that
# grep across bands when the floor looks suspiciously flat.
DECLARED_RE = re.compile(
    r"\b\w*(?:box|studio|session|windows|host|machine)\w*_required\b"
    r"|\b(?:box|host|machine)-bound(?:ed)?\b",
    re.I,
)

_BLOCK_VERB = (r"(?:requires?|needs?|only on|not present on|unavailable on|absent on|"
               r"must run on|runs? only on|has to run on|not available on)")

# The locus follows a blocking verb closely enough to be its object. Stronger
# than co-occurrence (verb anywhere + locus anywhere), which conflates
# "requires <unrelated thing>" with "requires <this box>".
BLOCKING_RE = re.compile(_BLOCK_VERB + r"[^.;]{0,60}?" + LOCUS_RE.pattern, re.I)

# A probe/measurement CITATION — the box is where something was observed, which
# is the opposite of the box being what blocks.
PROVENANCE_RE = re.compile(
    r"(?:re-?probed|probed|measured|verified|confirmed|observed|checked|re-derived)\b"
    r"[^.;]{0,90}?(?:on|from|by|\()\s*[^.;]{0,40}?"
    r"(?:cc-\d+|LAPTOP-[A-Z0-9-]+|DESKTOP-[A-Z0-9-]+)",
    re.I,
)
UNAME_RE = re.compile(r"uname\s+-r", re.I)

# A locus token that names a PARTICULAR place, as opposed to LOCUS_RE's bare
# "box"/"machine". Splits the ceiling into its specific and generic halves so a
# reader can see how much of the loose bound is genuinely loose.
SPECIFIC_LOCUS_RE = re.compile(
    r"cc-\d+|LAPTOP-[A-Z0-9-]+|DESKTOP-[A-Z0-9-]+|roblox[- ]?studio|win32", re.I)


# ── which locus, and does THIS box satisfy it? ──────────────────────────────
#
# Counting locus-bound rows is only half the goal. The other half is per-box:
# "of the fleet's frozen work, what could I unfreeze right now?" That needs the
# locus EXTRACTED and evaluated against this box, not merely detected.
#
# A NAMED BOX HAS FOUR ROLES, and only the first is a locus block. This list is
# the measured output of building the thing, not a design sketch — roles 2 and 3
# were found in the corpus before writing it, and role 4 only by reading the
# rows the finished sweep claimed:
#
#   1. THE BLOCKING LOCUS   "measurement requires cc-04 local cache"
#   2. PROBE PROVENANCE     "RE-PROBED 2026-08-14 (zeta, cc-02, uname -r ...)"
#   3. AN EXCLUSION         "legacy .history dirs not present on cc-07/cc-09"
#   4. AVAILABLE CAPACITY   "cc-09/cc-10 are live agent-agnostic boxes, verified"
#
# Role 3 inverts the answer on exactly the boxes the author ruled out, so
# exclusion spans are subtracted before anything else. Role 4 is the one this
# layer CANNOT separate: verbatim, g-306-126 is a genuine `blocking` row that
# names cc-09 as the box that could FIX it, so on cc-09 it is truly worth
# reading — while the same row on cc-04 names cc-04 as the reducer, and is not.
# Same text, same band, opposite meanings, decided by which box is asking.
#
# So this half REPORTS CANDIDATES, it does not assert actionability. That is the
# same refusal the bracket makes upstream (a fifth regex would only move the
# error), and the same discipline the goal itself asked for: match the
# declarative head, accept under-matching, hand the row to a reader.
_NAMED_BOX_RE = re.compile(
    r"\b(cc-\d+|LAPTOP-[A-Z0-9][A-Z0-9-]*|DESKTOP-[A-Z0-9][A-Z0-9-]*)\b")
_EXCLUSION_RE = re.compile(
    r"(?:not present on|not available on|not reachable from|unavailable on|"
    r"absent on|unreachable from|no longer on|not on|cannot[^.;]{0,25}from)"
    r"\s*([^.;]{0,60})", re.I)
# Deliberately NOT `\bnot\b`. An exclusion span legitimately carries a second
# negated clause — "not present on cc-07 and not on cc-09" captures the span
# "cc-07 and not on cc-09" — so matching bare "not" would skip a correct
# two-box exclusion. The hazard is the hyphenated negated NOUN ("non-Studio"),
# which flips the span's meaning; a trailing clause does not.
_NEGATED_SPAN_RE = re.compile(r"\bnon-|\bwithout\b", re.I)
_STUDIO_RE = re.compile(
    r"roblox[- ]?studio|studio[_ -](?:host|machine|session|lane|bridge)|"
    r"\bstudio\w*_required\b", re.I)
_WINDOWS_RE = re.compile(r"\bwin32\b|\bwindows\b|windows\w*_required", re.I)


def extract_loci(text: str) -> dict:
    """-> {"required": [...], "excluded": [...]} of normalised locus tokens.

    Tokens are namespaced so the satisfaction rules below cannot confuse a
    hostname with a capability: `box:cc-04`, `os:windows`, `cap:roblox-studio`.
    """
    text = text or ""
    excluded = set()
    for span in _EXCLUSION_RE.findall(text):
        # EVERY token kind must be excludable, not just hostnames. Handling
        # role 3 for `box:` alone left `os:`/`cap:` inverted: "not available on
        # windows box" put os:windows in REQUIRED with an empty exclusion set,
        # so a Windows box read it as a candidate — the exact inversion this
        # branch exists to prevent, reintroduced one token-type over. Live in
        # the corpus (g-350-242, "...a non-Studio box").
        # A DOUBLE NEGATIVE re-inverts, so a negated span excludes NOTHING.
        # "cannot be satisfied from a non-Studio box" (g-350-242, verbatim) is a
        # REQUIREMENT for Studio; excluding cap:roblox-studio there would mark
        # the one capable box "elsewhere". Harmless today only because
        # _STUDIO_RE does not match "non-Studio box" — widen it and this fires.
        # Skip the span rather than parse the negation: under-match, per the
        # posture this whole file takes.
        if _NEGATED_SPAN_RE.search(span):
            continue
        for name in _NAMED_BOX_RE.findall(span):
            excluded.add("box:" + name)
        if _STUDIO_RE.search(span):
            excluded.add("cap:roblox-studio")
        if _WINDOWS_RE.search(span):
            excluded.add("os:windows")

    required = {"box:" + n for n in _NAMED_BOX_RE.findall(text)}
    if _STUDIO_RE.search(text):
        required.add("cap:roblox-studio")
    if _WINDOWS_RE.search(text):
        required.add("os:windows")
    return {"required": sorted(required - excluded), "excluded": sorted(excluded)}


def box_profile(hostname=None, platform=None) -> dict:
    """Short hostname + platform.

    The `.split('.')` is a guard against a CONFIDENT ZERO, not tidiness. Defers
    name boxes by their short name ("cc-04"), so on any box whose `node()`
    returns an FQDN every comparison would miss and the census would report
    zero candidates — which reads as "nothing frozen for me here" and is the
    one answer nobody re-checks. Measured on cc-09: `node()` is bare but
    `getfqdn()` is `cc-09.lxd`, so the FQDN form is one host config away.
    """
    import platform as _p
    import sys as _s
    host = hostname or _p.node() or "unknown"
    return {"hostname": host.split(".")[0], "platform": platform or _s.platform}


def satisfies(profile: dict, locus: str):
    """-> ("yes"|"no"|"unknown", why) for ONE locus token.

    `unknown` is a first-class answer and is used rather than guessed. A
    Windows box may or may not have Roblox Studio installed and open; nothing
    here can tell, and claiming `no` would hide real actionable work while
    claiming `yes` would hand a reader a goal it cannot run. Under-match and
    say so (reclaim-routed-work.md rule 7 / the goal's own design constraint).
    """
    host = (profile.get("hostname") or "").lower()
    plat = (profile.get("platform") or "").lower()
    is_windows = plat.startswith("win")

    if locus.startswith("box:"):
        want = locus[4:].lower()
        return ("yes", f"hostname {host} matches") if want == host \
            else ("no", f"hostname is {host}, not {want}")
    if locus == "os:windows":
        return ("yes", f"platform {plat}") if is_windows \
            else ("no", f"platform is {plat}")
    if locus == "cap:roblox-studio":
        # Studio does not run on Linux at all — that half IS decidable.
        if not is_windows:
            return ("no", f"Roblox Studio cannot run on {plat}")
        return ("unknown", "Windows box, but Studio install/session not probed here")
    return ("unknown", "unrecognised locus token")


def evaluate_here(profile: dict, loci: dict):
    """-> ("candidate"|"elsewhere"|"undeterminable"|"none", details).

    `candidate` means "a locus token here names something this box satisfies —
    READ this row", NEVER "this box can run it". Role 4 above is why: the same
    hostname can be the blocker or the remedy. Exclusion wins outright, because
    an author naming a box as the place the work canNOT happen is a stronger and
    more deliberate signal than any co-occurring hostname.
    """
    host = (profile.get("hostname") or "").lower()
    if any(x[4:].lower() == host for x in loci["excluded"] if x.startswith("box:")):
        return "elsewhere", ["this box is named in the defer's own exclusion set"]
    if not loci["required"]:
        return "none", []

    verdicts = [(l, *satisfies(profile, l)) for l in loci["required"]]
    if any(v == "yes" for _, v, _ in verdicts):
        return "candidate", [f"{l}: {w}" for l, v, w in verdicts if v == "yes"]
    if any(v == "unknown" for _, v, _ in verdicts):
        return "undeterminable", [f"{l}: {w}" for l, v, w in verdicts if v == "unknown"]
    return "elsewhere", [f"{l}: {w}" for l, v, w in verdicts]


def classify(defer_reason: str) -> str:
    """One of: declared | blocking | provenance_only | undecided | no_locus.

    Order matters and is deliberate. `declared` wins outright — an author who
    wrote `studio_session_required` has stated the constraint, and no citation
    elsewhere in the same text can demote that. `blocking` outranks
    `provenance_only` for the same reason: a defer may legitimately BOTH name
    its blocking locus AND cite where it was probed, and 34 rows do exactly
    that. Only a row whose sole locus signal is a citation is demoted.
    """
    text = defer_reason or ""
    if not text.strip():
        return "no_locus"
    if DECLARED_RE.search(text):
        return "declared"
    if not LOCUS_RE.search(text):
        return "no_locus"
    if BLOCKING_RE.search(text):
        return "blocking"
    if PROVENANCE_RE.search(text) or UNAME_RE.search(text):
        return "provenance_only"
    return "undecided"


# ── positive control (guard-2421) ───────────────────────────────────────────
# A classifier written this turn that returns "no_locus" for everything is
# well-formed, actionable-looking, and wrong. These fixtures are checked on
# EVERY run and the script exits 2 if any regresses — an empty band must mean
# "nothing matched", never "the reader is broken". Each case is a VERBATIM
# excerpt from the live corpus, not an invented string, so the control tests the
# shapes production actually contains (guard-920).
CONTROL = [
    ("precondition_unmet:studio_session_required — RE-PROBED 2026-08-15T20:00 "
     "(foxtrot, LAPTOP-3IOFCNEO, the Studio host).", "declared",
     "declared wins even when a probe citation follows it"),
    ("precondition_unmet: measure half requires Roblox Studio on Windows box (box-bounded)",
     "declared", "box-bounded is a declaration too"),
    ("precondition_unmet: measurement requires cc-04 local cache (legacy .history dirs "
     "not present on cc-07/cc-09)", "blocking", "locus is the object of 'requires'"),
    ("precondition_unmet: push-half measurement needed on cc-04 (the owning box)",
     "blocking", "'needed on <box>'"),
    ("precondition_unmet: live dev-lane read-back on the Studio host ... the dev bridge "
     "is host-bound to the Studio machine. MEASURED 2026-08-16T23:1x from cc-08",
     "declared",
     "a self-declared locus block must NOT be demoted by its own probe citation "
     "— this row sat in provenance_only until `host-bound` joined the floor"),
    ("precondition_unmet: meta/approval-reference-telemetry.jsonl still 0 entries. "
     "RE-PROBED 2026-08-14T13:15 (zeta, cc-02, uname -r 6.8.0-137-generic)",
     "provenance_only", "box is where the probe RAN — not a locus block"),
    ("precondition_unmet: 3 of 17 stranded files remain undispositioned",
     "no_locus", "no locus token at all"),
]


def run_control():
    """Return (ok, failures). Never mutates anything."""
    bad = []
    for text, want, why in CONTROL:
        got = classify(text)
        if got != want:
            bad.append({"expected": want, "got": got, "why": why, "text": text[:110]})
    return (not bad), bad


# The measurement this script was filed to re-take, quoted so the delta is
# self-describing. NOT a threshold and NOT a regression check: the queue is
# live, so goals close and new defers land between any two runs. It says only
# "measured the same way, N days apart" (audit-baselines.md posture).
BASELINE_2026_08_18 = {"population": 167, "suppressed": 150, "locus_naming": 93}


def sweep(profile=None):
    profile = profile or box_profile()
    recs = _load_population()
    bands = {k: [] for k in ("declared", "blocking", "provenance_only", "undecided", "no_locus")}
    here = {k: [] for k in ("candidate", "elsewhere", "undeterminable", "none")}
    suppressed = 0

    for r in recs:
        text = r.get("defer_reason") or ""
        # A structured head suppresses the goal from EVERY selector — the
        # mechanism that makes a mis-channelled locus expensive. Counted via the
        # gate's own SSOT rather than a fourth copy of the prefix list.
        #
        # The `text.strip()` is load-bearing despite being latently dead today
        # (measured: 0 of 166 empty). `is_narrative_defer` answers a WRITE-time
        # question — "does this write need the gate?" — and returns False for a
        # CLEAR so unblock paths need no override. Read as "is it suppressed?"
        # that False inverts: an empty defer suppresses nothing. Same predicate,
        # opposite meaning, because the caller's question changed.
        if text.strip() and not _is_narrative_defer("defer_reason", text):
            suppressed += 1

        loci = extract_loci(text)
        verdict, why = evaluate_here(profile, loci)
        row = {
            "goal_id": r.get("goal_id"),
            "agent": r.get("agent") or "world",
            "title": r.get("title"),
            "defer_reason": " ".join(text.split()),
            "band": classify(text),
            "loci": loci,
            "here": verdict,
            "here_why": why,
        }
        bands[row["band"]].append(row)
        here[verdict].append(row)

    pop = len(recs)
    floor = len(bands["declared"])
    specific = sum(1 for b, rows in bands.items() if b != "no_locus"
                   for r in rows if SPECIFIC_LOCUS_RE.search(r["defer_reason"]))
    # The ceiling is every row carrying a locus token at all, including the
    # citations we can show are NOT locus blocks. Reported precisely because it
    # is too generous — it is the number the 56% claim was.
    ceiling = pop - len(bands["no_locus"])
    return {
        "population": pop,
        "suppressed_from_every_selector": suppressed,
        "bracket": {"floor": floor, "ceiling": ceiling},
        "bracket_pct": {
            "floor": round(100.0 * floor / pop, 1) if pop else 0.0,
            "ceiling": round(100.0 * ceiling / pop, 1) if pop else 0.0,
        },
        "counts": {k: len(v) for k, v in bands.items()},
        # Stated as a COUNT, not implied by a gap between two other numbers.
        # This is the rows a stricter reading would have claimed and this one
        # declines to (the goal's third outcome).
        "under_matched": {
            "count": ceiling - floor,
            "meaning": ("rows carrying a locus token that the FLOOR deliberately "
                        "declines to claim, because their author never declared "
                        "the constraint — blocking + undecided + provenance_only"),
        },
        "this_box": {
            "profile": profile,
            "counts": {k: len(v) for k, v in here.items()},
            "candidates": [{"goal_id": r["goal_id"], "agent": r["agent"], "band": r["band"],
                            "why": r["here_why"], "title": r["title"]}
                           for r in here["candidate"]],
            "meaning": {
                "candidate": ("a locus token here names something this box satisfies "
                              "— READ the row. NOT a claim it can run here: a hostname "
                              "can equally name available capacity (role 4)."),
                "elsewhere": "locus named and this box provably does not satisfy it",
                "undeterminable": ("locus named, satisfaction not probeable from here "
                                   "(e.g. a GUI session) — deliberately not guessed"),
                "none": "no locus extracted; the defer is about WHEN, not WHERE",
            },
        },
        "baseline_2026_08_18": BASELINE_2026_08_18,
        "delta_vs_baseline": {
            "population": pop - BASELINE_2026_08_18["population"],
            "suppressed": suppressed - BASELINE_2026_08_18["suppressed"],
            "locus_naming_vs_ceiling": ceiling - BASELINE_2026_08_18["locus_naming"],
            "note": ("the live queue moves between runs; read this as 'measured the "
                     "same way, later', never as a regression signal. The baseline's "
                     "93 was a single loose keyword count — this run's comparable "
                     "figure is the CEILING, and the floor is the defensible one."),
        },
        # A ceiling is only readable if you know what it is made of. Without
        # this split "55%" invites the reading that half the queue names a real
        # machine, when some of it is the bare word "box" in unrelated prose
        # ("S1 claim falsified on this box"). Reported, not silently trimmed:
        # the ceiling is DEFINED as the generous bound, so tightening it here
        # would quietly turn the bracket into the point estimate this whole
        # script exists to refuse.
        "ceiling_composition": {
            "names_a_specific_locus": specific,
            "generic_word_only": ceiling - specific,
            "meaning": ("specific = a hostname, roblox-studio, or win32. generic = "
                        "matched only on the bare word box/machine/windows, which "
                        "is the loosest part of the loosest bound"),
        },
        "method": {
            "floor": "the goal DECLARES its locus (<token>_required / box-bounded)",
            "ceiling": "any locus token anywhere — provably includes probe-provenance citations",
            "adjudication_band": "blocking + undecided: needs one human labelling pass",
            "not_regex_derivable": (
                "loosening admits provenance citations; tightening drops genuine "
                "members (measured: g-306-232 'win32 runtime required — this box "
                "cc-04 reports linux'). Report the bracket, not a percentage."),
        },
        "bands": bands,
    }


def render_markdown(res: dict) -> str:
    b, p = res["bracket"], res["bracket_pct"]
    tb, prof = res["this_box"], res["this_box"]["profile"]
    out = [
        "# Locus sweep (read-only)",
        "",
        f"- population (defer-carrying, non-terminal): **{res['population']}**, of which "
        f"**{res['suppressed_from_every_selector']}** are suppressed from every selector",
        f"- locus-bound is between **{b['floor']} ({p['floor']}%)** and "
        f"**{b['ceiling']} ({p['ceiling']}%)**",
        f"- deliberately under-matched: **{res['under_matched']['count']}** "
        "(locus token present, author never declared the constraint)",
        f"- adjudication band: **{res['counts']['blocking'] + res['counts']['undecided']}** "
        "rows need one human labelling pass",
        "",
        f"## This box — `{prof['hostname']}` ({prof['platform']})",
        "",
        f"- **{tb['counts']['candidate']}** CANDIDATES here — a locus token this box "
        "satisfies. Read them; this is not a claim they can run here (a hostname "
        "can equally name available capacity).",
        f"- {tb['counts']['elsewhere']} bound elsewhere · "
        f"{tb['counts']['undeterminable']} undeterminable from here · "
        f"{tb['counts']['none']} carry no locus",
        "",
    ]
    for r in tb["candidates"][:15]:
        out.append(f"- ▸ `{r['goal_id']}` ({r['agent']}, {r['band']}) — "
                   f"{'; '.join(r['why'])}: {r['title']}")
    out += [
        "",
        "| band | n | meaning |",
        "|---|---|---|",
        f"| declared | {res['counts']['declared']} | author stated it — the floor |",
        f"| blocking | {res['counts']['blocking']} | locus is the object of a blocking verb |",
        f"| undecided | {res['counts']['undecided']} | locus token, no decidable role |",
        f"| provenance_only | {res['counts']['provenance_only']} | box is where a probe RAN — NOT locus-bound |",
        f"| no_locus | {res['counts']['no_locus']} | no locus token |",
        "",
        "The share is **not regex-derivable** — see `method.not_regex_derivable`. "
        "The structural fix is a locus FIELD on the goal record, after which this "
        "classification layer is unnecessary.",
        "",
        "## Adjudication band (verbatim)",
        "",
    ]
    for row in res["bands"]["blocking"] + res["bands"]["undecided"]:
        out.append(f"- `{row['goal_id']}` ({row['agent']}) — {row['defer_reason'][:200]}")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Read-only locus-bound defer census.")
    ap.add_argument("--output", choices=["json", "markdown"], default="json")
    ap.add_argument("--band", help="dump ONE band verbatim (declared|blocking|"
                                   "provenance_only|undecided|no_locus)")
    ap.add_argument("--skip-control", action="store_true",
                    help="skip the positive control (not recommended)")
    # Evaluate the population AS IF standing on another box. This is how the
    # goal's "run on two unlike boxes and confirm the sets DIFFER" check becomes
    # runnable from one box — and it is the stronger form: what that check is
    # really testing is whether the output depends on the box at all, which a
    # deterministic override falsifies in one command instead of one round trip.
    ap.add_argument("--box", help="override hostname (what would <box> see?)")
    ap.add_argument("--platform", help="override platform, e.g. win32/linux")
    # `bands` carries every row's full defer_reason: 199 KB of a 202 KB payload,
    # 98%. The precheck lane that calls this runs EVERY iteration and reads four
    # summary fields, so shipping bands by default would push ~200 KB into loop
    # context per iteration to deliver ~2.7 KB of answer. Opt-in, not default —
    # the same shape as guardrails-read --summary vs --active.
    ap.add_argument("--full", action="store_true",
                    help="include every row's band membership in --output json "
                         "(large: ~200 KB). Default json is summary-only.")
    args = ap.parse_args(argv)

    if not args.skip_control:
        ok, bad = run_control()
        if not ok:
            print("FAIL: locus-sweep positive control regressed — an empty band would "
                  "mean the READER is broken, not that nothing matched (guard-2421):",
                  file=sys.stderr)
            for f in bad:
                print(f"  expected {f['expected']!r} got {f['got']!r} — {f['why']}\n"
                      f"    {f['text']}", file=sys.stderr)
            return 2

    res = sweep(box_profile(hostname=args.box, platform=args.platform))

    if args.band:
        if args.band not in res["bands"]:
            print(f"unknown band {args.band!r}; choose from {sorted(res['bands'])}",
                  file=sys.stderr)
            return 1
        for row in res["bands"][args.band]:
            print(f"{row['goal_id']}\t{row['agent']}\t{row['defer_reason'][:400]}")
        return 0

    if args.output == "markdown":
        print(render_markdown(res))
    else:
        if not args.full:
            res = {k: v for k, v in res.items() if k != "bands"}
            res["bands_omitted"] = ("per-row detail withheld to keep the "
                                    "per-iteration payload small; re-run with "
                                    "--full, or --band <name> for one band")
        print(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
