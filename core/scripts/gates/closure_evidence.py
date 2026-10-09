"""Closure-evidence table: one measured row per verification outcome ().

THE DEFECT. A goal closed on narrative. Measured 2026-09-23 on a worker Body
(g-373-125, a PROD Unblock). Outcome 2 required a unit to land "within 30s of
the POST ... measured live". The note said "landed within ~5s", but the trail
shows the first check ran ~115 s after the POST, plus a `sleep 5`. The same
note cited a session-scratch file as "owncloud push OK". Session scratch is
machine-local (owncloud_sync._EXCLUDE_DIRS holds "sessions"), so no push could
have happened, and the store had no such key. The own-unit verify passed both.
The residual-work gate already catches outcomes a note DEFERS. Nothing caught
outcomes a note CLAIMS MET with no measured value behind them.

THE STRUCTURE (the checklist technique). The closure note carries one row per
verification outcome, and a blank line ends a row:

    OUTCOME <n>: MET - <measured value>. Source: <command + output excerpt |
        path | store key | sha | the two timestamps>
    OUTCOME <n>: NOT MET - <what is missing>; deferred to <goal-id>

<n> indexes goal.verification.outcomes, or outcomes_agent_leg when the note
closes a collaborative goal's agent leg ("agent-leg-complete"). An optional
parenthetical restatement may sit before the colon. The dash may be an em dash.

THE CHECKS ON A MET ROW. Each maps to a measured shape:
  evidence     The row carries a concrete token: a digit, a path, or a
               backticked command. A bare "MET." states a verdict, not a value.
  paths        Every path this box can resolve must exist where a reader would
               look. A governed path (world/, meta/, agents/) is checked in the
               STORE. A framework path, a directory or a machine-local path is
               checked on local disk. A path this box cannot resolve (a product
               repo, another host) is left alone, because a check it cannot run
               is not a refusal.
  store claim  A row that says the evidence is in the store (own-cloud, S3,
               "the store", backend-cat) needs the store to hold it. A
               machine-local path can never satisfy that. Bare "pushed" is not
               a store claim, because it usually means `git push`.
  timing       A row that answers an outcome carrying a time bound ("within
               30s"), or that states an approximate interval ("~5s"), cites at
               least two distinct timestamps (clock, ISO or epoch).
A row that asserts ABSENCE (removed, deleted, exists: false) is exempt from the
path check: its paths are meant to be gone (guard-2190, polarity).

A NOT MET row on a completed close must say "deferred to <goal-id>", and the
goal it names must be live (g-375-162). The residual-work gate asks only that
SOME carrier in the whole note be live, so one live carrier lifted every row:
measured 2026-10-08, a note whose outcome 3 deferred to a completed goal passed
it once outcome 4 named a live one. So each NOT MET row is checked on its own,
by the residual-work gate's own live set, when the caller passes
carrier_status. The goal itself never counts as its own carrier. A carrier
that cannot be looked up is a warning, never a refusal.

A STORED NOTE IS FIXED BY APPENDING A ROW. The closure-note writer never
overwrites a note, so a bad row stays on the record (zc-04, 2026-10-08: one
refusal, then 3.5 h without the hand-rebuilt note it asked for). A row headed
"OUTCOME <n> (corrected): ..." that comes LAST for its outcome replaces every
earlier row for it, so goal-field-append.sh can fix one row and leave the rest
of the note alone. remedy_lines() prints that command, and the command that
files a missing carrier as pending.

It never decides a close by itself. It returns a verdict. The CLI
(closure-evidence-gate.py) picks the note that will land, wires the store, and
owns the refusal, the override and the ledger. One module, one path, for every
agent and every Body: iteration-close.sh do_verify calls it before the status
write, and both the worker (Phase 4a) and the reducer (Phase 5) close through
there (guard-5132).

THE ADVISORY (g-375-52) rides the same path and never refuses. A note that
cites a file under the closer's per-session dir cites evidence no other box can
open, so scratch_citations() names each such paragraph that lacks the lines the
claim rests on, or whose note names no host. See its section at the end.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

# A row header: "OUTCOME 2", an optional "(restatement)", a REQUIRED separator
# (":", an em/en dash, or a spaced hyphen), then the status. Anchored at a line
# start after optional list/quote/bold markers. The separator is what tells a
# row from prose: measured on 182 completed notes, lines like "Outcome 1's
# decision ..." and "OUTCOME 5 RATCHET: ..." were read as rows when it was
# optional, and each became a false "malformed row" refusal.
ROW_RE = re.compile(
    r"^[ \t>*#-]*OUTCOME[ \t]+(\d+)\b\**[ \t]*(?:\([^\n]*?\))?\**"
    r"(?:[ \t]*[:\u2014\u2013]|[ \t]+-[ \t])[ \t]*(.*)$",
    re.IGNORECASE)
STATUS_RE = re.compile(r"^(NOT[ \t-]+MET|UNMET|MET)\b", re.IGNORECASE)
DEFERRED_RE = re.compile(r"\bdeferred\s+to\b[^\n]*?\bg-\d{1,4}-\d+\b", re.IGNORECASE)
AGENT_LEG_RE = re.compile(r"\bagent-leg-complete\b", re.IGNORECASE)
# "OUTCOME 4 (corrected): ...". Matched only in the header, before the
# separator, so a row whose TEXT says "(corrected" is not a corrected row.
CORRECTED_RE = re.compile(r"\(\s*corrected\b", re.IGNORECASE)
# A carrier id as a lookup needs it, lettered child kept: -b is a
# different goal from , and a narrower pattern silently looks up the
# parent (guard-2414). aspirations.py GOAL_ID_RE admits the child; the
# residual-work gate's own pattern still drops it ().
CARRIER_ID_RE = re.compile(r"\bg-\d{1,4}-\d+\b(?:-[a-z]\b)?")

EVIDENCE_RE = re.compile(
    r"\d|[/\\]|`|::|\.(?:py|sh|md|ya?ml|jsonl?|java|kts?|ts|js|lua|txt|log|toml)\b",
    re.IGNORECASE)
STORE_CLAIM_RE = re.compile(
    r"\b(?:own-?cloud|s3|backend-cat|the\s+store|store\s+(?:copy|key|object)|"
    r"in\s+store|uploaded|synced)\b", re.IGNORECASE)
ABSENCE_RE = re.compile(
    r"\b(?:removed|deleted|gone|absent|no\s+longer|does\s+not\s+exist|"
    r"doesn'?t\s+exist|not\s+found|no\s+such\s+file|retired|renamed|moved)\b|"
    r"exists:\s*false", re.IGNORECASE)

_UNIT = r"(?:ms|milliseconds?|s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?)"
TIME_BOUND_RE = re.compile(
    r"\b(?:within|under|less\s+than|no\s+more\s+than|at\s+most|in)\s*[<~]?\s*"
    r"\d+(?:\.\d+)?\s*" + _UNIT + r"\b|[<\u2264]=?\s*\d+(?:\.\d+)?\s*" + _UNIT + r"\b",
    re.IGNORECASE)
APPROX_INTERVAL_RE = re.compile(
    r"(?:~|\u2248|\babout\s+|\bapprox(?:\.|imately)?\s+|\broughly\s+|\baround\s+)"
    r"\s*\d+(?:\.\d+)?\s*" + _UNIT + r"\b", re.IGNORECASE)
ISO_TS_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?")
CLOCK_TS_RE = re.compile(r"(?<![\d:])\d{1,2}:\d{2}:\d{2}(?:\.\d+)?Z?(?![\d:])")
EPOCH_TS_RE = re.compile(r"(?<![\w.])1\d{9}(?:\d{3})?(?:\.\d+)?(?!\w)")

URL_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)
PATH_RE = re.compile(r"(?:[A-Za-z]:)?[\\/]?(?:[\w.$<>{}~*+-]+[\\/])+[\w.$<>{}~*+-]*")
_PLACEHOLDER = ("...", "\u2026", "<", ">", "{", "}", "$", "*", "~")
FRAMEWORK_PREFIXES = ("core/", ".claude/", "mind_api/")


# ─── parsing ──────────────────────────────────────────────────────────────

#: The line that signs a worker Body's note, as _body_stamp.stamp_line writes it:
#: "Auto-signed: alpha worker Body 5af0e4c1, hostname zc-04." Every note writer
#: puts it after the text, so it can follow a row directly.
SIGNATURE_PREFIX = "Auto-signed:"


def _ends_row(line: str) -> bool:
    """A blank line, goal-field-append.sh's sentinel "[appended:<marker>]" (by
    that script's own predicate, is_block_boundary), or a Body's signature. The
    last two are not part of the row above them: their digits (a marker, a sid,
    a host) would otherwise pass a MET row's evidence check."""
    return (not line.strip() or line.startswith(SIGNATURE_PREFIX)
            or (line.startswith("[appended:") and line.rstrip().endswith("]")))


