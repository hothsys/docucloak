"""_parse_terms: turns the textarea / terms-file text into a term list."""

import pytest

from lib.terms import _parse_terms


@pytest.mark.parametrize('raw, expected', [
    ('', []),
    ('   \n\n , ,\n', []),
    ('secret', ['secret']),
    ('a, b,c', ['a', 'b', 'c']),
    ('a\nb\n\nc', ['a', 'b', 'c']),
    ('a, b\nc,d', ['a', 'b', 'c', 'd']),
    ('a\r\nb\r\n', ['a', 'b']),  # Windows line endings from an edited terms file
    ('  Alice Smith  \n\tJohn Doe\t', ['Alice Smith', 'John Doe']),
    ('SSN: 123-45-6789', ['SSN: 123-45-6789']),
])
def test_splits_on_commas_and_newlines(raw, expected):
    assert _parse_terms(raw) == expected


def test_dedupes_preserving_first_occurrence_order():
    assert _parse_terms('b, a\nb\nc, a') == ['b', 'a', 'c']


def test_dedupe_is_case_sensitive():
    assert _parse_terms('Secret, secret') == ['Secret', 'secret']


def test_comma_inside_term_splits_it():
    # There is no escaping; documents the current contract.
    assert _parse_terms('Smith, John') == ['Smith', 'John']


def test_unicode_terms():
    assert _parse_terms('José Müller\n東京') == ['José Müller', '東京']
