"""_redact_pdf: PyMuPDF search + redaction annotations."""

import shutil
import sys
import threading

import pymupdf as fitz
import pytest

import app

from app import _redact_pdf
from conftest import SAMPLE_LINES, make_pdf, pdf_text


def run(src, terms, out, cancel=None):
    return _redact_pdf(src, terms, out, cancel or threading.Event())


def test_removes_matched_text_and_keeps_the_rest(sample_pdf, tmp_path):
    out = tmp_path / 'out.pdf'
    result, err = run(sample_pdf, ['Alice Smith', 'SSN: 123-45-6789'], out)
    assert err is None
    assert result == {'matches': 2, 'pages': 1}
    text = pdf_text(out)
    assert 'Alice' not in text and '123-45-6789' not in text
    assert 'John Doe' in text and 'Phone' in text
    assert 'Alice Smith' in pdf_text(sample_pdf), 'input must not be modified'


def test_text_is_removed_not_just_covered(sample_pdf, tmp_path):
    out = tmp_path / 'out.pdf'
    run(sample_pdf, ['John Doe'], out)
    doc = fitz.open(str(out))
    words = [w[4] for w in doc[0].get_text('words')]
    doc.close()
    assert 'John' not in words and 'Doe' not in words


def test_draws_black_box_over_match(sample_pdf, tmp_path):
    out = tmp_path / 'out.pdf'
    src = fitz.open(str(sample_pdf))
    rect = src[0].search_for('John Doe')[0]
    src.close()
    run(sample_pdf, ['John Doe'], out)
    doc = fitz.open(str(out))
    pix = doc[0].get_pixmap(clip=rect)
    doc.close()
    samples = pix.samples
    dark = sum(1 for i in range(0, len(samples), pix.n) if samples[i] < 40)
    assert dark / (pix.width * pix.height) > 0.9


def test_search_is_case_insensitive(sample_pdf, tmp_path):
    out = tmp_path / 'out.pdf'
    result, _ = run(sample_pdf, ['alice smith'], out)
    assert result['matches'] == 1
    assert 'Alice' not in pdf_text(out)


def test_substring_match(sample_pdf, tmp_path):
    out = tmp_path / 'out.pdf'
    result, _ = run(sample_pdf, ['secret'], out)
    assert result['matches'] == 1
    assert 'secret123' not in pdf_text(out)


def test_counts_every_occurrence_across_pages(tmp_path):
    src = make_pdf(tmp_path / 'multi.pdf', [['token one', 'token two'], ['nothing'], ['token three']])
    out = tmp_path / 'out.pdf'
    result, err = run(src, ['token'], out)
    assert err is None
    assert result == {'matches': 3, 'pages': 3}
    assert 'token' not in pdf_text(out)


def test_no_matches_skips_file_and_writes_nothing(sample_pdf, tmp_path):
    before = (sample_pdf.read_bytes(), sample_pdf.stat().st_mtime_ns)
    out = tmp_path / 'out.pdf'
    result, err = run(sample_pdf, ['not present anywhere'], out)
    assert (result, err) == ({'matches': 0, 'pages': 1, 'skipped': True}, None)
    assert not out.exists()
    assert (sample_pdf.read_bytes(), sample_pdf.stat().st_mtime_ns) == before


def test_empty_terms_list_is_skipped(sample_pdf, tmp_path):
    out = tmp_path / 'out.pdf'
    result, err = run(sample_pdf, [], out)
    assert (result, err) == ({'matches': 0, 'pages': 1, 'skipped': True}, None)
    assert not out.exists()


def test_cancelled_before_start_writes_nothing(sample_pdf, tmp_path):
    out = tmp_path / 'out.pdf'
    cancel = threading.Event()
    cancel.set()
    assert run(sample_pdf, SAMPLE_LINES, out, cancel) == (None, 'cancelled')
    assert not out.exists()


def test_rerun_with_changed_terms_uses_new_terms(sample_pdf, tmp_path):
    out = tmp_path / 'out.pdf'
    run(sample_pdf, ['Alice Smith'], out)
    assert 'Alice' not in pdf_text(out)
    run(sample_pdf, ['John Doe'], out)
    text = pdf_text(out)
    assert 'Alice Smith' in text and 'John' not in text


# ─── Hidden content: metadata, bookmarks, leftover objects ────────────────────