def parse_rows(note: str) -> Dict[str, list]:
    """Split a note into outcome rows. A row runs from its header to the next
    header or the first line that _ends_row(). `malformed` holds headers whose
    status is neither MET nor NOT MET: a PASS or a DONE must not slip past as a
    row this gate silently declines to check (sig-40, the weaker-predicate trap).

    A corrected row that comes LAST for its outcome supersedes every earlier
    entry for that outcome, malformed ones included. Those move to `superseded`,
    so every reader of `rows` sees the table as corrected (g-375-162)."""
    rows: List[dict] = []
    malformed: List[dict] = []
    order: List[dict] = []
    cur: Optional[dict] = None
    for line in (note or "").splitlines():
        m = ROW_RE.match(line)
        if m:
            rest = m.group(2).lstrip("*_ \t:\u2014\u2013-")
            sm = STATUS_RE.match(rest)
            if not sm:
                malformed.append({"n": int(m.group(1)), "header": line.strip()[:160]})
                order.append(malformed[-1])
                cur = None
                continue
            status = "MET" if sm.group(1).upper() == "MET" else "NOT MET"
            cur = {"n": int(m.group(1)), "status": status,
                   "corrected": bool(CORRECTED_RE.search(line[:m.start(2)])),
                   "header": line.strip(), "text": rest[sm.end():]}
            rows.append(cur)
            order.append(cur)
        elif _ends_row(line):
            cur = None
        elif cur is not None:
            cur["text"] += "\n" + line
    last = {e["n"]: e for e in order}
    superseded = [e for e in order if last[e["n"]] is not e and last[e["n"]].get("corrected")]
    gone = {id(e) for e in superseded}
    return {"rows": [r for r in rows if id(r) not in gone],
            "malformed": [m for m in malformed if id(m) not in gone],
            "superseded": superseded}


