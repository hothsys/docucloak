"""_redact_image: OCR via pytesseract, black rectangles via PIL.

Most tests stub pytesseract.image_to_data so they run without the Tesseract
binary; test_real_ocr_* only runs when `tesseract` is on PATH.
"""

import shutil
import sys
import threading

import pytest
import pytesseract
from PIL import Image, ImageDraw, ImageFont

from lib import redaction
from lib.redaction import _redact_image

BLACK = (0, 0, 0)
WHITE = (255, 255, 255)


def ocr_data(*words):
    """words: (text, left, top, width, height)"""
    return {
        'text': [w[0] for w in words],
        'left': [w[1] for w in words],
        'top': [w[2] for w in words],
        'width': [w[3] for w in words],
        'height': [w[4] for w in words],
    }


@pytest.fixture
def fake_ocr(monkeypatch):
    holder = {'data': ocr_data()}
    monkeypatch.setattr(pytesseract, 'image_to_data', lambda img, output_type=None, **kw: holder['data'])
    return holder


@pytest.fixture
def white_png(tmp_path):
    p = tmp_path / 'in.png'
    Image.new('RGB', (300, 100), WHITE).save(p)
    return p


def run(src, terms, out, cancel=None):
    return _redact_image(src, terms, out, cancel or threading.Event())