def raw_pdf_contains(path, needle):
    """True if needle appears in any object or decompressed stream of the file,
    either as plain text or hex-encoded (how PDF content streams often store text)."""
    forms = [needle, needle.encode().hex(), needle.encode().hex().upper()]
    doc = fitz.open(str(path))
    try:
        for xref in range(1, doc.xref_length()):
            blob = doc.xref_object(xref, compressed=False) or ''
            if doc.xref_is_stream(xref):
                blob += (doc.xref_stream(xref) or b'').decode('latin-1')
            if any(f in blob for f in forms):
                return True
        return False
    finally:
        doc.close()


def pdf_with_hidden_term(path, page_lines):
    make_pdf(path, [page_lines])
    doc = fitz.open(str(path))
    doc.set_metadata({'title': 'Alice Smith statement', 'author': 'Alice Smith', 'keywords': 'nothing'})
    doc.set_xml_metadata('<x:xmpmeta xmlns:x="adobe:ns:meta/"><dc>Alice Smith</dc></x:xmpmeta>')
    doc.set_toc([[1, 'Alice Smith account', 1], [1, 'Summary', 1]])
    doc.saveIncr()
    doc.close()
    return path


def test_metadata_xmp_and_bookmarks_are_cleared(tmp_path):
    src = pdf_with_hidden_term(tmp_path / 'm.pdf', ['Statement for Alice Smith', 'Balance 42'])
    assert raw_pdf_contains(src, 'Alice Smith'), 'sanity check: the input does contain it'
    out = tmp_path / 'out.pdf'
    result, err = run(src, ['Alice Smith'], out)
    assert err is None
    assert result == {'matches': 1, 'pages': 1, 'metadata_matches': 4}
    doc = fitz.open(str(out))
    assert not any(v for k, v in doc.metadata.items() if k != 'format')
    assert not doc.get_xml_metadata()
    assert doc.get_toc() == []
    doc.close()
    assert 'Balance 42' in pdf_text(out)
    assert not raw_pdf_contains(out, 'Alice')


def test_term_only_in_metadata_still_produces_cleaned_copy(tmp_path):
    src = pdf_with_hidden_term(tmp_path / 'm.pdf', ['Nothing sensitive on the page'])
    out = tmp_path / 'out.pdf'
    result, err = run(src, ['alice smith'], out)  # matching is case-insensitive here too
    assert err is None and result == {'matches': 0, 'pages': 1, 'metadata_matches': 4}
    assert out.exists() and not raw_pdf_contains(out, 'Alice')


def test_skipped_file_keeps_its_metadata_untouched(tmp_path):
    src = pdf_with_hidden_term(tmp_path / 'm.pdf', ['Nothing here'])
    before = src.read_bytes()
    result, _ = run(src, ['not anywhere'], tmp_path / 'out.pdf')
    assert result['skipped'] and src.read_bytes() == before


def test_redacted_text_cannot_be_recovered_from_file(sample_pdf, tmp_path):
    out = tmp_path / 'out.pdf'
    run(sample_pdf, ['123-45-6789', 'John Doe'], out)
    assert raw_pdf_contains(sample_pdf, '123-45-6789'), 'sanity check: the input does contain it'
    assert not raw_pdf_contains(out, '123-45-6789')
    assert not raw_pdf_contains(out, 'John Doe')


# ─── Scanned pages (no text layer) are OCR'd ───────────────────────────────────

def scanned_pdf(path, lines, rotate=0):
    """A PDF whose page is only an image of the text, like a scanner produces."""
    path.parent.mkdir(parents=True, exist_ok=True)
    src = fitz.open()
    page = src.new_page()
    for i, line in enumerate(lines):
        page.insert_text((72, 100 + 40 * i), line, fontsize=18)
    pix = page.get_pixmap(dpi=200)
    doc = fitz.open()
    scan = doc.new_page(width=page.rect.width, height=page.rect.height)
    scan.insert_image(scan.rect, pixmap=pix)
    if rotate:
        scan.set_rotation(rotate)
    doc.save(str(path))
    return path


def dark_fraction(path, rect, page_no=0):
    doc = fitz.open(str(path))
    pix = doc[page_no].get_pixmap(clip=rect, dpi=72)
    doc.close()
    s = pix.samples
    return sum(1 for i in range(0, len(s), pix.n) if s[i] < 40) / (pix.width * pix.height)


@pytest.fixture
def fake_page_ocr(monkeypatch):
    """Stub OCR returning one word box, given in OCR pixel coordinates."""
    import pytesseract
    calls = []

    def fake(img, output_type=None, **kw):
        calls.append(img.size)
        scale = app.OCR_DPI / 72
        x, y, w, h = (100 * scale, 200 * scale, 150 * scale, 30 * scale)
        return {'text': ['Alice', 'Smith', 'Balance'], 'left': [x, x + w + 10, x],
                'top': [y, y, y + 100], 'width': [w, w, w], 'height': [h, h, h],
                'block_num': [1, 1, 1], 'par_num': [1, 1, 1], 'line_num': [1, 1, 2]}
    monkeypatch.setattr(pytesseract, 'image_to_data', fake)
    return calls