def deferred_carriers(text: str) -> List[str]:
    """The goal ids a row's "deferred to" names, in order: every id from that
    phrase to the end of the line its first id is on. [] without the phrase."""
    m = DEFERRED_RE.search(text)
    if not m:
        return []
    end = text.find("\n", m.end())
    out: List[str] = []
    for gid in CARRIER_ID_RE.findall(text[m.start():end if end >= 0 else len(text)].lower()):
        if gid not in out:
            out.append(gid)
    return out


def required_outcomes(goal: dict, note: str) -> Tuple[List[str], str]:
    """The outcomes this close must evidence, and which field they came from."""
    ver = goal.get("verification") or {}
    leg = ver.get("outcomes_agent_leg") or []
    if leg and "user" in (goal.get("participants") or []) and AGENT_LEG_RE.search(note or ""):
        return [_outcome_text(o) for o in leg], "outcomes_agent_leg"
    return [_outcome_text(o) for o in (ver.get("outcomes") or [])], "outcomes"


def _outcome_text(o) -> str:
    if isinstance(o, dict):
        return str(o.get("description") or o.get("text") or o.get("outcome") or o)
    return str(o)


def timestamps(text: str) -> List[str]:
    """Distinct timestamps in `text`: ISO, then clock, then epoch. Each match is
    masked before the next pattern runs, so an ISO stamp never counts twice."""
    found: List[str] = []
    for rx in (ISO_TS_RE, CLOCK_TS_RE, EPOCH_TS_RE):
        for m in rx.finditer(text):
            if m.group(0) not in found:
                found.append(m.group(0))
        text = rx.sub(lambda m: " " * len(m.group(0)), text)
    return found


