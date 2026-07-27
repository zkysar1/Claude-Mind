"""test_promotion_preflight_divergence.py —  Phase 2 content classifier.

Tests classify_divergence(): the VALUE-vs-COMMENT content classifier that Phase-1
(g-115-2867) identified as the missing reconcile-eligibility discriminator. A
PHENOTYPE-parametric file's diff can be pure comment/provenance drift (downstream
goal-id sanitization) with NO value change — reconciling that "up" would regress
the dev repo. The classifier parses both versions (YAML/JSON) and compares the
DATA: equal parsed data == comment/format-only (NOT reconcile-eligible), differing
parsed data == a real value change (reconcile-eligible).

Cases:
  1. COMMENT-only drift (the Phase-1 scenario: same values, goal-id stripped from a
     comment) -> kind=comment, reconcile_eligible=False.
  2. VALUE change (a threshold flipped) -> kind=value, reconcile_eligible=True.
  3. IDENTICAL bytes -> kind=identical, not eligible.
  4. STRUCTURAL zone (non-parametric) -> kind=structural (no value/comment split).
  5. UNPARSEABLE config -> kind=unparseable, conservative reconcile_eligible=True.
  6. JSON value change -> kind=value (JSON path).
  7. Whitespace/formatting-only YAML diff -> kind=comment (parsed data equal).
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "promotion_preflight", CORE_SCRIPTS / "promotion-preflight.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write(d: Path, name: str, text: str) -> Path:
    p = d / name
    p.write_text(text, encoding="utf-8")
    return p


def main() -> int:
    mod = _load()
    PARAM = mod.ZONE_PHENO_PARAM
    STRUCT = mod.ZONE_PHENO_STRUCT
    tmp = Path(tempfile.mkdtemp(prefix="preflight-divergence-test-"))
    failed: list[str] = []

    def check(name, cond):
        if not cond:
            failed.append(name)

    # Case 1: COMMENT-only drift — same values, goal-id stripped from a comment
    # (the exact Phase-1 finding: cognitive-horizons/meta/gates.yaml goal-id scrub).
    src1 = _write(tmp, "s1.yaml", "# BRD Gap 19 / g-306-02. Enumerates horizons.\nthreshold: 0.5\nwindow_days: 30\n")
    tgt1 = _write(tmp, "t1.yaml", "# BRD Gap 19 / . Enumerates horizons.\nthreshold: 0.5\nwindow_days: 30\n")
    r1 = mod.classify_divergence(src1, tgt1, PARAM)
    check("case1-kind-comment", r1["kind"] == "comment")
    check("case1-not-eligible", r1["reconcile_eligible"] is False)

    # Case 2: VALUE change — a threshold flipped (real config divergence).
    src2 = _write(tmp, "s2.yaml", "# same comment\nthreshold: 0.5\nwindow_days: 30\n")
    tgt2 = _write(tmp, "t2.yaml", "# same comment\nthreshold: 0.6\nwindow_days: 30\n")
    r2 = mod.classify_divergence(src2, tgt2, PARAM)
    check("case2-kind-value", r2["kind"] == "value")
    check("case2-eligible", r2["reconcile_eligible"] is True)

    # Case 3: IDENTICAL bytes.
    src3 = _write(tmp, "s3.yaml", "threshold: 0.5\n")
    tgt3 = _write(tmp, "t3.yaml", "threshold: 0.5\n")
    r3 = mod.classify_divergence(src3, tgt3, PARAM)
    check("case3-identical", r3["kind"] == "identical" and r3["reconcile_eligible"] is False)

    # Case 4: STRUCTURAL zone — no value/comment split.
    r4 = mod.classify_divergence(src2, tgt2, STRUCT)
    check("case4-structural", r4["kind"] == "structural")

    # Case 5: UNPARSEABLE config — conservative value-eligible (never drop a change).
    src5 = _write(tmp, "s5.yaml", "valid: 1\n")
    tgt5 = _write(tmp, "t5.yaml", "key: [unclosed\n  bad: : :\n")
    r5 = mod.classify_divergence(src5, tgt5, PARAM)
    check("case5-unparseable-eligible", r5["kind"] == "unparseable" and r5["reconcile_eligible"] is True)

    # Case 6: JSON value change (JSON parse path).
    src6 = _write(tmp, "s6.json", '{"threshold": 0.5, "n": 3}')
    tgt6 = _write(tmp, "t6.json", '{"threshold": 0.7, "n": 3}')
    r6 = mod.classify_divergence(src6, tgt6, PARAM)
    check("case6-json-value", r6["kind"] == "value" and r6["reconcile_eligible"] is True)

    # Case 7: whitespace/formatting-only YAML diff — parsed data equal -> comment.
    src7 = _write(tmp, "s7.yaml", "a: 1\nb: 2\n")
    tgt7 = _write(tmp, "t7.yaml", "a: 1\n\nb:   2\n")  # blank line + extra spaces
    r7 = mod.classify_divergence(src7, tgt7, PARAM)
    check("case7-formatting-comment", r7["kind"] == "comment" and r7["reconcile_eligible"] is False)

    # ── Phase 2 ENFORCEMENT helper: excuse_comment_only_blocks () ──
    # A parametric file blocks ONLY on comment/provenance drift -> excused.
    cd_comment = {"core/config/a.yaml": {"kind": "comment", "reconcile_eligible": False}}
    filt, exc = mod.excuse_comment_only_blocks(["core/config/a.yaml"], cd_comment)
    check("enf-comment-excused", filt == [] and exc == ["core/config/a.yaml"])

    # A VALUE-drift parametric file is NOT excused (real clobber risk).
    cd_value = {"core/config/a.yaml": {"kind": "value", "reconcile_eligible": True}}
    filt, exc = mod.excuse_comment_only_blocks(["core/config/a.yaml"], cd_value)
    check("enf-value-kept", filt == ["core/config/a.yaml"] and exc == [])

    # An UNPARSEABLE parametric file is NOT excused (conservative — never drop).
    cd_unp = {"core/config/a.yaml": {"kind": "unparseable", "reconcile_eligible": True}}
    filt, exc = mod.excuse_comment_only_blocks(["core/config/a.yaml"], cd_unp)
    check("enf-unparseable-kept", filt == ["core/config/a.yaml"] and exc == [])

    # An orphan / structural block (no content_divergence entry) is NEVER excused.
    filt, exc = mod.excuse_comment_only_blocks(["core/scripts/foo.sh"], {})
    check("enf-orphan-structural-kept", filt == ["core/scripts/foo.sh"] and exc == [])

    # Mixed: only the comment-drift member is excused; value + entryless stay.
    cd_mixed = {"core/config/c.yaml": {"kind": "comment", "reconcile_eligible": False},
                "core/config/v.yaml": {"kind": "value", "reconcile_eligible": True}}
    blk = ["core/config/c.yaml", "core/config/v.yaml", "core/scripts/orphan.sh"]
    filt, exc = mod.excuse_comment_only_blocks(blk, cd_mixed)
    check("enf-mixed", filt == ["core/config/v.yaml", "core/scripts/orphan.sh"]
          and exc == ["core/config/c.yaml"])

    # Empty blocking -> empty.
    filt, exc = mod.excuse_comment_only_blocks([], cd_comment)
    check("enf-empty", filt == [] and exc == [])

    # ── Phase 3b (): zone partition of the target-ahead blocking set ──
    # main() partitions ta_core_blocking by classify_zone into kernel_up_conflict /
    # structural_requires_review / param_reconcile_up (labeling only — all still
    # block). Verify classify_zone on representative paths + the exact 3-way group.
    check("zone-kernel-seedengine", mod.classify_zone("core/scripts/_seed_engine.py") == mod.ZONE_KERNEL)
    check("zone-kernel-manifest", mod.classify_zone("core/config/seed-manifest.yaml") == mod.ZONE_KERNEL)
    check("zone-kernel-seedskill", mod.classify_zone(".claude/skills/seed/SKILL.md") == mod.ZONE_KERNEL)
    check("zone-struct-script", mod.classify_zone("core/scripts/goal-selector.py") == mod.ZONE_PHENO_STRUCT)
    check("zone-struct-rule", mod.classify_zone(".claude/rules/foo.md") == mod.ZONE_PHENO_STRUCT)
    check("zone-param-config", mod.classify_zone("core/config/aspirations.yaml") == mod.ZONE_PHENO_PARAM)
    check("zone-niche-world", mod.classify_zone("world/knowledge/tree/x.md") == mod.ZONE_NICHE)

    _ta = ["core/config/seed-manifest.yaml",   # KERNEL
           "core/scripts/goal-selector.py",    # PHENOTYPE-structural
           ".claude/rules/foo.md",             # PHENOTYPE-structural
           "core/config/aspirations.yaml"]     # PHENOTYPE-parametric
    _zmap = {k: mod.classify_zone(k) for k in _ta}
    _kern = [k for k in _ta if _zmap.get(k) == mod.ZONE_KERNEL]
    _struct = [k for k in _ta if _zmap.get(k) == mod.ZONE_PHENO_STRUCT]
    _param = [k for k in _ta if _zmap.get(k) not in (mod.ZONE_KERNEL, mod.ZONE_PHENO_STRUCT)]
    check("p3b-kernel-group", _kern == ["core/config/seed-manifest.yaml"])
    check("p3b-struct-group", _struct == ["core/scripts/goal-selector.py", ".claude/rules/foo.md"])
    check("p3b-param-group", _param == ["core/config/aspirations.yaml"])
    # Exhaustive + mutually exclusive: every ta file lands in exactly one group.
    check("p3b-partition-exhaustive", sorted(_kern + _struct + _param) == sorted(_ta))
    check("p3b-partition-disjoint", len(_kern) + len(_struct) + len(_param) == len(_ta))

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

    if failed:
        for f in failed:
            print("FAIL:", f)
        return 1
    print("PASS: classify_divergence (comment-drift / value / identical / structural / "
          "unparseable / json-value / formatting) + excuse_comment_only_blocks enforcement")
    return 0


def test_promotion_preflight_divergence():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
