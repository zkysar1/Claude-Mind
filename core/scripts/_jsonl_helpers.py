"""Shared helpers for JSONL store modules (reasoning-bank, experience, pipeline, pattern-signatures).

Prior to this module, each store defined a near-identical `set_nested_field`
helper with a silent foot-gun: typo'd dot paths (e.g., `utilziation.count`
instead of `utilization.count`) created phantom parent sub-objects alongside
the real ones. The record still wrote and validated — silent data corruption.

The hardened version below refuses to create missing intermediate parents.
Callers MUST run `normalize_record(rec, DEFAULT_FIELDS)` first so any
declared nested structure already exists before set_nested_field is invoked.
Single-level paths (new top-level fields) remain unrestricted — the
`parts[:-1]` loop simply doesn't fire when there's no dot.

See g-240-30 / session 93 fresh-eyes F-phantom finding.
"""


def set_nested_field(obj, field_path, value):
    """Set a nested field via dot notation. REFUSES to create missing parents.

    Args:
        obj: The record dict to mutate.
        field_path: Dot-separated path (e.g., 'utilization.retrieval_count').
        value: The value to set at the terminal key.

    Raises:
        ValueError: If any intermediate parent doesn't exist, or isn't a dict.
                    The error message names the offending path so the typo is
                    obvious in logs.

    Behavior:
        - Single-level paths (no dot): always succeeds. Adds a new top-level
          field even if not declared in DEFAULT_FIELDS. This is the supported
          extension path for new fields (see g-240-24 experience_ref).
        - Multi-level paths: every ancestor must already exist as a dict.
          normalize_record() is expected to have pre-populated the declared
          structure before this helper is called.
    """
    parts = field_path.split(".")
    for i, part in enumerate(parts[:-1]):
        current_path = ".".join(parts[: i + 1])
        if part not in obj:
            raise ValueError(
                f"set_nested_field: parent '{current_path}' does not exist in record. "
                "Typo? Or add the field to DEFAULT_FIELDS so normalize_record backfills it."
            )
        if not isinstance(obj[part], dict):
            raise ValueError(
                f"set_nested_field: parent '{current_path}' is {type(obj[part]).__name__}, "
                "not a dict — cannot descend."
            )
        obj = obj[part]
    obj[parts[-1]] = value