def path_tokens(text: str) -> List[str]:
    """Concrete path-like tokens: URLs dropped, line/test suffixes and edge
    punctuation stripped, and placeholders (<agent>, $S, ..., *) skipped."""
    text = URL_RE.sub(" ", text)
    out: List[str] = []
    for m in PATH_RE.finditer(text):
        # A leading dot belongs to the path. strip() removed it, so ".claude/..."
        # became "claude/...", matched no FRAMEWORK_PREFIXES entry, and was never
        # checked, while "core/..." was.
        tok = m.group(0).lstrip(",;:)]}'\"!?").rstrip(".,;:)]}'\"!?")
        tok = re.sub(r"(?:::[\w\[\]-]+|#L\d+(?:-L?\d+)?|:\d+(?:[-:]\d+)*)$", "", tok)
        tok = tok.rstrip(".,;:")
        if len(tok.replace("/", "").replace("\\", "")) < 2 or any(p in tok for p in _PLACEHOLDER):
            continue
        if tok not in out:
            out.append(tok)
    return out


# ─── path classification ──────────────────────────────────────────────────

def _norm(p) -> str:
    return os.path.normcase(os.path.normpath(str(p))).replace("\\", "/")


def _under(p: Path, root: Optional[Path]) -> bool:
    if root is None:
        return False
    a, r = _norm(p), _norm(root).rstrip("/")
    return a == r or a.startswith(r + "/")


def classify(token: str, roots: Dict[str, Optional[Path]]) -> Tuple[str, Optional[Path]]:
    """-> ("governed" | "framework" | "unverifiable", absolute path or None).
    roots: project, world, meta, agents; "msys" true on a Windows box, where a
    /c/... path means C:/...  Governed roots are tried before the project root,
    because world/, meta/ and agents/ may sit inside it."""
    t = token.replace("\\", "/")
    if roots.get("msys") and re.match(r"^/[A-Za-z]/", t):
        t = t[1].upper() + ":" + t[2:]
    for prefix, key in (("world/", "world"), ("meta/", "meta"), ("agents/", "agents")):
        if t.startswith(prefix):
            base = roots.get(key)
            return ("governed", Path(base) / t[len(prefix):]) if base else ("unverifiable", None)
    if t.startswith(FRAMEWORK_PREFIXES):
        return "framework", Path(roots["project"]) / t
    # Absolute by the STRING, not Path.is_absolute(): on Windows a "/opt/..."
    # path has no drive and reads as relative, which misfiled a Linux Body's
    # governed path as unverifiable when checked from a Windows box.
    if t.startswith("/") or re.match(r"^[A-Za-z]:/", t):
        p = Path(t)
        for key in ("world", "meta", "agents"):
            if _under(p, roots.get(key)):
                return "governed", p
        if _under(p, roots.get("project")):
            return "framework", p
    return "unverifiable", None


def _local_probe(path: Path, kind: str) -> dict:
    """Default probe: local disk only (the LocalBackend view, where the store
    IS the disk). The CLI replaces it with a store-aware one."""
    exists = path.exists()
    return {"local": exists, "store": exists, "machine_local": False,
            "is_dir": path.is_dir()}


# ─── the verdict ──────────────────────────────────────────────────────────

def _check_paths(text: str, roots, probe, cache) -> Tuple[List[str], List[str]]:
    problems: List[str] = []
    warnings: List[str] = []
    store_claim = bool(STORE_CLAIM_RE.search(text))
    absence = bool(ABSENCE_RE.search(text))
    for tok in path_tokens(text):
        kind, p = classify(tok, roots)
        if kind == "unverifiable" or p is None:
            continue
        key = (_norm(p), kind)
        if key not in cache:
            try:
                cache[key] = probe(p, kind)
            except Exception as e:  # our own fault: never a refusal (guard-142)
                cache[key] = {"error": str(e)}
        info = cache[key]
        if "error" in info:
            warnings.append(f"{tok}: could not be probed ({info['error'][:80]})")
            continue
        miss = None
        if kind == "framework" or info.get("is_dir") or tok.endswith("/"):
            if not info.get("local"):
                miss = f"{tok} does not exist on this box"
        elif info.get("machine_local"):
            if store_claim:
                miss = (f"{tok} is machine-local (session scratch, temp, .history), so the "
                        f"store never receives it. The row's store claim cannot be true")
            elif not info.get("local"):
                miss = f"{tok} does not exist on this box"
        elif info.get("store") is True:
            pass
        elif info.get("store") is False:
            if not info.get("local"):
                miss = f"{tok} is in neither the store nor this box"
            elif store_claim:
                miss = f"{tok} is only on this box. The store does not hold it, but the row says it does"
            else:
                warnings.append(f"{tok} is only on this box (not in the store yet)")
        elif not info.get("local"):
            warnings.append(f"{tok}: store unreadable and not on this box")
        if miss:
            if absence:
                warnings.append(f"{miss} (row asserts absence, not refused)")
            else:
                problems.append(miss)
    return problems, warnings


