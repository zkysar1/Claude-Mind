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

A NOT MET row on a completed close must say "deferred to <goal-id>". That
phrase is a residual-work marker, so gates.residual_work then requires the
named goal to be live. This module does not re-implement that check.

It never decides a close by itself. It returns a verdict. The CLI
(closure-evidence-gate.py) picks the note that will land, wires the store, and
owns the refusal, the override and the ledger. One module, one path, for every
agent and every Body: iteration-close.sh do_verify calls it before the status
write, and both the worker (Phase 4a) and the reducer (Phase 5) close through
there (guard-5132).
"""
from __future__ import annotations

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

def parse_rows(note: str) -> Dict[str, list]:
    """Split a note into outcome rows. A row runs from its header to the next
    header or the first blank line. `malformed` holds headers whose status is
    neither MET nor NOT MET: a PASS or a DONE must not slip past as a row this
    gate silently declines to check (sig-40, the weaker-predicate trap)."""
    rows: List[dict] = []
    malformed: List[dict] = []
    cur: Optional[dict] = None
    for line in (note or "").splitlines():
        m = ROW_RE.match(line)
        if m:
            rest = m.group(2).lstrip("*_ \t:\u2014\u2013-")
            sm = STATUS_RE.match(rest)
            if not sm:
                malformed.append({"n": int(m.group(1)), "header": line.strip()[:160]})
                cur = None
                continue
            status = "MET" if sm.group(1).upper() == "MET" else "NOT MET"
            cur = {"n": int(m.group(1)), "status": status,
                   "header": line.strip(), "text": rest[sm.end():]}
            rows.append(cur)
        elif not line.strip():
            cur = None
        elif cur is not None:
            cur["text"] += "\n" + line
    return {"rows": rows, "malformed": malformed}


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


def evaluate(goal: dict, note: str, *,
             roots: Optional[Dict[str, Optional[Path]]] = None,
             probe: Optional[Callable[[Path, str], dict]] = None) -> dict:
    """Verdict for closing `goal` as completed with `note` as its closure note.

    decision: "noop" (nothing to evidence), "pass", or "block". Problems are
    per-row strings the refusal prints verbatim; warnings never refuse."""
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

    if not parsed["rows"] and not parsed["malformed"]:
        problems.append(f"the note has no evidence table: {len(outcomes)} outcome(s) need "
                        f"one OUTCOME row each")
    for m in parsed["malformed"]:
        problems.append(f"OUTCOME {m['n']}: the status after the colon must be MET or "
                        f"NOT MET (got: {m['header'][:80]!r})")

    by_n: Dict[int, List[dict]] = {}
    for r in parsed["rows"]:
        by_n.setdefault(r["n"], []).append(r)
    malformed_n = {m["n"] for m in parsed["malformed"]}
    for i, text in enumerate(outcomes, start=1):
        rows = by_n.get(i, [])
        if not rows:
            if parsed["rows"] and i not in malformed_n:
                problems.append(f"OUTCOME {i} has no row (\"{text[:70]}\")")
            continue
        if len(rows) > 1:
            problems.append(f"OUTCOME {i} has {len(rows)} rows. Write one")
            continue
        row = rows[0]
        row_problems: List[str] = []
        body = row["text"]
        if row["status"] == "NOT MET":
            if not DEFERRED_RE.search(body):
                row_problems.append("a NOT MET row on a completed close must say "
                                    "\"deferred to <goal-id>\" so the residual-work gate "
                                    "can require that goal to be live")
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
        rows_out.append({"n": i, "status": row["status"], "problems": row_problems})
    extra = sorted(n for n in by_n if n > len(outcomes) or n < 1)
    if extra:
        warnings.append(f"row(s) {extra} match no outcome ({len(outcomes)} in {field})")

    return {"decision": "block" if problems else "pass", "field": field,
            "required": len(outcomes), "rows": rows_out,
            "problems": problems, "warnings": warnings}


FORMAT_HELP = (
    "  Format: one row per outcome, and a blank line ends a row.\n"
    "    OUTCOME <n>: MET - <measured value>. Source: <command + output excerpt | path |"
    " store key | sha | the two timestamps>\n"
    "    OUTCOME <n>: NOT MET - <what is missing>; deferred to <goal-id>\n")


def refusal_text(goal_id: str, result: dict, note_source: str) -> str:
    """Short on purpose: a lesser model acts on the first screen of a refusal
    (the g-375-10 lesson). Names each failing row, the note that was read, the
    format, and the one retry."""
    lines = [f"closure-evidence-gate: REFUSED. {goal_id} status was NOT changed: the closure "
             f"note must show a measured value for each verification outcome (g-375-05)."]
    probs = result.get("problems") or []
    lines += [f"  - {p}" for p in probs[:8]]
    if len(probs) > 8:
        lines.append(f"  - ... and {len(probs) - 8} more")
    lines.append(f"  Note checked: {note_source}.")
    lines.append(FORMAT_HELP.rstrip("\n"))
    # The file REPLACES the note, and the daemon refuses a replacement under 25% of
    # a note over 2000 chars (field_shrink.py) with an override iteration-close
    # does not forward. Rows ABOVE the current text never shrink it (guard-1532).
    lines.append(f"  Then re-run this same close with --outcome-note-file <file>. The file REPLACES "
                 f"the record's note: put the rows first and keep the note's current text below "
                 f"them (read it: bash core/scripts/aspirations-query.sh --goal-field id {goal_id} "
                 f"--full). For a false refusal, add --override-closure-evidence \"<why>\" (audited).")
    return "\n".join(lines)
