"""PII detectors: validation rules, boundaries, and false-positive guards."""

import pytest

import pii


def spans(text, kinds):
    return [text[m.start:m.end] for m in pii.find_pii(text, kinds)]


def test_type_ids_are_stable():
    assert pii.PII_TYPES == ('email', 'credit_card', 'ssn', 'phone')
    assert pii.DEFAULT_TYPES == ('email', 'credit_card', 'ssn')


def test_every_type_has_a_display_label():
    assert set(pii.LABELS) == set(pii.PII_TYPES)


def test_match_holds_a_source_span():
    match = pii.Match(4, 9, pii.EMAIL)
    assert (match.start, match.end, match.kind) == (4, 9, pii.EMAIL)


# ─── Email addresses ───────────────────────────────────────────────────────────

@pytest.mark.parametrize('text, expected', [
    ('Write to alice@example.com today', ['alice@example.com']),
    ('a.b+tag@sub.example.co.uk', ['a.b+tag@sub.example.co.uk']),
    ('<ops@example.org>,', ['ops@example.org']),
    ('alice@example.com.', ['alice@example.com']),
    ('no at sign here', []),
    ('user@localhost', []),
    ('not.an.email@', []),
    ('two: a@b.co and c@d.org', ['a@b.co', 'c@d.org']),
])
def test_email_detection(text, expected):
    assert spans(text, (pii.EMAIL,)) == expected


# ─── Credit cards ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize('text, expected', [
    ('Card 4111 1111 1111 1111', ['4111 1111 1111 1111']),
    ('4111-1111-1111-1111', ['4111-1111-1111-1111']),
    ('4111111111111111', ['4111111111111111']),
    ('Amex 3782 822463 10005', ['3782 822463 10005']),
    ('Mastercard 5555555555554444', ['5555555555554444']),
    ('Discover 6011111111111117', ['6011111111111117']),
    ('Visa 4222 2222 2222 2', ['4222 2222 2222 2']),
    ('4111 1111 1111 1111', ['4111 1111 1111 1111']),
    # A failed checksum needs a card label; an account/invoice label is not one.
    ('Number 4111 1111 1111 1112', []),
    ('Card 4111 1111 1111 1112', ['4111 1111 1111 1112']),
    ('Account Number: 4111 1111 1111 1112', []),
    # A valid number introduced as an account/invoice/reference is not a card.
    ('Account Number: 4111 1111 1111 1111', []),
    ('My account number is 4111 1111 1111 1111', []),
    ('Account 4111111111111111', []),
    ('Invoice 5555 5555 5555 4444', []),
    ('Reference 6011 1111 1111 1117', []),
    # A nearby card label wins over a generic account word.
    ('Credit card account number 4111 1111 1111 1111', ['4111 1111 1111 1111']),
    ('Account Number: 4111 1111 1111 1111 Visa', ['4111 1111 1111 1111']),
    # Luhn-valid but not a known issuer: only with a card label
    ('1234 5678 9012 3452', []),
    ('Card 1234 5678 9012 3452', ['1234 5678 9012 3452']),
    ('Invoice 1234 5678 9012 3456', []),
    ('4111 1111 1111', []),
    ('4111 1111 1111 1111 9', []),
    ('x4111111111111111', []),
    ('4111111111111111x', []),
])
def test_card_detection(text, expected):
    assert spans(text, (pii.CREDIT_CARD,)) == expected


# ─── Social Security numbers ───────────────────────────────────────────────────

@pytest.mark.parametrize('text, expected', [
    ('SSN: 123-45-6789', ['123-45-6789']),
    ('SSN 123 45 6789', ['123 45 6789']),
    # The 3-2-4 shape is strong enough to redact even placeholders
    ('Placeholder 000-00-0000', ['000-00-0000']),
    ('ZIP+4 94105-1234', []),
    ('Phone 555-123-4567', []),
    ('SSN 123456789', ['123456789']),
    ('Social Security number 123456789', ['123456789']),
    ('Account 123456789', []),
    ('SSN 000456789', []),
    ('SSN 666456789', []),
    ('SSN 900456789', []),
    ('SSN 123006789', []),
    ('SSN 123450000', []),
    ('x123-45-6789', []),
    ('123-45-6789x', []),
])
def test_ssn_detection(text, expected):
    assert spans(text, (pii.SSN,)) == expected


# ─── Phone numbers ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize('text, expected', [
    ('Call 555-123-4567', ['555-123-4567']),
    ('Call (555) 123-4567', ['(555) 123-4567']),
    ('Call +1 555 123 4567', ['+1 555 123 4567']),
    ('Call 5551234567', ['5551234567']),
    ('Call 15551234567', ['15551234567']),
    ('Call 1-800-555-1212', ['1-800-555-1212']),
    ('Call 555.123.4567', ['555.123.4567']),
    ('Call 555-123-4567 ext. 89', ['555-123-4567 ext. 89']),
    ('Call 555-123-4567x89', ['555-123-4567x89']),
    ('SSN 123-45-6789', []),
    ('Account 123456789', []),
    ('Call 1234567890', []),          # area codes cannot start with 1
    ('Call 0551234567', []),          # ...or 0
    ('Call 555-123-456', []),         # 9 digits
    ('Call 55512345678', []),         # 11 digits without a leading 1
    ('x5551234567', []),
    ('5551234567x', []),
    ('Call 555-123-4567-8', []),      # part of a longer number
    ('Order 5551234567', ['5551234567']),  # a plain 10-digit ID is unavoidable
])
def test_phone_detection(text, expected):
    assert spans(text, (pii.PHONE,)) == expected


# ─── Combining, disabling and overlaps ────────────────────────────────────────

def test_only_enabled_kinds_run():
    assert spans('alice@example.com', ()) == []
    assert spans('alice@example.com', (pii.SSN,)) == []
    assert spans('Call 555-123-4567', pii.DEFAULT_TYPES) == []  # phone is opt-in


def test_unknown_kinds_are_ignored():
    assert spans('alice@example.com', ('credit_card', 'nonsense')) == []


def test_string_kind_is_accepted():
    assert spans('alice@example.com', pii.EMAIL) == ['alice@example.com']


def test_empty_text_has_no_matches():
    assert pii.find_pii('', pii.PII_TYPES) == []


def test_email_wins_over_a_phone_shaped_local_part():
    text = '5551234567@example.com'
    found = pii.find_pii(text, (pii.EMAIL, pii.PHONE))
    assert [(text[m.start:m.end], m.kind) for m in found] == [(text, pii.EMAIL)]


def test_all_types_together_and_sorted():
    text = 'alice@example.com, SSN 123456789, card 4111 1111 1111 1111, call 555-123-4567'
    found = pii.find_pii(text, pii.PII_TYPES)
    assert [m.kind for m in found] == [pii.EMAIL, pii.SSN, pii.CREDIT_CARD, pii.PHONE]
    assert [m.start for m in found] == sorted(m.start for m in found)


def test_dates_and_amounts_are_not_matched():
    text = 'On 2026-10-23 the amount was 1,234.56 and the order was 12345678.'
    assert pii.find_pii(text, pii.PII_TYPES) == []