def _check_carriers(body: str, own_id: str,
                    carrier_status: Callable[[str], Optional[str]]) -> Tuple[Optional[str], List[str]]:
    """-> (problem or None, warnings) for one NOT MET row's deferral. It refuses
    only when every goal the row defers to was looked up and none is live: a
    lookup that raised leaves the row unknown, and unknown is a warning."""
    try:
        from gates.residual_work import ACTIVE_STATUSES  # the live set has one definition
    except Exception as e:  # our own fault: never a refusal (guard-142)
        return None, [f"carrier liveness not checked ({str(e)[:80]})"]
    named: List[str] = []
    failed: List[str] = []
    for gid in deferred_carriers(body):
        if gid == own_id:
            named.append(f"{gid} (this goal)")
            continue
        try:
            status = carrier_status(gid)
        except Exception as e:
            failed.append(f"{gid} ({str(e)[:80]})")
            continue
        if status in ACTIVE_STATUSES:
            return None, []
        named.append(f"{gid} ({status or 'not found'})")
    if failed:
        return None, [f"carrier lookup failed, so its liveness is unchecked: {'; '.join(failed)}"]
    if not named:
        return None, []
    return (f"it defers to {', '.join(named)}, and none of them is live (pending or "
            f"in-progress), so nothing owns this gap. Name a live goal that does"), []


def evaluate(goal: dict, note: str, *,
             roots: Optional[Dict[str, Optional[Path]]] = None,
             probe: Optional[Callable[[Path, str], dict]] = None,
             carrier_status: Optional[Callable[[str], Optional[str]]] = None) -> dict:
    """Verdict for closing `goal` as completed with `note` as its closure note.

    decision: "noop" (nothing to evidence), "pass", or "block". Problems are
    per-row strings the refusal prints verbatim; warnings never refuse.
    carrier_status(goal_id) returns that goal's status, or None when no goal has
    the id, and may raise when it cannot look. Without it no carrier is looked
    up. `fix` lists each outcome the closer has to change, for remedy_lines()."""
    if goal.get("recurring"):
        return {"decision": "noop", "reason": "recurring goal (closes through complete-by, "
                "never status=completed)", "problems": [], "warnings": [], "rows": []}
    outcomes, field = required_outcomes(goal, note)
    if not outcomes:
        return {"decision": "noop", "reason": f"no {field} to evidence",
                "problems": [], "warnings": [], "rows": []}
    roots = roots or {"project": Path.cwd()}
    probe = probe or _local_probe
    parsed = parse_rows(note)
    problems: List[str] = []
    warnings: List[str] = []
    rows_out: List[dict] = []
    cache: dict = {}
    fix: Dict[int, dict] = {}

    def needs_fix(n: int, status: Optional[str] = None, carrier: bool = False) -> None:
        f = fix.setdefault(n, {"n": n, "status": None, "needs_carrier": False})
        f["status"] = status or f["status"]
        f["needs_carrier"] = f["needs_carrier"] or carrier

    if not parsed["rows"] and not parsed["malformed"]:
        problems.append(f"the note has no evidence table: {len(outcomes)} outcome(s) need "
                        f"one OUTCOME row each")
        for i in range(1, len(outcomes) + 1):
            needs_fix(i)
    for m in parsed["malformed"]:
        problems.append(f"OUTCOME {m['n']}: the status after the colon must be MET or "
                        f"NOT MET (got: {m['header'][:80]!r})")
        needs_fix(m["n"])

    by_n: Dict[int, List[dict]] = {}
    for r in parsed["rows"]:
        by_n.setdefault(r["n"], []).append(r)
    malformed_n = {m["n"] for m in parsed["malformed"]}
    for i, text in enumerate(outcomes, start=1):
        rows = by_n.get(i, [])
        if not rows:
            if parsed["rows"] and i not in malformed_n:
                problems.append(f"OUTCOME {i} has no row (\"{text[:70]}\")")
                needs_fix(i)
            continue
        if len(rows) > 1:
            problems.append(f"OUTCOME {i} has {len(rows)} rows. Write one")
            needs_fix(i, rows[-1]["status"])
            continue
        row = rows[0]
        row_problems: List[str] = []
        needs_carrier = False
        body = row["text"]
        if row["status"] == "NOT MET":
            if not DEFERRED_RE.search(body):
                row_problems.append("a NOT MET row on a completed close must say "
                                    "\"deferred to <goal-id>\" so the residual-work gate "
                                    "can require that goal to be live")
                needs_carrier = True
            elif carrier_status is not None:
                # Lowered like the ids deferred_carriers returns, so the goal never
                # passes as its own carrier.
                c_problem, c_warns = _check_carriers(body, str(goal.get("id") or "").lower(),
                                                     carrier_status)
                if c_problem:
                    row_problems.append(c_problem)
                    needs_carrier = True
                warnings += [f"OUTCOME {i}: {w}" for w in c_warns]
        else:
            if not EVIDENCE_RE.search(body):
                row_problems.append("MET with no measured value. Cite the value and its "
                                    "source (an output excerpt, a path, a sha, timestamps)")
            p_probs, p_warns = _check_paths(body, roots, probe, cache)
            row_problems += p_probs
            warnings += [f"OUTCOME {i}: {w}" for w in p_warns]
            bound = TIME_BOUND_RE.search(text)
            approx = APPROX_INTERVAL_RE.search(body)
            if bound or approx:
                ts = timestamps(body)
                if len(ts) < 2:
                    why = (f"outcome {i} sets a time bound ({bound.group(0).strip()!r})" if bound
                           else f"the row states an approximate interval ({approx.group(0).strip()!r})")
                    row_problems.append(
                        f"{why}, and the row cites {len(ts)} timestamp(s). Cite the two "
                        f"timestamps the interval was measured between (e.g. POST 08:50:29 -> "
                        f"first seen 08:52:24)")
        problems += [f"OUTCOME {i} ({row['status']}): {p}" for p in row_problems]
        rows_out.append({"n": i, "status": row["status"], "problems": row_problems,
                         "needs_carrier": needs_carrier})
        if row_problems:
            needs_fix(i, row["status"], needs_carrier)
    extra = sorted(n for n in by_n if n > len(outcomes) or n < 1)
    if extra:
        warnings.append(f"row(s) {extra} match no outcome ({len(outcomes)} in {field})")

    return {"decision": "block" if problems else "pass", "field": field,
            "required": len(outcomes), "rows": rows_out,
            "fix": [fix[n] for n in sorted(fix)],
            "problems": problems, "warnings": warnings}


