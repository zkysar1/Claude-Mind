#!/usr/bin/env python3
"""Classify core/scripts/*.sh wrappers into daemon-migration buckets.

Re-runnable roadmap-hygiene tool. Produces the *current* bucket counts so the
daemon-migration tail can be quantified and the next wave prioritized, without
migrating anything. Origin: g-240-100 (BRD SS11.2 / HARDENING SS1).

Buckets (first-match precedence — a file lands in the FIRST bucket it matches):

  irreducibly-local   : carries the "IRREDUCIBLY LOCAL" annotation. An explicit
                        human decision that the wrapper must stay local forever
                        (per-Bash-call latency budget, hook hot path, shell->py
                        bridge). Highest precedence — overrides every other signal.
  daemon-aware        : already behind the daemon (sources _runtime.sh, or uses
                        rt_call / _no_daemon_error / rt_is_up). The client half of
                        the client/server split — ALREADY MIGRATED, not a candidate.
  domain-leak         : carries a domain-leak-exempt marker (intentional domain
                        strings). Out of generic-migration scope; the authoritative
                        detector is domain-leak-check.sh. Expected near-zero in
                        core/scripts (domain-free-examples rule).
  glue                : pure shell (no python invocation) OR a sourced helper
                        (basename starts with "_"). No daemon-relevant payload to move.
  migration-candidate : invokes python for real work, is NOT already daemon-aware,
                        NOT frozen-local, NOT glue. The actual unmigrated tail.

HARDENING SS1: these bucket counts are regression baselines. A >20% divergence
in any bucket between runs warrants an investigation (the wrapper population
shifted — either new wrappers landed unbucketed or a wave moved candidates).

Usage:
  py -3 core/scripts/daemon-migration-classify.py            # JSON: total + counts + per-bucket file lists
  py -3 core/scripts/daemon-migration-classify.py --counts   # JSON: total + counts only (compact)
  py -3 core/scripts/daemon-migration-classify.py --next-wave # prioritized migration-candidate list (text)
"""
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent          # .../core/scripts
PROJECT_ROOT = SCRIPT_DIR.parent.parent               # repo root
SKILLS_DIR = PROJECT_ROOT / ".claude" / "skills"
RULES_DIR = PROJECT_ROOT / ".claude" / "rules"

LOCAL_MARKER = "IRREDUCIBLY LOCAL"
DOMAIN_LEAK_MARKER = "domain-leak-exempt"
# Daemon-aware: sources the runtime client OR calls a runtime primitive.
DAEMON_MARKERS = ("_runtime.sh", "rt_call", "_no_daemon_error", "rt_is_up")
# Python invocation (real work that could become a daemon endpoint).
PY_INVOKE = re.compile(r"\bpython3\b|\bpy -3\b")

BUCKETS = ("irreducibly-local", "daemon-aware", "domain-leak", "glue", "migration-candidate")


def classify(text, name):
    """Return the bucket for a single wrapper. First-match precedence."""
    if LOCAL_MARKER in text:
        return "irreducibly-local"
    if any(m in text for m in DAEMON_MARKERS):
        return "daemon-aware"
    if DOMAIN_LEAK_MARKER in text:
        return "domain-leak"
    if not PY_INVOKE.search(text) or name.startswith("_"):
        return "glue"
    return "migration-candidate"


def py_payload_lines(text):
    """Heuristic: count lines that invoke python (proxy for daemon-able payload)."""
    return sum(1 for ln in text.splitlines() if PY_INVOKE.search(ln))


def build_ref_index():
    """Concatenate every SKILL.md + rule file once so candidate hot-path
    reference counts are a single in-memory substring scan (no subprocess)."""
    blob = []
    for base in (SKILLS_DIR, RULES_DIR):
        if not base.exists():
            continue
        for p in base.rglob("*.md"):
            try:
                blob.append(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
    return "\n".join(blob)


def scan():
    files = sorted((PROJECT_ROOT / "core" / "scripts").glob("*.sh"))
    buckets = {b: [] for b in BUCKETS}
    meta = {}  # name -> {bucket, py_lines}
    for p in files:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        b = classify(text, p.name)
        buckets[b].append(p.name)
        meta[p.name] = {"bucket": b, "py_lines": py_payload_lines(text)}
    return files, buckets, meta


def next_wave(buckets, meta, ref_blob):
    """Prioritize migration-candidates. Score rewards hot-path wrappers
    (referenced by many skills/rules — daemon caching helps them most) and
    python payload size (more work to move behind the daemon)."""
    out = []
    for name in buckets["migration-candidate"]:
        refs = ref_blob.count(name)
        py_lines = meta[name]["py_lines"]
        score = refs * 3 + min(py_lines, 10)
        out.append({
            "wrapper": name,
            "score": score,
            "skill_rule_refs": refs,
            "py_lines": py_lines,
            "rationale": f"refs={refs} py_lines={py_lines}",
        })
    out.sort(key=lambda e: (-e["score"], e["wrapper"]))
    return out


def main():
    args = set(sys.argv[1:])
    files, buckets, meta = scan()
    counts = {b: len(buckets[b]) for b in BUCKETS}
    result = {
        "total": len(files),
        "counts": counts,
        "regression_note": "HARDENING SS1: >20% divergence in any bucket between runs = investigate",
    }

    if "--next-wave" in args:
        wave = next_wave(buckets, meta, build_ref_index())
        print(f"Daemon-migration next-wave — {len(wave)} migration-candidate(s), highest value first:")
        print(f"(total {len(files)} wrappers; counts: " +
              ", ".join(f"{b}={counts[b]}" for b in BUCKETS) + ")")
        for i, e in enumerate(wave, 1):
            print(f"{i:3}. {e['wrapper']:<40} score={e['score']:<4} {e['rationale']}")
        return

    if "--counts" not in args:
        result["buckets"] = buckets
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
