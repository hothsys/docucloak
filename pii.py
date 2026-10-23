"""High-precision PII detectors used by the redaction engine.

Detectors find candidate spans in plain text. They validate with checksums,
issuer ranges, numbering-plan rules and nearby labels, but always report the
raw span exactly as it appears so the redaction engine can remove it.

Values are never logged or returned to the UI: only spans move through the
redaction path, and only per-type counts reach the results list.
"""

from __future__ import annotations

import re
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

# When spans overlap (for example, a phone-shaped email local part), keep the
# most specific interpretation rather than redacting a fragment of it.
_PRIORITY = {EMAIL: 4, CREDIT_CARD: 3, SSN: 2, PHONE: 1}

_EMAIL_RE = re.compile(
    r'(?<![A-Za-z0-9._%+-])'
    r'[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}'
    r'(?![A-Za-z0-9-])'
)

# 13-19 digits with at most one space or hyphen between them. Boundaries keep
# it from finding a card-shaped fragment inside a longer number or identifier.
_CARD_RE = re.compile(r'(?<![0-9A-Za-z])(?:[0-9][ -]?){12,18}[0-9](?![0-9A-Za-z])')

# The 3-2-4 shape is distinctive; bare 9-digit runs need a nearby label
# because ZIP+4s, EINs and invoice numbers share the shape.
_SSN_SEP_RE = re.compile(r'(?<![0-9A-Za-z])(\d{3})[- ](\d{2})[- ](\d{4})(?![0-9A-Za-z])')
_SSN_BARE_RE = re.compile(r'(?<![0-9A-Za-z])(\d{9})(?![0-9A-Za-z])')
_SSN_CONTEXT_RE = re.compile(
    r'(?<![A-Za-z])(?:ssn|s\.\s?s\.\s?n\.?|social\s+security(?:\s+number)?|'
    r'taxpayer\s+(?:id|identification)(?:\s+number)?)(?![A-Za-z])',
    re.IGNORECASE,
)

# NANP: 10 digits, optional leading 1/+1, separators optional, optional
# extension. Area and exchange first digits are checked after matching.
_PHONE_RE = re.compile(
    r'(?<![0-9A-Za-z])'
    r'(?:\+?1[ .-]?)?'
    r'(?:\(\d{3}\)|\d{3})[ .-]?'
    r'\d{3}[ .-]?\d{4}'
    r'(?:\s*(?:x|ext\.?|extension)\s*\d{1,6})?'
    r'(?![0-9A-Za-z])',
    re.IGNORECASE,
)
_PHONE_EXT_RE = re.compile(r'\s*(?:x|ext\.?|extension)\s*\d{1,6}\s*$', re.IGNORECASE)

