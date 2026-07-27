"""Regression guard for : .claude/settings.json must have no
textually-duplicated JSON keys.

Root cause (g-115-2736 cluster): the v2.5.0 --living-prod promotion textually
merged three settings.json env blocks, appending the two keys common to all
three (CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY, DISABLE_AUTOUPDATER) three times
each. json.load() silently takes last-wins on duplicate object keys, so the
defect parsed cleanly and went undetected until a byte-level grep. There is NO
settings-merge stage in the seed-transplant path (the engine PRESERVES
.claude/settings.json under --living-prod, do_copy_staged); the duplication is
a source-file / textual-merge artifact, not a runtime merge bug. This test
parses with object_pairs_hook so a duplicate key at ANY nesting level fails
loudly instead of silently collapsing to last-wins.
"""
import json
from pathlib import Path

# test file lives at core/scripts/tests/ — repo root is three parents up.
REPO = Path(__file__).resolve().parents[3]
SETTINGS = REPO / ".claude" / "settings.json"


def test_settings_json_no_duplicate_keys():
    dups = []

    def hook(pairs):
        seen = set()
        for key, _val in pairs:
            if key in seen:
                dups.append(key)
            seen.add(key)
        return dict(pairs)

    raw = SETTINGS.read_text(encoding="utf-8")
    json.loads(raw, object_pairs_hook=hook)

    assert not dups, (
        f".claude/settings.json has duplicate JSON keys: {sorted(set(dups))}. "
        "json.load() keeps last-wins so this parses cleanly, but a duplicate key "
        "is a textual-merge defect (g-115-2741 — three env blocks concatenated "
        "during the v2.5.0 promotion). Dedup by upserting each key exactly once."
    )
