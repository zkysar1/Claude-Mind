#!/usr/bin/env python3
"""doc-retrievability-probe.py — is a doc FINDABLE for its own subject?

Companion to the `probe-doc-retrievability` forged skill (gap-095). Measures
whether a document/convention/store entry is returned by `retrieve.sh` when
queried by its subject, and — decisively — whether it is still returned when
the query avoids the target's OWN title and H2 tokens.

WHY THE SECOND PROBE IS THE POINT (guard-2820). A protocol that reads the
target first to confirm it is a good control has already contaminated the
phrasing, which is the independent variable. Measured 2026-08-06 on hypothesis
2026-07-30_convention-retrievability-independent-of-correctness: the
contaminated run gave 5/5 HIT (reads as CORRECTED) while de-contaminated
phrasing on the same files gave 1/3 MISS. The two runs disagreed on the
DIRECTION of the finding, not merely its size. So the contaminated number is a
FLOOR, never an estimate, and both must be reported.

What this script mechanizes that prose could not: the de-contamination check
itself. `--blind-query` is REFUSED when it shares a distinctive token with the
target's title or H2 headings, so "I avoided the title tokens" stops being an
honor-system claim and becomes a precondition the caller cannot skip.

SCORING is on FILE-PATH presence in the response, never on a token in the
returned body: retrieve.sh truncates bodies (guard-1834b), so a body-token
match is unreliable in a way that silently reads as a MISS.

Exit codes:
  0  probe ran; verdicts reported (read `blind.verdict` for the real signal)
  1  control failed — the target does not answer its own subject, so any
     retrievability verdict about it would be meaningless
  2  refused — bad input, or `--blind-query` is contaminated by title/H2 tokens
"""
import argparse
import json
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent

sys.path.insert(0, str(HERE))
from _runtime_bash import bash_cmd  # noqa: E402  (needs HERE on sys.path)

# Tokens too generic to carry contamination signal. Kept deliberately small:
# over-stopping defeats the check, and a false REFUSE is cheap (rephrase and
# re-run) while a false PROCEED silently reinstates the contaminated protocol
# this script exists to prevent.
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "before", "but", "by", "did",
    "do", "does", "for", "from", "has", "have", "how", "in", "is", "it", "its",
    "not", "of", "on", "or", "that", "the", "their", "then", "there", "this",
    "to", "was", "what", "when", "where", "which", "who", "why", "will",
    "with", "you", "your", "md", "the",
}

TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokens(text):
    """Distinctive lowercase tokens, stopwords and 1-char noise removed."""
    return {t for t in TOKEN_RE.findall((text or "").lower())
            if len(t) > 1 and t not in STOPWORDS}


def prose_only(raw):
    """Body text with front matter and ALL heading lines removed.

    The control check must not be able to satisfy itself: subject tokens are
    extracted FROM the title and H2 lines, so leaving those lines in the body
    makes coverage true by construction.
    """
    lines = raw.splitlines()
    out, in_fm = [], False
    for idx, line in enumerate(lines):
        if idx == 0 and line.strip() == "---":
            in_fm = True
            continue
        if in_fm:
            if line.strip() == "---":
                in_fm = False
            continue
        if line.lstrip().startswith("#"):
            continue
        out.append(line)
    return "\n".join(out)


def extract_subject(path):
    """Title + H2 headings of a markdown target.

    Title precedence: front-matter `name:`/`title:` -> first `# ` heading ->
    filename stem. H2 list is every `## ` heading. These are exactly the
    fields retrieve.sh surfaces as `title` and `tags`, so the contamination
    surface here matches the one the retriever indexes.
    """
    raw = path.read_text(encoding="utf-8", errors="replace")
    lines = raw.splitlines()
    title, h2 = None, []

    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() == "---":
                break
            m = re.match(r"^(name|title):\s*(.+)$", line.strip())
            if m and not title:
                title = m.group(2).strip().strip("\"'")

    for line in lines:
        if not title and line.startswith("# "):
            title = line[2:].strip()
        if line.startswith("## "):
            h2.append(line[3:].strip())

    if not title:
        title = path.stem

    return {"title": title, "h2": h2, "body_chars": len(raw), "raw": raw}


