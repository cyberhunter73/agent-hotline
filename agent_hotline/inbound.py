"""Inbound caller-ID allowlist gate.

Both the stored allowlist entries and the incoming caller number are normalized to
the same canonical E.164 form before comparison, so formatting differences neither
reject a legitimate caller nor let a different number match.

Invalid-input policy (fail-closed):
- normalize_number raises ValueError for anything it cannot confidently turn into a
  real E.164 number: empty input, letters, stray characters, or too few / too many
  digits. It never guesses a partial number.
- InboundGate.is_allowed returns False for a caller number that fails normalization.
- InboundGate.__init__ raises ValueError for an invalid allowlist entry, so a typo in
  config fails loudly instead of silently dropping that caller.
"""

from __future__ import annotations

import re

# Characters allowed between digits in a phone number. Anything else (letters,
# punctuation like '#' or '*', extension markers) is rejected rather than stripped.
_SEPARATORS = re.compile(r"[\s()\-.]")

# E.164 allows at most 15 digits in total; 8 is a conservative floor for any real
# subscriber number once a country code is present.
_MIN_E164_DIGITS = 8
_MAX_E164_DIGITS = 15


def normalize_number(raw: str) -> str:
    """Return the canonical E.164 form of a phone number, e.g. "+15551234567".

    Formatting (spaces, parentheses, dashes, dots) is removed. A 10-digit number
    with no country code is assumed to be US (+1). An 11-digit number starting
    with 1 and no '+' is treated as US as well.

    Raises ValueError for anything that is not an unambiguous phone number.
    """
    if not isinstance(raw, str):
        raise ValueError(f"phone number must be a string, got {type(raw).__name__}")

    text = raw.strip()
    has_plus = text.startswith("+")
    body = text[1:] if has_plus else text

    digits = _SEPARATORS.sub("", body)
    if not digits or not digits.isdigit() or not digits.isascii():
        raise ValueError(f"not a phone number: {raw!r}")

    if has_plus:
        if not (_MIN_E164_DIGITS <= len(digits) <= _MAX_E164_DIGITS):
            raise ValueError(f"wrong digit count for an international number: {raw!r}")
        return "+" + digits

    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    raise ValueError(f"cannot determine country code for number: {raw!r}")


class InboundGate:
    """Admits a call only when the caller's number is on the allowlist."""

    def __init__(self, allowed_numbers: list[str]):
        # Normalize the stored entries up front, with the same function used on
        # incoming callers, so both sides are compared in one canonical form.
        self._allowed = frozenset(normalize_number(entry) for entry in allowed_numbers)

    def is_allowed(self, caller_number: str) -> bool:
        try:
            canonical = normalize_number(caller_number)
        except ValueError:
            return False
        return canonical in self._allowed