FORMAT_HELP = (
    "  Format: one row per outcome, and a blank line ends a row.\n"
    "    OUTCOME <n>: MET - <measured value>. Source: <command + output excerpt | path |"
    " store key | sha | the two timestamps>\n"
    "    OUTCOME <n>: NOT MET - <what is missing>; deferred to <goal-id>\n")


#: goal-field-append's marker is its idempotency key, so each fix takes a new one.
FIX_MARKER = "closure-fix-"
_FIX_SENTINEL_RE = re.compile(r"^\[appended:" + re.escape(FIX_MARKER) + r"(\d+)\]", re.MULTILINE)


def remedy_lines(goal_id: str, result: dict, *, goal: Optional[dict] = None, note: str = "",
                 stored: bool = False, source: str = "world") -> List[str]:
    """The commands that fix a refused close, filled in for this goal ().

    Measured 2026-10-08 on zc-04: after one refusal a worker Body read framework
    source 14 times and took four tries over 85 minutes to file a carrier, and
    3.5 h later still had a 3,021-character note to rebuild by hand. So the
    refusal hands over both commands:
      - when a NOT MET row has no live carrier, the filing that makes one. Its
        origin is decomposition:<goal>, which intake files as pending, a live
        status;
      - when the note is the record's stored one, an append of one row per
        failing outcome. Nothing else in the note changes, so there is nothing to
        rebuild and no shrink for the daemon to refuse.
    Commands sit at column 0, where a here-document needs its closing word."""
    goal = goal or {}
    fix = result.get("fix") or []
    lines: List[str] = []
    if any(f.get("needs_carrier") for f in fix):
        m = re.match(r"^g-(\d+)-", goal_id)
        body = {"title": f"Residual: <what is missing> (from {goal_id})",
                "priority": goal.get("priority") or "MEDIUM", "participants": ["agent"],
                "category": goal.get("category") or "<category>",
                "origin_signal": f"decomposition:{goal_id}",
                "description": f"<what is missing>, left open by {goal_id}."}
        lines += ["  File the carrier first. It lands as pending, so it is live. Put the id it "
                  "prints in the row:",
                  f"bash core/scripts/aspirations-add-goal.sh --source {source} "
                  f"{'asp-' + m.group(1) if m else '<asp-id>'} <<'GOAL'",
                  json.dumps(body), "GOAL"]
    if stored and fix:
        parsed = parse_rows(note)
        had = {e["n"] for k in ("rows", "malformed", "superseded") for e in parsed[k]}
        k = 1 + max((int(x) for x in _FIX_SENTINEL_RE.findall(note or "")), default=0)
        lines += ["  Fix the stored note in place: this appends a row per failing outcome, and a "
                  "(corrected) row replaces the earlier rows for its outcome:",
                  f"bash core/scripts/goal-field-append.sh --source {source} {goal_id} "
                  f"outcome_note {FIX_MARKER}{k} --value-stdin <<'ROWS'"]
        for f in fix[:8]:
            head = f"OUTCOME {f['n']}" + (" (corrected)" if f["n"] in had else "")
            lines.append(f"{head}: NOT MET - <what is missing>; deferred to <live goal-id>"
                         if f.get("status") == "NOT MET" else
                         f"{head}: MET - <measured value>. Source: <command + output | path | sha>")
        lines.append("ROWS")
        if len(fix) > 8:
            lines.append(f"  Add outcome(s) {', '.join(str(f['n']) for f in fix[8:])} the same way.")
    return lines


