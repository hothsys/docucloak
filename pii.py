"""High-precision PII detectors used by the redaction engine.

Detectors find candidate spans in plain text. They validate with checksums,
issuer ranges, numbering-plan rules and nearby labels, but always report the
raw span exactly as it appears so the redaction engine can remove it.

Values are never logged or returned to the UI: only spans move through the
redaction path, and only per-type counts reach the results list.
"""

from __future__ import annotations

from dataclasses import dataclass

# Stable ids: sent by the page, logged, and passed to worker processes.
EMAIL = 'email'
CREDIT_CARD = 'credit_card'
SSN = 'ssn'
PHONE = 'phone'

PII_TYPES = (EMAIL, CREDIT_CARD, SSN, PHONE)
DEFAULT_TYPES = (EMAIL, CREDIT_CARD, SSN)  # phone is noisier, so it is opt-in

LABELS = {
    EMAIL: 'email address',
    CREDIT_CARD: 'credit card number',
    SSN: 'Social Security number',
    PHONE: 'phone number',
}


@dataclass(frozen=True)
class Match:
    """One detected span in the source text; ``text[start:end]`` is sensitive."""
    start: int
    end: int
    kind: str


def find_pii(text: str, kinds=DEFAULT_TYPES) -> list[Match]:
    """Return the PII spans in *text* for the enabled *kinds*, sorted by start."""
    raise NotImplementedError
