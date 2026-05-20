"""audit-helpers package — paired-data audit utilities.

See `core/config/conventions/paired-data-audits.md` for the rb-707 rule:
paired-data audits MUST emit (A − B) AND (B − A) separately, never net delta.
"""

from ._paired_diff import paired_diff

__all__ = ["paired_diff"]