def refusal_text(goal_id: str, result: dict, note_source: str,
                 remedy: Optional[List[str]] = None, stored: bool = False) -> str:
    """Short on purpose: a lesser model acts on the first screen of a refusal
    (the g-375-10 lesson). Names each failing row, the note that was read, the
    fix, and the one retry. The fix is remedy_lines(): for a stored note its
    append rows stand in for the format, unless an outcome's status is not
    known yet (no table, a missing row, a malformed one), when the rows show
    only the MET form and the closer still needs both."""
    lines = [f"closure-evidence-gate: REFUSED. {goal_id} status was NOT changed: the closure "
             f"note must show a measured value for each verification outcome (g-375-05)."]
    probs = result.get("problems") or []
    lines += [f"  - {p}" for p in probs[:8]]
    if len(probs) > 8:
        lines.append(f"  - ... and {len(probs) - 8} more")
    lines.append(f"  Note checked: {note_source}.")
    appends = bool(stored and remedy)
    if not appends or any(f.get("status") is None for f in result.get("fix") or []):
        lines.append(FORMAT_HELP.rstrip("\n"))
    lines += remedy or []
    # The file REPLACES the note, and the daemon refuses a replacement under 25% of
    # a note over 2000 chars (field_shrink.py) with an override iteration-close
    # does not forward. Rows ABOVE the current text never shrink it (guard-1532).
    rerun = ("Then re-run this same close as it was. Or rewrite the whole note: re-run it"
             if appends else "Then re-run this same close")
    lines.append(f"  {rerun} with --outcome-note-file <file>. The file REPLACES "
                 f"the record's note: put the rows first and keep the note's current text below "
                 f"them (read it: bash core/scripts/aspirations-query.sh --goal-field id {goal_id} "
                 f"--full). For a false refusal, add --override-closure-evidence \"<why>\" (audited).")
    return "\n".join(lines)


# ─── the session-scratch advisory () ──────────────────────────────
#
# A per-session dir (agents/<agent>/sessions/<SID>/) never syncs: its dirname is
# in owncloud_sync._EXCLUDE_DIRS. So a note that cites a suite log, probe output
# or census file there cites evidence that only the closing box can open. A
# reviewer on any other box has to re-derive the claim or record it unverified.
# Measured 2026-09-27: 14 of the first 39 close reviews flagged it, and
# guard-7485 is the behavioural rule: keep the path as a pointer, inline the
# lines it rests on, and name the host.
#
# This half is ADVISORY. It never refuses, because a closure can be sound and
# still cite a pointer; the cost of the gap falls on the reviewer, not the close.

