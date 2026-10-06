#!/usr/bin/env python3
"""Audit a carrier-ref merge of a one-record-per-line JSONL path BY RECORD.

`worker-ref-consume.sh --check` flags a carrier whose merge result differs from
HEAD by paired added and deleted lines (MIXED, +N / -M). A line count cannot say
what that pair is. It is a Body's snapshot of a moving ledger, a duplicate-line
collapse, a delete the Body made on purpose, a counter update, or a stale
rewrite that undoes live state (rb-12157) and raises no conflict. Telling them
apart takes the records themselves, so the reducer rebuilt the same audit by hand
on every occurrence. This module is that audit, and `--check` runs it beside the
MIXED line.

NOT merge_record_audit.py. That sibling audits a merge that already HAPPENED, across
every store with a handler in coordination_merge.py, and scores loss and backward
lifecycle moves. It has no handler for the experience or journal ledgers (which
carry the MIXED shape most often) and cannot take a merge-tree result, which is a
tree and not a commit. This module audits the PREVIEW of one carrier merge, path by
path, from the three blobs plus the merge base, for any one-record-per-line ledger.

INPUTS. Three blobs of ONE path, all read from git objects: PRE (HEAD, what main
holds), TIP (the Body's snapshot) and MERGED (the tree `git merge-tree` wrote,
which runs the repo's merge drivers: its blobs were compared with the blobs of
real merge commits and matched byte for byte). The merge BASE is read too when
one exists. It turns "the merge changed a record main holds" from a bare alarm
into a classification, because only the base shows who edited the record.

WHAT IT COUNTS. Audit what the merge loses, not what the tip lacks (rb-12672):
a tip that predates a record main added is expected and never counted.
    died      a record that was in PRE or TIP and is not in MERGED. Split by who
              caused it, so a delete the Body made on purpose does not read as a
              loss of main's work: `main_lost` (main's record vanished and
              nothing explains it), `tip_deleted` (main's copy was untouched
              since the base and the tip deleted it), `tip_only` (a record the
              tip added or edited never arrived).
    invented  a record in MERGED that neither input holds (a synthesised record,
              or an unparsable line such as a conflict marker).
    twins     a key that appears on more lines in MERGED than in either input
              (rb-12184: a record re-serialised on one side survives as a pair).
    changed   a record main holds that MERGED holds differently. With the base:
              `delivered` (only the Body edited it, the merge applied that edit:
              expected), `overwrote` (both sides edited it and the Body's copy
              won), `reverted` (main edited it, the Body did not, and the merge
              put the old copy back), `blended` (a third version). Without the
              base every Body-copy-won record is `took_tip` and none can be
              called delivered.
    drift     a changed record whose ONLY differing top-level keys are
              COUNTER_KEYS. Reported, never blocking.
Only `CLEARS` means died, invented, twins and content changes are all zero.
Anything else is `NOT CLEARED` and names the records. `UNMEASURED` means the
audit could not run (unreadable object, over budget): never read it as clear.

KEYING (per record, so one run handles any ledger):
  - a record with a scalar `id` is keyed by it;
  - a record with a `goals` list of id-bearing objects is split into one unit per
    goal (keyed by the goal id) plus the record without its goals, because the
    line holds every goal and a stale rewrite of it reverts them all (guard-6539);
  - anything else is keyed by its canonical JSON (sorted keys, no spaces), so a
    record that is only re-serialised reads as the same record, and an edited
    one reads as one died plus one invented.
A line that is not valid JSON is keyed by its bytes and counted as unparsed.

Multiplicity is not an edit: a duplicate line that the merge collapsed is
reported in the duplicate-line counts and is not a loss (guard-6539 measured
1080 such lines in one ledger). Only the DISTINCT versions of a record are
compared.

CLI (used by worker-ref-consume.sh):
    carrier_merge_audit.py --repo R --path P --pre REV --tip REV --merged REV
                           [--base REV] [--max-bytes N] [--json]
REV is any commit or tree. Without --base the merge base of PRE and TIP is used.
Exit 0 whenever a verdict was produced (UNMEASURED included); 2 on bad usage.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections import Counter

# Largest single blob (bytes) the audit reads. Above it the verdict is
# UNMEASURED with the size named: a skipped audit must never read as a clear one.
MAX_BLOB_BYTES = 64 * 1024 * 1024

# Top-level keys that are a usage counter every reader bumps, so two copies of one
# record routinely differ in nothing else (measured on eight real ledger merges: the
# changed experience records differed in this key alone). A difference confined to
# these keys is DRIFT. Any other key changing is CONTENT.
COUNTER_KEYS = frozenset({"retrieval_stats"})

SAMPLE_N = 5

# `base` argument of audit(): the merge base could not be determined. Distinct
# from None, which means "the base exists and the path is absent in it".
UNKNOWN = object()

_KIND_NAMES = {"id": "id", "goal": "goal id", "json": "canonical json", "raw": "UNPARSED line"}


class Unmeasured(Exception):
    """The audit could not run; the message becomes the UNMEASURED reason."""


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _scalar_id(value) -> bool:
    return isinstance(value, (str, int)) and not isinstance(value, bool)


def _sha(text) -> str:
    data = text if isinstance(text, bytes) else text.encode("utf-8", "replace")
    return hashlib.sha1(data).hexdigest()[:16]


def _clean(text, limit=60) -> str:
    """Printable ASCII only, one line: a sample label must never break the report."""
    out = text.encode("ascii", "backslashreplace").decode("ascii")
    out = "".join(ch if ch.isprintable() else " " for ch in out)
    return out[:limit]


class _Index:
    """One blob read as units: key -> the canonical record strings under that key."""

    def __init__(self, blob):
        self.recs = {}
        self.labels = {}
        self.kinds = Counter()
        self.lines = 0
        self.unparsed = 0
        if blob:
            for raw in blob.split(b"\n"):
                if raw.strip():
                    self.lines += 1
                    self._add_line(raw)

    def _put(self, key, record, label):
        self.recs.setdefault(key, []).append(record)
        self.labels.setdefault(key, label)
        self.kinds[key[0]] += 1

    @staticmethod
    def _record_key(obj):
        rid = obj.get("id") if isinstance(obj, dict) else None
        if _scalar_id(rid):
            return ("id", str(rid)), str(rid)
        canon = _canon(obj)
        return ("json", _sha(canon)), canon

    def _add_line(self, raw):
        try:
            obj = json.loads(raw)
        except ValueError:
            self.unparsed += 1
            text = raw.decode("utf-8", "replace").rstrip("\r")
            self._put(("raw", _sha(raw)), text, "unparsed: " + text)
            return
        if isinstance(obj, dict):
            goals = obj.get("goals")
            if isinstance(goals, list) and all(isinstance(g, dict) and _scalar_id(g.get("id")) for g in goals):
                shell = {k: v for k, v in obj.items() if k != "goals"}
                key, label = self._record_key(shell)
                self._put(key, _canon(shell), label)
                for g in goals:
                    self._put(("goal", str(g["id"])), _canon(g), str(g["id"]))
                return
        key, label = self._record_key(obj)
        self._put(key, _canon(obj), label)

    @property
    def units(self):
        return sum(len(v) for v in self.recs.values())

    @property
    def dup_lines(self):
        return self.units - len(self.recs)


def _same(a, b) -> bool:
    return a == b or set(a) == set(b)


def _fields(a, b):
    """Sorted top-level keys that differ between two single-record versions, else None."""
    try:
        if len(a) != 1 or len(b) != 1:
            return None
        oa, ob = json.loads(a[0]), json.loads(b[0])
    except ValueError:
        return None
    if not isinstance(oa, dict) or not isinstance(ob, dict):
        return None
    return sorted(k for k in set(oa) | set(ob) if oa.get(k) != ob.get(k))


def _sample(indexes, keys, with_fields=None):
    out = []
    for k in keys[:SAMPLE_N]:
        label = next((i.labels[k] for i in indexes if k in i.labels), k[1])
        text = _clean(label)
        if with_fields is not None and with_fields.get(k):
            text += "[" + ",".join(with_fields[k][:4]) + "]"
        out.append(text)
    return out


def audit(pre, tip, merged, base=UNKNOWN, path=""):
    """Audit one path. pre/tip/merged are bytes, or None when the path is absent
    in that revision. base is bytes, None (absent in the base) or UNKNOWN."""
    P, T, M = _Index(pre), _Index(tip), _Index(merged)
    B = None if base is UNKNOWN else _Index(base)

    died_main, died_tip_deleted, died_tip_only = [], [], []
    for k, rp in P.recs.items():
        if k in M.recs:
            continue
        if B is not None and k in B.recs and _same(rp, B.recs[k]) and k not in T.recs:
            died_tip_deleted.append(k)
        else:
            died_main.append(k)
    for k, rt in T.recs.items():
        if k in P.recs or k in M.recs:
            continue
        if B is not None and k in B.recs and _same(rt, B.recs[k]):
            continue  # main deleted it and the tip kept its untouched copy: the merge honoured main
        died_tip_only.append(k)

    invented = [k for k in M.recs if k not in P.recs and k not in T.recs]
    twins = [k for k, rm in M.recs.items()
             if len(rm) > max(len(P.recs.get(k, ())), len(T.recs.get(k, ())))]

    delivered = drift = 0
    content = {"overwrote": [], "reverted": [], "blended": [], "took_tip": []}
    field_map = {}
    for k, rm in M.recs.items():
        rp = P.recs.get(k)
        if rp is None or _same(rm, rp):
            continue
        rt = T.recs.get(k)
        if rt is None or not _same(rm, rt):
            cls = "blended"
        elif B is None:
            cls = "took_tip"
        else:
            rb = B.recs.get(k)
            if rb is not None and _same(rp, rb):
                cls = "delivered"
            elif rb is not None and _same(rt, rb):
                cls = "reverted"
            else:
                cls = "overwrote"
        if cls == "delivered":
            delivered += 1
            continue
        fields = _fields(rp, rm)
        if fields is not None and set(fields) <= COUNTER_KEYS:
            drift += 1
            continue
        field_map[k] = fields
        content[cls].append(k)
    n_content = sum(len(v) for v in content.values())

    reasons = []
    if died_main:
        reasons.append("%d record(s) main holds are missing from the merge result" % len(died_main))
    if died_tip_deleted:
        reasons.append("%d record(s) main still holds are deleted by the tip" % len(died_tip_deleted))
    if died_tip_only:
        reasons.append("%d record(s) the tip added or edited never arrive" % len(died_tip_only))
    if invented:
        reasons.append("%d record(s) in the merge result exist in neither input" % len(invented))
    if twins:
        reasons.append("%d record(s) appear on more lines than in either input" % len(twins))
    if n_content:
        reasons.append("%d of main's records are altered by the merge" % n_content)

    indexes = (P, T, M) if B is None else (P, T, M, B)
    kinds = Counter()
    for idx in (P, T, M):
        for kind, n in idx.kinds.items():
            kinds[kind] = max(kinds[kind], n)
    return {
        "path": path,
        "verdict": "NOT CLEARED" if reasons else "CLEARS",
        "reasons": reasons,
        "base": "unknown" if B is None else ("absent" if base is None else "known"),
        "lines": {"pre": P.lines, "tip": T.lines, "merged": M.lines,
                  **({} if B is None else {"base": B.lines})},
        "keys": {"pre": len(P.recs), "tip": len(T.recs), "merged": len(M.recs)},
        "dup_lines": {"pre": P.dup_lines, "tip": T.dup_lines, "merged": M.dup_lines},
        "unparsed": {"pre": P.unparsed, "tip": T.unparsed, "merged": M.unparsed},
        "keying": dict(sorted(kinds.items())),
        "died": {"main_lost": len(died_main), "tip_deleted": len(died_tip_deleted),
                 "tip_only": len(died_tip_only)},
        "invented": len(invented),
        "twins": len(twins),
        "changed": {"delivered": delivered, "drift": drift,
                    "content": {c: len(v) for c, v in content.items() if v}},
        "samples": {
            "main_lost": _sample(indexes, died_main),
            "tip_deleted": _sample(indexes, died_tip_deleted),
            "tip_only": _sample(indexes, died_tip_only),
            "invented": _sample(indexes, invented),
            "twins": _sample(indexes, twins),
            **{c: _sample(indexes, v, field_map) for c, v in content.items() if v},
        },
    }


def render(rep):
    """The report as text lines (ASCII only; the caller indents them)."""
    if rep["verdict"] == "UNMEASURED":
        return ["audit %s: UNMEASURED (%s)" % (rep["path"], "; ".join(rep["reasons"]))]
    ln, dl = rep["lines"], rep["dup_lines"]
    keyed = ", ".join("%d %s" % (n, _KIND_NAMES.get(k, k)) for k, n in rep["keying"].items()) or "no records"
    base_note = {"known": "base %d lines" % ln.get("base", 0),
                 "absent": "path absent in the merge base",
                 "unknown": "merge base UNKNOWN (a Body-only edit cannot be told from a stale overwrite)"}[rep["base"]]
    out = ["audit %s: %s" % (rep["path"], rep["verdict"]),
           "  keyed by %s; lines pre %d / tip %d / merged %d; %s"
           % (keyed, ln["pre"], ln["tip"], ln["merged"], base_note)]
    if any(dl.values()):
        out.append("  duplicate lines pre %d / tip %d / merged %d" % (dl["pre"], dl["tip"], dl["merged"]))
    if any(rep["unparsed"].values()):
        u = rep["unparsed"]
        out.append("  unparsed lines pre %d / tip %d / merged %d" % (u["pre"], u["tip"], u["merged"]))
    d, ch = rep["died"], rep["changed"]
    n_content = sum(ch["content"].values())
    out.append("  died %d (main-lost %d, tip-deleted %d, tip-only %d) | invented %d | twins %d"
               " | changed %d (delivered %d, drift %d, content %d)"
               % (sum(d.values()), d["main_lost"], d["tip_deleted"], d["tip_only"], rep["invented"], rep["twins"],
                  ch["delivered"] + ch["drift"] + n_content, ch["delivered"], ch["drift"], n_content))
    notes = {"main_lost": "died, main-lost",
             "tip_deleted": "died, tip-deleted (main's copy untouched since the base)",
             "tip_only": "died, tip-only", "invented": "invented", "twins": "twins",
             "overwrote": "changed, overwrote (both sides edited, the Body's copy won)",
             "reverted": "changed, reverted (main edited, the merge restored the old copy)",
             "blended": "changed, blended (a third version)",
             "took_tip": "changed, took the tip's copy (no base to classify)"}
    counts = {"main_lost": d["main_lost"], "tip_deleted": d["tip_deleted"], "tip_only": d["tip_only"],
              "invented": rep["invented"], "twins": rep["twins"], **ch["content"]}
    for name, text in notes.items():
        n, shown = counts.get(name, 0), rep["samples"].get(name) or []
        if n and shown:
            more = " (+%d more)" % (n - len(shown)) if n > len(shown) else ""
            out.append("  %s %d: %s%s" % (text, n, ", ".join(shown), more))
    if rep["reasons"]:
        out.append("  NOT CLEARED because: " + "; ".join(rep["reasons"]))
    return out


def _git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True)


def _read_blob(repo, rev, path, max_bytes):
    """Bytes of rev:path, or None when the revision resolves and the path is absent."""
    size = _git(repo, "cat-file", "-s", "%s:%s" % (rev, path))
    if size.returncode != 0:
        if _git(repo, "rev-parse", "--verify", "-q", "%s^{tree}" % rev).returncode == 0:
            return None
        raise Unmeasured("revision %s does not resolve" % rev)
    n = int(size.stdout.strip() or 0)
    if n > max_bytes:
        raise Unmeasured("%s:%s is %d bytes, over the %d-byte audit budget" % (rev[:12], path, n, max_bytes))
    blob = _git(repo, "cat-file", "blob", "%s:%s" % (rev, path))
    if blob.returncode != 0:
        raise Unmeasured("cannot read %s:%s" % (rev[:12], path))
    return blob.stdout


def audit_revisions(repo, path, pre, tip, merged, base=None, max_bytes=MAX_BLOB_BYTES):
    if base is None:
        found = _git(repo, "merge-base", pre, tip)
        base = found.stdout.decode().strip() if found.returncode == 0 else ""
    base_blob = _read_blob(repo, base, path, max_bytes) if base else UNKNOWN
    return audit(_read_blob(repo, pre, path, max_bytes), _read_blob(repo, tip, path, max_bytes),
                 _read_blob(repo, merged, path, max_bytes), base_blob, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Audit a carrier-ref merge of a JSONL path by record.")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--path", required=True)
    ap.add_argument("--pre", required=True)
    ap.add_argument("--tip", required=True)
    ap.add_argument("--merged", required=True)
    ap.add_argument("--base", default=None)
    ap.add_argument("--max-bytes", type=int, default=MAX_BLOB_BYTES)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        rep = audit_revisions(args.repo, args.path, args.pre, args.tip, args.merged, args.base, args.max_bytes)
    except Unmeasured as e:
        rep = {"path": args.path, "verdict": "UNMEASURED", "reasons": [str(e)]}
    except Exception as e:  # a crash is UNMEASURED: never a clear, never a traceback in the reducer's report
        rep = {"path": args.path, "verdict": "UNMEASURED",
               "reasons": ["%s: %s" % (type(e).__name__, _clean(str(e), 120))]}
    if args.json:
        print(json.dumps(rep, sort_keys=True))
    else:
        print("\n".join(render(rep)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
