"""Retired knowledge-tree node front-matter keys ().

A RETIRED key is one the framework no longer reads, so any copy left in a
node's front matter can only go stale. The set today is {"parent"}: a node's
parent lives in the _tree.yaml index (``nodes.<key>.parent``), which is what
every reader consults. The front-matter copy was hand-written, never updated
when a node moved, and so drifted from the index. Measured 2026-09-29 (alpha,
cc-08): 117 of 1,891 node files carried it, and no code path read it.

ONE implementation serves every caller, so they cannot disagree about what
"retired" means:
  - tree-front-matter-sync.py (T21) removes it on every Edit/Write of a node;
  - coordination_merge._reconcile_front_matter removes it from BOTH sides of a
    merge, so a stripped node and a stale copy that still carries the key
    reconcile instead of freezing on a key nobody reads;
  - tree-retired-fm-keys.py removes it from the store (--apply) and counts
    carriers (--check), the census a Bash writer needs, since Bash bypasses T21;
  - tree.py validate_tree warns on each carrier.

PURE: stdlib + yaml, no _paths import. coordination_merge is a pure merge
library and imports this lazily.

BYTE-MINIMAL: only the whole line(s) of a retired key are removed; every other
byte, line endings included, is left as it was. A removal is kept only when the
front matter re-parses to the original mapping minus exactly the removed keys.
Anything else raises RetiredKeyStripError, and the caller leaves the file alone.
"""
import re

import yaml

RETIRED_FM_KEYS = ("parent",)

# A TOP-LEVEL key only: anchored at column 0, so a nested `parent:` (inside
# last_update_trigger, say) is never touched.
_KEY_LINE_RE = re.compile(
    r"^(" + "|".join(re.escape(k) for k in RETIRED_FM_KEYS) + r")[ \t]*:(?=[ \t\r\n]|$)")


class RetiredKeyStripError(ValueError):
    """A retired key is present, but removing it could not be verified safe."""


def _lines(text):
    # Split after each "\n" only, keeping it. str.splitlines would also split on
    # \x85,   and friends, which the other front-matter readers do not.
    return [ln for ln in re.split(r"(?<=\n)", text) if ln]


def _front_matter_end(lines):
    """Index of the closing '---' line, or None when there is no front matter.

    Same boundaries as tree-front-matter-sync.py and coordination_merge: the
    document opens with a '---' line, and the block ends at the FIRST later line
    that starts with '---'."""
    if not lines or lines[0].rstrip("\r\n") != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].startswith("---"):
            return i
    return None


def _carrier_spans(lines, end):
    """(start, stop, key) for each top-level retired key inside lines[1:end].

    A key's span is its own line plus any continuation: indented lines, or
    column-0 '- ' items under an empty value. Blank lines are taken only BETWEEN
    continuation lines, never trailing, so a blank separator after the key stays."""
    spans = []
    i = 1
    while i < end:
        m = _KEY_LINE_RE.match(lines[i])
        if not m:
            i += 1
            continue
        empty_value = lines[i][m.end():].strip() == ""
        j = i + 1
        while j < end:
            ln = lines[j]
            if (ln[:1] in (" ", "\t") or not ln.strip()
                    or (empty_value and ln.startswith("- "))):
                j += 1
                continue
            break
        while j > i + 1 and not lines[j - 1].strip():
            j -= 1
        spans.append((i, j, m.group(1)))
        i = j
    return spans


def find_retired_fm_keys(text):
    """Retired keys present at the top level of text's leading front matter.

    Textual and cheap (no YAML parse), for census and detection."""
    lines = _lines(text)
    end = _front_matter_end(lines)
    if end is None:
        return []
    return list(dict.fromkeys(k for _s, _e, k in _carrier_spans(lines, end)))


def strip_retired_fm_keys(text):
    """Return (new_text, removed_keys).

    new_text is text with each top-level retired key's line(s) removed from the
    leading front matter; every other byte is unchanged. Returns (text, []) when
    there is no front matter or no retired key.

    Raises RetiredKeyStripError when a retired key is present but the removal
    cannot be verified: the front matter does not parse or is not a mapping, the
    stripped block would be empty, or it does not re-parse to the original
    mapping minus exactly the removed keys. Pinned by
    core/scripts/tests/test_tree_retired_fm_keys.py."""
    lines = _lines(text)
    end = _front_matter_end(lines)
    if end is None:
        return text, []
    spans = _carrier_spans(lines, end)
    if not spans:
        return text, []
    drop = set()
    for s, e, _k in spans:
        drop.update(range(s, e))
    removed = list(dict.fromkeys(k for _s, _e, k in spans))
    kept_fm = "".join(ln for i, ln in enumerate(lines[1:end], start=1) if i not in drop)
    if not kept_fm.strip():
        raise RetiredKeyStripError(
            "removing the retired key(s) would leave the front matter empty")
    try:
        before = yaml.safe_load("".join(lines[1:end]))
        after = yaml.safe_load(kept_fm)
    except yaml.YAMLError as e:
        raise RetiredKeyStripError(f"front matter does not parse: {e}") from None
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise RetiredKeyStripError("front matter is not a mapping")
    expected = {k: v for k, v in before.items() if k not in removed}
    if any(k not in before for k in removed) or after != expected:
        raise RetiredKeyStripError(
            "the stripped front matter does not re-parse to the original "
            "minus the retired key(s)")
    return "".join(ln for i, ln in enumerate(lines) if i not in drop), removed