def run_retrieve(query, depth, include_framework, timeout):
    """Invoke the canonical retriever. Never re-implement its matching here."""
    # bash_cmd, never a bare "bash" argv[0] (guard-580 — resolves to the
    # System32 WSL stub on win32 and can hang forever) and never str(Path)
    # for the script (guard-581 — bash strips the backslashes of a
    # str(WindowsPath), silently yielding a nonexistent path).
    cmd = bash_cmd(HERE / "retrieve.sh",
                   "--category", query, "--depth", depth, "--read-only")
    if include_framework:
        cmd.append("--include-framework")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, cwd=str(PROJECT_ROOT))
    except subprocess.TimeoutExpired:
        return None, "retrieve.sh timed out after %ss" % timeout
    if proc.returncode != 0:
        return None, "retrieve.sh exit %d: %s" % (
            proc.returncode, (proc.stderr or "")[:200])
    # retrieve.sh can emit several concatenated documents; decode them all
    # rather than json.load (guard-2557: single-doc parsers die on "Extra data").
    dec, text, i, docs = json.JSONDecoder(), proc.stdout, 0, []
    while i < len(text):
        while i < len(text) and text[i] in " \t\r\n":
            i += 1
        if i >= len(text):
            break
        try:
            obj, end = dec.raw_decode(text, i)
        except ValueError:
            nl = text.find("\n", i)
            if nl < 0:
                break
            i = nl + 1
            continue
        docs.append(obj)
        i = end
    for d in docs:
        if isinstance(d, dict) and ("framework_rules" in d or "tree_nodes" in d):
            return d, None
    return None, "no retrieval document found in retrieve.sh output"


