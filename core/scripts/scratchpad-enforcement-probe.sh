#!/usr/bin/env bash
# scratchpad-enforcement-probe.sh — re-probe the harness-scratchpad enforcement
# claims that .claude/rules/no-scratchpad.md makes ().
#
# WHY THIS EXISTS: the rule's Enforcement section drifted into asserting a
# control that did not exist in one deployment and denying one that did. Both
# directions are the same defect — a stale claim about a safety layer — and
# nothing re-probed it, so it went unnoticed until a session wrote 47 files
# into the scratchpad while the rule said that path was covered.
#
# It is deliberately a SCRIPT and not an inline check: the gap it caught was a
# PROMOTION gap, found on a downstream deployment. A probe that only runs here
# cannot detect the case it exists for. Run it on any Mind.
#
# Claims re-probed (numbered as in the goal's VERIFY list):
#   2. deny rules exist AND match a real SUFFIXED scratchpad path
#      (the original glob matched only the unsuffixed form and would have
#      missed every real path)
#   3. the deny set is not NARROWER than the hook's own match list
#   4. a Bash redirect into the scratchpad is refused by the L1 hook
#
# Exit 0 = every claim holds. Exit 1 = at least one drifted; read the output.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

fails=0
pass() { printf '  PASS  %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; fails=$((fails + 1)); }

echo "scratchpad-enforcement-probe (g-115-8761)"

# ---- Claims 2 + 3: the settings deny set --------------------------------
# Tested against REAL path shapes, including the numeric suffix the harness
# actually appends. A probe against the unsuffixed form passes vacuously and
# is exactly the mistake being guarded against.
settings_out=$(py -3 -c '
import json, fnmatch, re, sys, pathlib
p = pathlib.Path(".claude/settings.json")
if not p.exists():
    print("NO_SETTINGS"); sys.exit(0)
deny = json.loads(p.read_text(encoding="utf-8")).get("permissions", {}).get("deny", [])
def parts(rule):
    m = re.match(r"^([A-Za-z]+)\((.*)\)$", rule)
    return (m.group(1), m.group(2)) if m else (None, None)
# The hook (via _path_roots.is_harness_scratchpad) matches these four roots;
# the deny set must not be narrower than that.
shapes = {
    "tmp-suffixed":     "/tmp/claude-0/-proj/sid/scratchpad/f.txt",
    "tmp-plain":        "/tmp/claude/-proj/sid/scratchpad/f.txt",
    "vartmp-suffixed":  "/var/tmp/claude-3/-proj/sid/scratchpad/f.txt",
    "appdata-suffixed": "~/AppData/Local/Temp/claude-2/-proj/sid/scratchpad/f.txt",
}
need = {"Write", "Edit", "MultiEdit"}
for label, path in sorted(shapes.items()):
    got = {t for r in deny for t, g in [parts(r)] if g and fnmatch.fnmatch(path, g)}
    missing = sorted(need - got)
    print(("OK " if not missing else "MISS ") + label + ("" if not missing else " missing=" + ",".join(missing)))
# Negative control: a non-scratchpad temp path the framework legitimately
# writes (suite logs) must NOT be denied, or the globs are over-broad.
ctrl = "/tmp/ayoai-suite-run-agent/chunk-00.log"
hit = sorted({t for r in deny for t, g in [parts(r)] if g and fnmatch.fnmatch(ctrl, g)} & need)
print(("OK control-not-denied" if not hit else "OVERBROAD control denied by=" + ",".join(hit)))
' 2>&1)

if [ "$settings_out" = "NO_SETTINGS" ]; then
    fail "claims 2-3: .claude/settings.json not found"
else
    while IFS= read -r line; do
        case "$line" in
            OK\ *)   pass "claim 2/3: ${line#OK }" ;;
            MISS\ *) fail "claim 2/3: deny set NARROWER than the hook — ${line#MISS }" ;;
            OVERBROAD*) fail "claim 3: ${line}" ;;
            *)       fail "claim 2/3: unexpected probe output: $line" ;;
        esac
    done <<< "$settings_out"
fi

# ---- Claim 4: the Bash hook actually refuses a redirect ------------------
# READS must stay approved: the harness writes background-task output under
# the scratchpad and instructs the model to read it, so a read-side deny
# would break the harness rather than the anti-pattern.
probe_hook() {
    printf '%s' "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"$1\"}}" \
        | py -3 core/scripts/bash-path-resolution-hook.py 2>&1
}

deny_out=$(probe_hook 'echo x > /tmp/claude-0/-proj/sid/scratchpad/probe.txt')
case "$deny_out" in
    *'"permissionDecision": "deny"'*) pass "claim 4: Bash redirect into scratchpad is REFUSED" ;;
    *) fail "claim 4: Bash redirect into scratchpad was NOT refused (got: ${deny_out:-<empty>})" ;;
esac

read_out=$(probe_hook 'cat /tmp/claude-0/-proj/sid/scratchpad/probe.txt')
case "$read_out" in
    *'"permissionDecision": "deny"'*) fail "claim 4: READING the scratchpad was refused — the harness writes task output there" ;;
    *) pass "claim 4 control: reading the scratchpad is still approved" ;;
esac

ctrl_out=$(probe_hook 'echo x > /tmp/ayoai-suite-run-agent/chunk-99.log')
case "$ctrl_out" in
    *'"permissionDecision": "deny"'*) fail "claim 4 control: a non-scratchpad temp write was refused (over-broad)" ;;
    *) pass "claim 4 control: non-scratchpad temp write still approved" ;;
esac

# ---- Both hooks must share ONE predicate, or they will diverge -----------
for h in core/scripts/path-resolution-hook.py core/scripts/bash-path-resolution-hook.py; do
    if grep -q "is_harness_scratchpad" "$h" 2>/dev/null; then
        pass "shared predicate: $(basename "$h") uses is_harness_scratchpad"
    else
        fail "shared predicate: $(basename "$h") no longer uses is_harness_scratchpad"
    fi
done

echo
if [ "$fails" -eq 0 ]; then
    echo "scratchpad-enforcement-probe: all claims hold"
    exit 0
fi
echo "scratchpad-enforcement-probe: $fails claim(s) DRIFTED — .claude/rules/no-scratchpad.md"
echo "  Enforcement section may now describe a control that is not installed."
exit 1
