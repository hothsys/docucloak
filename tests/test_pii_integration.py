"""PII detection through the PDF, image and logging paths."""

import logging
import threading

import pymupdf as fitz
import pytesseract
import pytest
from PIL import Image

from docucloak import pii, redaction
from conftest import make_pdf, pdf_text


PII_LINES = [
    'Email alice@example.com',
    'SSN 123-45-6789',
    'Card 4111 1111 1111 1111',
    'Phone 555-123-4567',
    'Keep this line',
]
SECRETS = ['alice@example.com', '123-45-6789', '4111 1111 1111 1111', '555-123-4567']


def run(src, out, kinds):
    return redaction._redact_file(src, out, [], threading.Event(), kinds)


# ─── PDFs ─────────────────────────────────────────────────────────────────────

def test_pdf_redacts_all_enabled_types_without_terms(tmp_path):
    src = make_pdf(tmp_path / 'in' / 'pii.pdf', [PII_LINES])
    out = tmp_path / 'out' / 'pii_redacted.pdf'
    info = run(src, out, pii.PII_TYPES)
    assert info['error'] is None
    assert info['result']['matches'] == 4
    assert info['result']['pii'] == {'email': 1, 'ssn': 1, 'credit_card': 1, 'phone': 1}
    text = pdf_text(out)
    assert 'Keep this line' in text
    for secret in SECRETS:
        assert secret not in text


def test_pdf_phone_detection_is_opt_in(tmp_path):
    src = make_pdf(tmp_path / 'in' / 'pii.pdf', [PII_LINES])
    out = tmp_path / 'out' / 'pii_redacted.pdf'
    info = run(src, out, pii.DEFAULT_TYPES)
    assert info['result']['pii'] == {'email': 1, 'ssn': 1, 'credit_card': 1}
    text = pdf_text(out)
    assert '555-123-4567' in text
    for secret in SECRETS[:3]:
        assert secret not in text


def test_account_numbers_are_not_reported_as_cards(tmp_path):
    src = make_pdf(tmp_path / 'in' / 'account.pdf', [['Account Number: 4111 1111 1111 1111']])
    out = tmp_path / 'out' / 'account_redacted.pdf'
    info = run(src, out, (pii.CREDIT_CARD,))
    assert info['error'] is None
    assert info['result']['skipped'] is True
    assert not out.exists()


def test_labelled_card_is_still_redacted(tmp_path):
    src = make_pdf(tmp_path / 'in' / 'card.pdf', [['Card Number: 4111 1111 1111 1111']])
    out = tmp_path / 'out' / 'card_redacted.pdf'
    info = run(src, out, (pii.CREDIT_CARD,))
    assert info['error'] is None
    assert info['result']['pii'] == {'credit_card': 1}
    assert '4111 1111 1111 1111' not in pdf_text(out)


def test_pdf_without_terms_or_detectors_is_skipped(tmp_path):
    src = make_pdf(tmp_path / 'in' / 'pii.pdf', [PII_LINES])
    out = tmp_path / 'out' / 'pii_redacted.pdf'
    info = run(src, out, ())
    assert info['result']['skipped'] is True
    assert not out.exists()


def test_metadata_only_pii_still_produces_a_clean_copy(tmp_path):
    src = tmp_path / 'in' / 'metadata.pdf'
    src.parent.mkdir()
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), 'Nothing sensitive in the body', fontsize=12)
    doc.set_metadata({'author': 'alice@example.com', 'title': 'Report'})
    doc.save(str(src))
    doc.close()

    out = tmp_path / 'out' / 'metadata_redacted.pdf'
    info = run(src, out, (pii.EMAIL,))
    assert info['error'] is None
    assert info['result']['matches'] == 0
    assert info['result']['pii'] == {'email': 1}
    assert out.exists()
    doc = fitz.open(str(out))
    assert not doc.metadata.get('author')
    doc.close()


# ─── Images ───────────────────────────────────────────────────────────────────

def _ocr_data(words):
    keys = ('text', 'left', 'top', 'width', 'height')
    return {key: [word[i] for word in words] for i, key in enumerate(keys)}


@pytest.fixture
def fake_ocr(monkeypatch):
    words = [
        ('Email', 10, 20, 60, 20), ('alice@example.com', 80, 20, 180, 20),
        ('SSN', 10, 60, 50, 20), ('123-45-6789', 70, 60, 140, 20),
        ('Card', 10, 100, 50, 20), ('4111', 70, 100, 50, 20),
        ('1111', 130, 100, 50, 20), ('1111', 190, 100, 50, 20), ('1111', 250, 100, 50, 20),
        ('Phone', 10, 140, 60, 20), ('555-123-4567', 80, 140, 140, 20),
    ]
    monkeypatch.setattr(pytesseract, 'image_to_data',
                        lambda img, output_type=None, **kw: _ocr_data(words))


def test_image_redacts_pii_without_terms(tmp_path, fake_ocr):
    src = tmp_path / 'in' / 'pii.png'
    src.parent.mkdir()
    Image.new('RGB', (500, 200), 'white').save(src)

    out = tmp_path / 'out' / 'pii_redacted.png'
    info = run(src, out, pii.PII_TYPES)
    assert info['error'] is None
    assert info['result']['matches'] == 4
    assert info['result']['pii'] == {'email': 1, 'ssn': 1, 'credit_card': 1, 'phone': 1}

    img = Image.open(out)
    for point in [(85, 25), (75, 65), (75, 105), (85, 145)]:
        assert img.getpixel(point) == (0, 0, 0)
    assert img.getpixel((450, 180)) == (255, 255, 255)


# ─── Privacy ──────────────────────────────────────────────────────────────────

def test_detected_values_are_never_logged(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger='docucloak')
    src = make_pdf(tmp_path / 'in' / 'pii.pdf', [PII_LINES])
    info = run(src, tmp_path / 'out' / 'pii_redacted.pdf', pii.PII_TYPES)
    assert info['result']['pii']

    logged = caplog.text
    for secret in SECRETS:
        assert secret not in logged
    assert 'credit card number' in logged  # counts and type names are fine