_CARD_LABEL_RE = re.compile(
    r'(?<![A-Za-z])(?:card|credit|debit|visa|mastercard|amex|american\s+express|'
    r'discover|jcb|diners)(?![A-Za-z])',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Match:
    """One detected span in the source text; ``text[start:end]`` is sensitive."""
    start: int
    end: int
    kind: str


def find_pii(text: str, kinds=DEFAULT_TYPES) -> list[Match]:
    """Return the PII spans in *text* for the enabled *kinds*, sorted by start."""
    if not text:
        return []
    if isinstance(kinds, str):
        kinds = (kinds,)
    kinds = set(kinds)
    found = []
    if EMAIL in kinds:
        found.extend(_emails(text))
    if CREDIT_CARD in kinds:
        found.extend(_cards(text))
    if SSN in kinds:
        found.extend(_ssns(text))
    if PHONE in kinds:
        found.extend(_phones(text))
    return _resolve(found)


def _resolve(matches):
    """Drop overlapping lower-priority detections, then sort by position."""
    matches.sort(key=lambda m: (m.start, -_PRIORITY[m.kind], -(m.end - m.start)))
    chosen = []
    for match in matches:
        for index, previous in enumerate(chosen):
            if match.start < previous.end and previous.start < match.end:
                if _PRIORITY[match.kind] > _PRIORITY[previous.kind]:
                    chosen[index] = match
                break
        else:
            chosen.append(match)
    chosen.sort(key=lambda m: m.start)
    return chosen


def _emails(text):
    return [Match(m.start(), m.end(), EMAIL) for m in _EMAIL_RE.finditer(text)]


def _cards(text):
    found = []
    for match in _CARD_RE.finditer(text):
        if _continues_number(text, match.start(), match.end()):
            continue
        raw = match.group(0)
        digits = re.sub(r'\D', '', raw)
        if not 13 <= len(digits) <= 19:
            continue
        issuer, luhn = _issuer_ok(digits), _luhn(digits)
        # A checksum failure is not a veto for a grouped number with a known
        # issuer: a single mistyped digit must still be redacted.
        if issuer and (luhn or _is_grouped(raw)):
            found.append(Match(match.start(), match.end(), CREDIT_CARD))
        elif luhn and _has_label(text, match.start(), match.end(), _CARD_LABEL_RE):
            found.append(Match(match.start(), match.end(), CREDIT_CARD))
    return found


def _ssns(text):
    found = []
    for match in _SSN_SEP_RE.finditer(text):
        if not _continues_number(text, match.start(), match.end()):
            found.append(Match(match.start(), match.end(), SSN))
    for match in _SSN_BARE_RE.finditer(text):
        if _continues_number(text, match.start(), match.end()):
            continue
        area, group, serial = match.group(1)[:3], match.group(1)[3:5], match.group(1)[5:]
        if not _valid_ssn(area, group, serial):
            continue
        if _has_label(text, match.start(), match.end(), _SSN_CONTEXT_RE):
            found.append(Match(match.start(), match.end(), SSN))
    return found


def _phones(text):
    found = []
    for match in _PHONE_RE.finditer(text):
        if _continues_number(text, match.start(), match.end()):
            continue
        digits = re.sub(r'\D', '', _PHONE_EXT_RE.sub('', match.group(0)))
        if len(digits) == 11 and digits.startswith('1'):
            digits = digits[1:]
        if len(digits) != 10:
            continue
        # NANP area codes start with 2-9. The exchange rule is relaxed: common
        # example numbers such as 555-123-4567 would otherwise be missed, and a
        # false negative is worse than a false positive for an opt-in detector.
        if digits[0] not in '23456789':
            continue
        found.append(Match(match.start(), match.end(), PHONE))
    return found


def _continues_number(text, start, end):
    """Reject a match that is only part of a longer separated number."""
    return bool(re.search(r'\d[ -]$', text[max(0, start - 2):start])
                or re.match(r'[ -]\d', text[end:end + 2]))


def _luhn(digits):
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = ord(char) - 48
        if index % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _issuer_ok(digits):
    """Whether the number starts like a card from a major issuer."""
    if digits[0] == '4':  # Visa
        return True
    if 51 <= int(digits[:2]) <= 55:  # Mastercard
        return True
    if len(digits) >= 4 and 2221 <= int(digits[:4]) <= 2720:  # Mastercard
        return True
    if digits[:2] in ('34', '37'):  # Amex
        return True
    if digits[:4] == '6011' or digits[:2] == '65':  # Discover
        return True
    if len(digits) >= 4 and 3528 <= int(digits[:4]) <= 3589:  # JCB
        return True
    if digits[:2] in ('36', '38') or 300 <= int(digits[:3]) <= 305:  # Diners
        return True
    return False


def _is_grouped(raw):
    """A card-like grouping such as 4-4-4-4, 4-6-5 or 4-4-4-4-3."""
    groups = re.split(r'[ -]', raw)
    return len(groups) >= 3 and all(3 <= len(group) <= 6 for group in groups)


def _valid_ssn(area, group, serial):
    return (area not in ('000', '666') and not area.startswith('9')
            and group != '00' and serial != '0000')


def _has_label(text, start, end, pattern, before=48, after=24):
    """Whether a validating label appears just before or after the span."""
    return bool(pattern.search(text[max(0, start - before):start])
                or pattern.search(text[end:end + after]))