def score_presence(response, target_rel):
    """HIT iff the target's PATH appears in the response.

    Path-presence only — never a body token. Paths are compared on their
    normalised suffix so an absolute/relative or ./-prefixed form still
    matches the same file.
    """
    want = target_rel.replace("\\", "/").lstrip("./")
    paths, where = [], None
    for entry in (response.get("framework_rules") or []):
        paths.append(("framework_rules", (entry.get("path") or "")))
    for entry in (response.get("tree_nodes") or []):
        paths.append(("tree_nodes", (entry.get("file") or "")))
    for rank, (bucket, p) in enumerate(paths, start=1):
        norm = p.replace("\\", "/").lstrip("./")
        # `norm` must be non-empty. An entry whose path/file field is absent or
        # blank normalises to "", and `want.endswith("")` is unconditionally
        # True — so ONE field-less candidate scores HIT for EVERY target, at
        # rank 1, ahead of any real match. Narrowing a predicate behind a
        # POSITIVE verdict can only remove HITs, never admit new ones
        # (guard-1901); the excluded entries become MISS, which is the
        # conservative direction here because a MISS is reported depth-bounded
        # alongside `candidates_returned` while a HIT is not. Field-less
        # entries stay COUNTED in `candidates_returned` — the retriever did
        # return them; they just cannot evidence a match.
        if norm and (norm.endswith(want) or want.endswith(norm)):
            where = {"bucket": bucket, "rank": rank, "path": p}
            break
    return {
        "verdict": "HIT" if where else "MISS",
        "found_at": where,
        "candidates_returned": len(paths),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", required=True,
                    help="path to the doc under test (repo-relative or absolute)")
    ap.add_argument("--query", required=True,
                    help="natural phrasing a real caller would use")
    ap.add_argument("--blind-query",
                    help="de-contaminated phrasing avoiding title/H2 tokens "
                         "(REQUIRED unless --contaminated-floor-only)")
    ap.add_argument("--contaminated-floor-only", action="store_true",
                    help="report the contaminated probe alone and LABEL it a floor "
                         "(guard-2820: never an estimate)")
    ap.add_argument("--depth", default="shallow",
                    choices=["shallow", "medium", "deep"])
    ap.add_argument("--no-framework", action="store_true",
                    help="omit --include-framework (tree-only targets)")
    ap.add_argument("--max-age-minutes", type=float,
                    help="arm-discriminator mode: fail control if the target is "
                         "older than this (an already-indexed doc cannot discriminate)")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--response-fixture", action="append", default=[],
                    help="TEST SEAM: canned retrieval JSON, one per probe in order "
                         "(natural, then blind). Excludes the retrieve.sh call "
                         "itself from coverage — see guard-1462.")
    args = ap.parse_args()

    out = {
        "target": args.target,
        "box": {"hostname": platform.node(), "kernel": platform.release()},
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "depth": args.depth,
    }

    target = Path(args.target)
    if not target.is_absolute():
        target = PROJECT_ROOT / args.target
    if not target.is_file():
        out["error"] = "target not found: %s" % target
        print(json.dumps(out, indent=2))
        return 2

    if not args.blind_query and not args.contaminated_floor_only:
        out["error"] = ("--blind-query is required (guard-2820 makes the "
                        "de-contaminated re-probe mandatory). Pass "
                        "--contaminated-floor-only to report the floor alone.")
        print(json.dumps(out, indent=2))
        return 2

    subject = extract_subject(target)
    subject_tokens = tokens(subject["title"]) | tokens(" ".join(subject["h2"]))
    out["subject"] = {"title": subject["title"], "h2_count": len(subject["h2"]),
                      "distinctive_tokens": sorted(subject_tokens)}

    # ── CONTROL: does the target actually answer its own subject? ──
    # A probe against a doc that does not answer its subject measures nothing:
    # a MISS would be correct behaviour, not a retrieval defect.
    #
    # Compare against PROSE ONLY. Tokenizing the raw file would include the
    # heading lines the subject was extracted FROM, so coverage would be
    # satisfied by the heading's own existence and the control could never
    # fail — vacuous, and it read PASS on a deliberately-subjectless fixture
    # until the Step 3.6 dogfood run caught it (guard-1220: a check that
    # returns the same verdict on the PASS and FAIL fixtures has no
    # discriminating power).
    body_tokens = tokens(prose_only(subject["raw"]))
    covered = subject_tokens & body_tokens
    control = {
        "body_chars": subject["body_chars"],
        "subject_tokens_present_in_body": len(covered),
        "subject_tokens_total": len(subject_tokens),
    }
    control["passed"] = bool(subject["body_chars"] > 0 and subject_tokens
                             and len(covered) >= max(1, len(subject_tokens) // 2))

    if args.max_age_minutes is not None:
        age_min = (time.time() - target.stat().st_mtime) / 60.0
        control["age_minutes"] = round(age_min, 2)
        control["max_age_minutes"] = args.max_age_minutes
        if age_min > args.max_age_minutes:
            control["passed"] = False
            control["reason"] = (
                "target is %.1f min old (> %.1f): an already-indexed doc cannot "
                "discriminate, so an arm verdict from it is not measuring the arm"
                % (age_min, args.max_age_minutes))
    out["control"] = control
    if not control["passed"]:
        control.setdefault("reason", "target does not answer its own subject")
        print(json.dumps(out, indent=2))
        return 1

    # ── DE-CONTAMINATION CHECK: the mechanized half of guard-2820 ──
    if args.blind_query:
        leaked = tokens(args.blind_query) & subject_tokens
        out["decontamination"] = {
            "blind_query": args.blind_query,
            "leaked_tokens": sorted(leaked),
            "clean": not leaked,
        }
        if leaked:
            out["error"] = (
                "--blind-query is CONTAMINATED: it reuses the target's own "
                "title/H2 token(s) %s. That is the contaminated protocol wearing "
                "a blind label. Rephrase from the originating question and re-run."
                % sorted(leaked))
            print(json.dumps(out, indent=2))
            return 2

    fixtures = list(args.response_fixture)
    target_rel = str(target.relative_to(PROJECT_ROOT)) if str(target).startswith(
        str(PROJECT_ROOT)) else str(target)

    def probe(query, label):
        if fixtures:
            fx = fixtures.pop(0)
            resp = json.loads(Path(fx).read_text(encoding="utf-8"))
            err = None
            src = "fixture:%s" % fx
        else:
            resp, err = run_retrieve(query, args.depth, not args.no_framework,
                                     args.timeout)
            src = "retrieve.sh"
        if err:
            return {"query": query, "verdict": "ERROR", "error": err, "source": src}
        result = score_presence(resp, target_rel)
        result.update({"query": query, "source": src})
        return result

    out["natural"] = probe(args.query, "natural")
    out["natural"]["label"] = ("contaminated FLOOR — not an estimate (guard-2820)"
                               if not args.blind_query else "natural phrasing")

    if args.blind_query:
        out["blind"] = probe(args.blind_query, "blind")
        out["blind"]["label"] = "de-contaminated — THIS is the reportable signal"
        out["summary"] = "natural=%s blind=%s" % (
            out["natural"]["verdict"], out["blind"]["verdict"])
        out["disagree"] = out["natural"]["verdict"] != out["blind"]["verdict"]
        if out["disagree"]:
            out["note"] = ("the two probes DISAGREE — report both numbers; the "
                           "contaminated one is a floor (guard-2820)")
    else:
        out["summary"] = "natural=%s (floor only; no blind probe run)" % (
            out["natural"]["verdict"])

    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