def center(box):
    x, y, w, h = box
    return (x + w // 2, y + h // 2)


def test_blacks_out_matching_word_only(fake_ocr, white_png, tmp_path):
    secret, public = (10, 10, 60, 20), (100, 10, 60, 20)
    fake_ocr['data'] = ocr_data(('secret', *secret), ('public', *public))
    out = tmp_path / 'out.png'
    result, err = run(white_png, ['secret'], out)
    assert (result, err) == ({'matches': 1}, None)
    img = Image.open(out)
    assert img.getpixel(center(secret)) == BLACK
    assert img.getpixel(center(public)) == WHITE


def test_match_is_case_insensitive_substring(fake_ocr, white_png, tmp_path):
    fake_ocr['data'] = ocr_data(('PASSWORD:SECRET123', 10, 10, 100, 20))
    result, _ = run(white_png, ['secret'], tmp_path / 'out.png')
    assert result['matches'] == 1


def test_each_matching_term_counts(fake_ocr, white_png, tmp_path):
    fake_ocr['data'] = ocr_data(('alpha-beta', 10, 10, 100, 20))
    result, _ = run(white_png, ['alpha', 'beta', 'gamma'], tmp_path / 'out.png')
    assert result['matches'] == 2


def test_skips_blank_text_and_degenerate_boxes(fake_ocr, white_png, tmp_path):
    fake_ocr['data'] = ocr_data(('', 0, 0, 50, 50), ('   ', 0, 0, 50, 50), ('secret', 10, 10, 0, 20), ('secret', 10, 10, 20, 0))
    result, err = run(white_png, ['secret'], tmp_path / 'out.png')
    assert (result, err) == ({'matches': 0, 'skipped': True}, None)


def test_no_match_writes_nothing_and_leaves_input_untouched(fake_ocr, white_png, tmp_path):
    fake_ocr['data'] = ocr_data(('public', 10, 10, 60, 20))
    before = (white_png.read_bytes(), white_png.stat().st_mtime_ns)
    out = tmp_path / 'out.png'
    assert run(white_png, ['secret'], out) == ({'matches': 0, 'skipped': True}, None)
    assert not out.exists()
    assert (white_png.read_bytes(), white_png.stat().st_mtime_ns) == before


def test_multiword_term_spanning_ocr_words_is_redacted(fake_ocr, white_png, tmp_path):
    # Tesseract returns one entry per word, so "Alice Smith" never equals any
    # single OCR token. A multi-word term like this (or "SSN: 123-45-6789") is
    # silently left visible in images today.
    alice, smith = (10, 10, 50, 20), (70, 10, 50, 20)
    fake_ocr['data'] = ocr_data(('Alice', *alice), ('Smith', *smith))
    out = tmp_path / 'out.png'
    run(white_png, ['Alice Smith'], out)
    img = Image.open(out)
    assert img.getpixel(center(alice)) == BLACK
    assert img.getpixel(center(smith)) == BLACK


def lines_ocr(*lines):
    """OCR data with block/paragraph/line numbers; each line is a list of (text, x)."""
    data = {k: [] for k in ('text', 'left', 'top', 'width', 'height', 'block_num', 'par_num', 'line_num')}
    for line_no, words in enumerate(lines, 1):
        for text, x in words:
            for k, v in zip(data, (text, x, line_no * 30, 40, 20, 1, 1, line_no)):
                data[k].append(v)
    return data


def test_multiword_term_matches_only_within_one_line(fake_ocr, white_png, tmp_path):
    fake_ocr['data'] = lines_ocr([('Dear', 0), ('Alice', 50)], [('Smith', 0), ('Jones', 50)])
    result, _ = run(white_png, ['Alice Smith'], tmp_path / 'out.png')
    assert result == {'matches': 0, 'skipped': True}, 'words on different lines are not one phrase'


def test_ocr_counts_each_occurrence_and_is_case_insensitive(fake_ocr, white_png, tmp_path):
    fake_ocr['data'] = lines_ocr([('ALICE', 0), ('smith', 50), ('and', 100), ('Alice', 150), ('Smith', 200)])
    result, _ = run(white_png, ['alice SMITH'], tmp_path / 'out.png')
    assert result == {'matches': 2}


def test_ocr_matches_term_split_across_punctuated_words(fake_ocr, white_png, tmp_path):
    fake_ocr['data'] = lines_ocr([('SSN:', 0), ('123-45-6789,', 50), ('ok', 150)])
    out = tmp_path / 'out.png'
    result, _ = run(white_png, ['SSN: 123-45-6789'], out)
    assert result == {'matches': 1}
    img = Image.open(out)
    assert img.getpixel((20, 40)) == BLACK and img.getpixel((70, 40)) == BLACK
    assert img.getpixel((170, 40)) == WHITE


@pytest.mark.parametrize('mode', ['RGBA', 'L', 'P', '1'])
def test_non_rgb_inputs_are_converted(fake_ocr, tmp_path, mode):
    src = tmp_path / 'in.png'
    Image.new('RGB', (100, 50), WHITE).convert(mode).save(src)
    fake_ocr['data'] = ocr_data(('secret', 10, 10, 30, 20))
    out = tmp_path / 'out.png'
    result, err = run(src, ['secret'], out)
    assert err is None and result['matches'] == 1
    img = Image.open(out)
    assert img.mode == 'RGB'
    assert img.getpixel((20, 20)) == BLACK


@pytest.mark.parametrize('ext', ['.jpg', '.png', '.bmp', '.tif'])
def test_output_keeps_requested_format(fake_ocr, tmp_path, ext):
    src = tmp_path / f'in{ext}'
    Image.new('RGB', (40, 40), WHITE).save(src)
    fake_ocr['data'] = ocr_data(('x', 5, 5, 10, 10))
    out = tmp_path / f'out{ext}'
    run(src, ['x'], out)
    assert Image.open(out).format == Image.open(src).format


def test_cancel_mid_scan_writes_nothing(fake_ocr, white_png, tmp_path):
    fake_ocr['data'] = ocr_data(('secret', 10, 10, 30, 20))
    cancel = threading.Event()
    cancel.set()
    out = tmp_path / 'out.png'
    assert run(white_png, ['secret'], out, cancel) == (None, 'cancelled')
    assert not out.exists()


def test_missing_pytesseract_returns_error(monkeypatch, white_png, tmp_path):
    monkeypatch.setitem(sys.modules, 'pytesseract', None)
    out = tmp_path / 'out.png'
    assert run(white_png, ['secret'], out) == (None, redaction.MSG_NEEDS_TESSERACT)
    assert not out.exists()


def test_missing_tesseract_binary_is_a_friendly_error(monkeypatch, white_png, tmp_path):
    def raise_not_found(*a, **k):
        raise pytesseract.TesseractNotFoundError()
    monkeypatch.setattr(pytesseract, 'image_to_data', raise_not_found)
    out = tmp_path / 'out.png'
    assert run(white_png, ['secret'], out) == (None, redaction.MSG_NEEDS_TESSERACT)
    assert not out.exists()


def test_unreadable_image_is_a_friendly_error(fake_ocr, tmp_path):
    bad = tmp_path / 'broken.jpg'
    bad.write_bytes(b'not an image')
    assert run(bad, ['secret'], tmp_path / 'out.jpg') == (None, redaction.MSG_UNREADABLE_IMAGE)


@pytest.mark.skipif(not shutil.which('tesseract'), reason='tesseract binary not installed')
def test_real_ocr_redacts_rendered_word(tmp_path):
    src = tmp_path / 'in.png'
    img = Image.new('RGB', (600, 120), WHITE)
    ImageDraw.Draw(img).text((20, 30), 'CONFIDENTIAL public', fill=BLACK, font=ImageFont.load_default(size=48))
    img.save(src)
    out = tmp_path / 'out.png'
    result, err = run(src, ['confidential'], out)
    assert err is None and result['matches'] >= 1
    data = pytesseract.image_to_data(Image.open(out), output_type=pytesseract.Output.DICT)
    words = ' '.join(data['text']).lower()
    assert 'confidential' not in words and 'public' in words
