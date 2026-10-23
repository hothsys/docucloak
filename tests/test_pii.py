"""PII detectors: validation rules, boundaries, and false-positive guards."""

import pii


def test_type_ids_are_stable():
    assert pii.PII_TYPES == ('email', 'credit_card', 'ssn', 'phone')
    assert pii.DEFAULT_TYPES == ('email', 'credit_card', 'ssn')


def test_every_type_has_a_display_label():
    assert set(pii.LABELS) == set(pii.PII_TYPES)


def test_match_holds_a_source_span():
    match = pii.Match(4, 9, pii.EMAIL)
    assert (match.start, match.end, match.kind) == (4, 9, pii.EMAIL)
