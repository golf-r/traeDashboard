"""Shared email validation + normalization helpers.

Lives in its own module to avoid import cycles between the API layer
(which imports the config writer for write-back) and the writer itself
(which validates the same addresses). Both sides must agree on what
counts as a valid email and how addresses are normalized (case +
whitespace), otherwise the writer could reject an address the API
accepted (or vice versa), surfacing as 400/422 after a successful save.
"""

from __future__ import annotations

import re

# Permissive email regex — local-part + "@" + domain. We do not aim for
# RFC 5322 completeness; the goal is to reject obvious typos while letting
# legitimate addresses through.
VALID_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def is_valid_email(addr: str | None) -> bool:
    """True iff ``addr`` matches the project's email regex."""
    if not addr:
        return False
    return bool(VALID_EMAIL.match(addr.strip()))


def normalize_email(addr: str | None) -> str:
    """Trim + lowercase. Returns ``""`` for None / empty / whitespace-only.

    Callers that need to skip empties (vs. reject them) can check the
    return value with ``if not normalized:`` — the writer uses this to
    silently drop blanks from a recipients list, while the API uses it
    to surface a 400 if any input was invalid.
    """
    if not addr:
        return ""
    return addr.strip().lower()