# An excerpt is a backticked or quoted span of 12+ characters with a space in
# it: an output line, or a command. A lone name or a lone path has no space.
EXCERPT_RE = re.compile(r"`([^`\n]+)`|\"([^\"\n]+)\"|\u201c([^\u201d\n]+)\u201d")
# A host is named as "hostname <box>", "hostname: <box>" or "host=<box>". The
# CLI also passes this box's own hostname, which counts anywhere in the note.
# Not after a dash, so a command's --host=127.0.0.1 flag names no host.
HOST_RE = re.compile(r"(?<![\w-])hostname\b[ \t]*[:=]?[ \t]*[`\"(]?[A-Za-z0-9][\w.-]*"
                     r"|(?<![\w-])host[ \t]*[:=][ \t]*[`\"(]?[A-Za-z0-9][\w.-]*", re.IGNORECASE)
# \r too: a note written on Windows (or a CRLF summary on stdin) has "\r\n\r\n"
# between paragraphs, and without it the whole note reads as one paragraph.
PARAGRAPH_RE = re.compile(r"\n[ \t\r]*\n")


def session_path_re(sessions_dirname: str) -> "re.Pattern[str]":
    """A path that runs through <sessions_dirname>/<SID>/ into something below
    it. The SID must be hex, 8+ characters with dashes allowed, so a product
    repo's src/sessions/handlers/ does not match. path_tokens() has already
    dropped placeholders such as <SID>, so a note describing the convention
    does not match either."""
    return re.compile(r"(?:^|[\\/])" + re.escape(sessions_dirname)
                      + r"[\\/][0-9A-Fa-f]{8}[0-9A-Fa-f-]*[\\/][^\\/]")


def has_excerpt(text: str) -> bool:
    for m in EXCERPT_RE.finditer(text):
        span = next(g for g in m.groups() if g is not None).strip()
        if len(span) >= 12 and " " in span:
            return True
    return False


def names_host(note: str, hostname: str = "") -> bool:
    if HOST_RE.search(note):
        return True
    return bool(hostname) and re.search(
        r"(?<![\w.-])" + re.escape(hostname) + r"(?![\w-])", note, re.IGNORECASE) is not None


def scratch_citations(note: str, *, sessions_dirname: str, hostname: str = "") -> List[dict]:
    """One entry per paragraph (blank-line separated, 1-based) that cites a
    per-session path and is missing what a reviewer on another box needs:
    {"paragraph": n, "paths": [...], "missing": ["excerpt", "host"]}. The
    excerpt must sit in the same paragraph as the path it backs; the host may
    be named anywhere in the note. [] means nothing to advise."""
    sess = session_path_re(sessions_dirname)
    note = note or ""
    hosted = names_host(note, hostname)
    out: List[dict] = []
    for n, para in enumerate(PARAGRAPH_RE.split(note), start=1):
        paths = [t for t in path_tokens(para) if sess.search(t)]
        if not paths:
            continue
        missing = ([] if has_excerpt(para) else ["excerpt"]) + ([] if hosted else ["host"])
        if missing:
            out.append({"paragraph": n, "paths": paths, "missing": missing})
    return out


def advisory_text(goal_id: str, found: List[dict], hostname: str = "") -> str:
    """Short, like the refusal: where it fired, what is missing, the fix."""
    lines = [f"closure-evidence-gate: ADVISORY (g-375-52, never refuses). {goal_id}'s note cites "
             f"session-scratch evidence that no other box can open:"]
    for f in found[:6]:
        state = "no lines inline beside it" if "excerpt" in f["missing"] else "lines inline"
        lines.append(f"  - paragraph {f['paragraph']}: {', '.join(f['paths'][:2])} ({state})")
    if len(found) > 6:
        lines.append(f"  - ... and {len(found) - 6} more")
    if any("host" in f["missing"] for f in found):
        lines.append("  The note names no host.")
    lines.append(f"  Keep each path as a pointer. Beside it, quote in backticks the lines the claim "
                 f"rests on: the VERDICT or TOTAL line with its command, a count with its predicate, "
                 f"or the decisive probe output. Name the box as \"hostname {hostname or '<box>'}\" "
                 f"(guard-7485).")
    return "\n".join(lines)
