#!/usr/bin/env python3
# domain-leak-exempt: framework check tool — references reasoning-bank type enum + skill paths, no domain terms
"""verify-rb-type-parity.py — assert every reasoning-bank ``type`` prescription in
``.claude/skills/*/SKILL.md`` is within ``RB_VALID_TYPES``, read from the validator
source itself (parity, NOT a hardcoded list — expanding the enum there auto-updates
this check). Exit 0 = clean; exit 1 = drift found (offenders printed).

Guards the skill-vs-validator prescription-drift class (g-115-2048 / rb-3195). Canonical
incident: ``reflect-on-outcome`` prescribed ``type ("contrastive")`` (Step 2.6) and
``type ("counterfactual")`` (Step 2.6c) — both REJECTED by ``reasoning-bank-add.sh``'s
validator (enum {success, failure, user_provided}), both failed on their FIRST live
exercise ~2 months after authoring, because nothing asserted prescription ⊆ enum at
authoring/review time. This check catches that class at review time instead.

Invoked from verify-learning Section RBT. Run from the repo root (the invocation
context in verify-learning): paths below are repo-root-relative, matching the
Section CAS sibling convention.

SCOPING (deliberate): scans for ``type`` prescriptions within +/-18 lines of a
``reasoning-bank-add.sh`` mention, in three RB-unambiguous forms — paren
``type ("X")``, JSON ``"type":"X"``, and piped ``type: X | Y`` (2+ alternatives).
The single unquoted ``type: X`` form is intentionally NOT matched: it collides with
verification-block ``type: file_check`` lines (false positives). Invalid rb types are
authored in the paren/piped/JSON forms — which ARE covered — so the exclusion costs no
real coverage. A single unquoted ``type: badtype`` would slip through; documented here
per the no-silent-caps principle.

AUTHORING CAVEAT: this check cannot tell a real prescription from PROSE that quotes the
literal forms. When DOCUMENTING an invalid type near a ``reasoning-bank-add.sh`` mention
(e.g. in this check's own verify-learning Section RBT comment), write it as bare-quoted
prose (the type "X"), NOT as the paren form — else the documentation self-triggers the
check (observed during g-115-2048: the Section RBT comment's own incident description
tripped the check on its first run; fixed by rephrasing to bare-quoted prose).
"""
import re
import sys
import pathlib

VALIDATOR_SRC = pathlib.Path("core/scripts/reasoning-bank.py")
SKILLS_GLOB = pathlib.Path(".claude/skills")
ANCHOR = "reasoning-bank-add.sh"
WINDOW = 18


def read_valid_types():
    """Read RB_VALID_TYPES = {...} from the validator source (parity, not hardcoded)."""
    m = re.search(r"RB_VALID_TYPES\s*=\s*\{([^}]*)\}", VALIDATOR_SRC.read_text(encoding="utf-8"))
    return set(re.findall(r'"([a-z_]+)"', m.group(1))) if m else set()


def prescribed_types(line):
    """Extract rb type value(s) from the three RB-unambiguous prescription forms."""
    out = re.findall(r'type\s*\(\s*"([a-z_]+)"', line)          # paren:  type ("X")
    out += re.findall(r'"type"\s*:\s*"([a-z_]+)"', line)        # JSON:   "type":"X"
    piped = re.search(r"\btype:\s*([a-z_]+(?:\s*\|\s*[a-z_]+)+)", line)  # piped: type: X | Y
    if piped:
        out += [t.strip() for t in piped.group(1).split("|")]
    return out


def main():
    valid = read_valid_types()
    if not valid:
        print(f"FAIL: could not read RB_VALID_TYPES from {VALIDATOR_SRC}")
        return 1
    violations = []
    for path in sorted(SKILLS_GLOB.glob("*/SKILL.md")):
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        anchors = [i for i, line in enumerate(lines) if ANCHOR in line]
        seen = set()
        for i, line in enumerate(lines):
            if not any(abs(i - a) <= WINDOW for a in anchors):
                continue
            for t in prescribed_types(line):
                if t and t not in valid and (i, t) not in seen:
                    seen.add((i, t))
                    violations.append(f"{path.parent.name}/{path.name}:{i + 1}:{t}")
    if violations:
        print(f"FAIL: SKILL.md reasoning-bank type prescription(s) outside RB_VALID_TYPES {sorted(valid)}:")
        for v in violations:
            print(f"  {v}")
        print("  Fix: align the prescription to the enum. Analysis style (contrastive, "
              "counterfactual, etc.) belongs in tags/content, NOT the outcome-class `type` field.")
        return 1
    print(f"PASS: all SKILL.md reasoning-bank type prescriptions within RB_VALID_TYPES {sorted(valid)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
