"""Spoken permission gate for agent-hotline.

Decides whether a caller's answer to a tool-use permission prompt is an
approval, a denial, or something unclear that should be asked again.

Safety rule: silence, a timeout, or non-speech never approves. Only an exact
"yes" approves.
"""

from __future__ import annotations

import string
from enum import Enum

from agent_hotline.ears import TranscriptResult

_DENY_WORDS = frozenset({"no", "deny", "nope", "stop"})
_STRIP_CHARS = string.punctuation + string.whitespace


class PermissionAnswer(Enum):
    APPROVE = "approve"
    DENY = "deny"
    UNCLEAR = "unclear"


def evaluate_answer(transcript: TranscriptResult | None) -> PermissionAnswer:
    # No transcript (timeout) or no speech at all: never approve by default.
    if transcript is None or not transcript.is_likely_speech:
        return PermissionAnswer.DENY

    normalized = transcript.text.strip(_STRIP_CHARS).lower()
    if normalized == "yes":
        return PermissionAnswer.APPROVE
    if normalized in _DENY_WORDS:
        return PermissionAnswer.DENY
    # Anything else (including clarifying questions) lets the caller be asked again.
    return PermissionAnswer.UNCLEAR