def test_scanned_page_is_ocrd_and_box_lands_on_the_words(tmp_path, fake_page_ocr):
    src = scanned_pdf(tmp_path / 'scan.pdf', ['x'])
    out = tmp_path / 'out.pdf'
    result, err = run(src, ['Alice Smith'], out)
    assert err is None and result == {'matches': 1, 'pages': 1, 'ocr_pages': 1}
    assert len(fake_page_ocr) == 1
    assert dark_fraction(out, fitz.Rect(105, 205, 245, 225)) > 0.9    # "Alice"
    assert dark_fraction(out, fitz.Rect(265, 205, 405, 225)) > 0.9    # "Smith"
    assert dark_fraction(out, fitz.Rect(105, 305, 245, 325)) < 0.1    # "Balance" untouched


def test_pages_with_text_are_not_ocrd(sample_pdf, tmp_path, fake_page_ocr):
    run(sample_pdf, ['John Doe'], tmp_path / 'out.pdf')
    assert fake_page_ocr == []


def test_blank_page_without_images_is_not_ocrd(tmp_path, fake_page_ocr):
    make_pdf(tmp_path / 'blank.pdf', [[]])
    result, _ = run(tmp_path / 'blank.pdf', ['Alice'], tmp_path / 'out.pdf')
    assert fake_page_ocr == [] and result['skipped']


def test_scanned_page_without_pytesseract_is_an_error_not_a_skip(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, 'pytesseract', None)
    src = scanned_pdf(tmp_path / 'scan.pdf', ['Alice Smith'])
    out = tmp_path / 'out.pdf'
    result, err = run(src, ['Alice Smith'], out)
    assert result is None and err == app.MSG_NEEDS_TESSERACT
    assert not out.exists()


def test_cancel_during_ocr(tmp_path, monkeypatch):
    import pytesseract
    cancel = threading.Event()
    monkeypatch.setattr(pytesseract, 'image_to_data', lambda img, output_type=None, **kw: (
        cancel.set(), {'text': ['a'], 'left': [0], 'top': [0], 'width': [1], 'height': [1]})[1])
    out = tmp_path / 'out.pdf'
    assert run(scanned_pdf(tmp_path / 's.pdf', ['x']), ['a'], out, cancel) == (None, 'cancelled')
    assert not out.exists()


needs_tesseract = pytest.mark.skipif(not shutil.which('tesseract'), reason='tesseract binary not installed')


def ocr_pdf(path):
    import pytesseract
    from PIL import Image
    doc = fitz.open(str(path))
    pix = doc[0].get_pixmap(dpi=300)
    doc.close()
    return pytesseract.image_to_string(Image.frombytes('RGB', (pix.width, pix.height), pix.samples))


@needs_tesseract
@pytest.mark.parametrize('rotate', [0, 90])
def test_real_ocr_redacts_scanned_pdf(tmp_path, rotate):
    lines = ['Statement for Alice Smith', 'SSN 123-45-6789', 'Balance due 42.00']
    src = scanned_pdf(tmp_path / 'scan.pdf', lines, rotate=rotate)
    assert 'Alice Smith' in ocr_pdf(src), 'sanity check: OCR reads the input'
    out = tmp_path / 'out.pdf'
    result, err = run(src, ['Alice Smith', '123-45-6789'], out)
    assert err is None and result['matches'] == 2 and result['ocr_pages'] == 1
    text = ocr_pdf(out)
    assert 'Alice' not in text and 'Smith' not in text and '6789' not in text
    assert 'Balance due' in text


@needs_tesseract
def test_real_ocr_scanned_image_pixels_are_removed_not_covered(tmp_path):
    """The black box must not just sit on top: the embedded image itself is changed."""
    import pytesseract
    from PIL import Image
    import io
    src = scanned_pdf(tmp_path / 'scan.pdf', ['Statement for Alice Smith', 'Balance due 42.00'])
    out = tmp_path / 'out.pdf'
    run(src, ['Alice Smith'], out)
    doc = fitz.open(str(out))
    images = [doc.extract_image(xref)['image'] for xref, *_ in doc[0].get_images(full=True)]
    doc.close()
    assert images
    for data in images:
        text = pytesseract.image_to_string(Image.open(io.BytesIO(data)))
        assert 'Alice' not in text and 'Smith' not in text
