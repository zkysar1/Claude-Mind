#!/usr/bin/env python3
"""Repair double-cp1252 mojibake in JSONL stores.

Pre-2026-04-19 writes on Windows decoded stdin as cp1252, then re-read those
strings and applied the same misdecoding again. Two rounds of round-trip
through `cp1252.encode(errors='surrogateescape') → bytes.decode('utf-8')`
undoes the corruption. Example: em-dash written as 0xE2 0x80 0x94 (UTF-8) was
stored as the 8-codepoint sequence
'\\u00c3\\u00a2\\u00e2\\u201a\\u00ac\\u00e2\\u20ac\\udc9d'; after repair it
becomes '\\u2014'.

Safety:
- Operates on JSON-loaded string values (structural keys and numbers are not
  touched). Every line must json.loads and json.dumps cleanly; if not, that
  line is preserved verbatim and counted as a skip.
- The unmojibake transform only accepts a result when the full two-round
  encode/decode chain succeeds AND contains no surrogate codepoints. Any
  UnicodeEncodeError / UnicodeDecodeError short-circuits and returns the
  input unchanged — legitimate single non-ASCII chars (trademark, euro,
  real em-dash) fail cp1252-encode-then-UTF-8-decode and are left alone.
- Writes a timestamped .bak-YYYY-MM-DDTHH-MM-SS copy before overwriting.
  The operation is also fully reversible through git history.
"""
import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path


def unmojibake(s: str) -> str:
    """Try up to 2 rounds of cp1252→UTF-8 reverse transform.

    Returns the earliest stable value that contains no surrogate codepoints.
    If any round raises UnicodeEncodeError/UnicodeDecodeError, returns the
    input unchanged (not a mojibake we recognize).
    """
    cur = s
    for _ in range(2):
        try:
            raw = cur.encode("cp1252", errors="surrogateescape")
            nxt = raw.decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return cur
        if nxt == cur:
            return cur
        if any(0xDC00 <= ord(c) <= 0xDFFF for c in nxt):
            # Surrogate leaked through — the bytes didn't round-trip as valid
            # UTF-8 (rare; happens if we over-applied). Reject this round.
            return cur
        cur = nxt
    return cur


def walk_fix(obj):
    """Recursively apply unmojibake to every string value in a JSON tree.

    Returns (fixed_obj, count_of_strings_changed).
    """
    if isinstance(obj, str):
        repaired = unmojibake(obj)
        return repaired, (1 if repaired != obj else 0)
    if isinstance(obj, list):
        out = []
        changed = 0
        for v in obj:
            fv, c = walk_fix(v)
            out.append(fv)
            changed += c
        return out, changed
    if isinstance(obj, dict):
        out = {}
        changed = 0
        for k, v in obj.items():
            # Keys in our schemas are ASCII identifiers — don't touch them.
            fv, c = walk_fix(v)
            out[k] = fv
            changed += c
        return out, changed
    return obj, 0


def process_file(path: Path, apply: bool):
    lines_total = 0
    lines_changed = 0
    lines_skipped_unparseable = 0
    strings_fixed = 0
    out_lines = []
    with path.open("r", encoding="utf-8") as f:
        for raw_line in f:
            lines_total += 1
            stripped = raw_line.rstrip("\n")
            if not stripped.strip():
                out_lines.append(raw_line)
                continue
            try:
                rec = json.loads(stripped)
            except json.JSONDecodeError:
                lines_skipped_unparseable += 1
                out_lines.append(raw_line)
                continue
            fixed, count = walk_fix(rec)
            if count:
                lines_changed += 1
                strings_fixed += count
                # Preserve trailing newline behavior
                new_line = json.dumps(fixed, ensure_ascii=True, sort_keys=False)
                out_lines.append(new_line + "\n")
            else:
                out_lines.append(raw_line)

    if apply and lines_changed:
        ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        backup = path.with_suffix(path.suffix + f".bak-{ts}")
        shutil.copy2(path, backup)
        with path.open("w", encoding="utf-8", newline="") as f:
            f.writelines(out_lines)
        backup_note = f"  backup: {backup.name}"
    else:
        backup_note = ""

    return {
        "path": str(path),
        "lines_total": lines_total,
        "lines_changed": lines_changed,
        "lines_skipped_unparseable": lines_skipped_unparseable,
        "strings_fixed": strings_fixed,
        "backup_note": backup_note,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help="JSONL files to repair")
    ap.add_argument("--apply", action="store_true", help="Write changes to disk (default is dry-run)")
    args = ap.parse_args()

    totals = {"lines_total": 0, "lines_changed": 0, "lines_skipped_unparseable": 0, "strings_fixed": 0}
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== mojibake-repair ({mode}) ===")
    for f in args.files:
        p = Path(f)
        if not p.is_file():
            print(f"SKIP  {f} (not a file)")
            continue
        r = process_file(p, args.apply)
        print(f"{r['path']}: {r['lines_changed']}/{r['lines_total']} lines changed, "
              f"{r['strings_fixed']} strings fixed, "
              f"{r['lines_skipped_unparseable']} unparseable{r['backup_note']}")
        for k in totals:
            totals[k] += r[k]
    print("---")
    print(f"TOTAL: {totals['lines_changed']}/{totals['lines_total']} lines changed, "
          f"{totals['strings_fixed']} strings fixed, "
          f"{totals['lines_skipped_unparseable']} unparseable")
    if not args.apply and totals["lines_changed"]:
        print("(dry-run — rerun with --apply to write changes)")


if __name__ == "__main__":
    main()
