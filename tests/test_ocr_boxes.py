"""_fit_box_to_ink: correcting Tesseract's word boxes against the glyphs drawn.

Real case: in "Garden City, NY 11530-1234" Tesseract read the zip correctly but
placed the first "1" on top of the second, so its word box started at the second
digit and the redaction left the first "1" visible.
"""

import pytest
from PIL import Image, ImageDraw, ImageFont

from lib import redaction


def drawn(text, size=40, origin=(20, 30), width=700, height=110):
    img = Image.new('L', (width, height), 255)
    ImageDraw.Draw(img).text(origin, text, fill=0, font=ImageFont.load_default(size=size))
    return img


def glyph_runs(img, y0=0, y1=None):
    """Dark column runs: where each glyph actually is."""
    y1 = img.height if y1 is None else y1
    dark = [any(img.getpixel((x, y)) < 160 for y in range(y0, y1)) for x in range(img.width)]
    runs, start = [], None
    for x, d in enumerate(dark + [False]):
        if d and start is None:
            start = x
        elif not d and start is not None:
            runs.append((start, x - 1)); start = None
    return runs


def ink_top_bottom(img):
    rows = [y for y in range(img.height) if any(img.getpixel((x, y)) < 160 for x in range(img.width))]
    return rows[0], rows[-1]


def test_box_missing_the_first_digit_is_extended_to_it():
    img = drawn('NY 11530-1234')
    runs = glyph_runs(img)
    ny_end = runs[1][1]                       # "N", "Y" are the first two glyphs
    first_one, rest = runs[2], runs[3:]
    top, bottom = ink_top_bottom(img)
    # Tesseract's mistake: the word box starts at the second "1"
    box = (rest[0][0], top + 3, rest[-1][1] + 1 - rest[0][0], bottom - top - 6)
    x, y, w, h = redaction._fit_box_to_ink(img, box, '11530-1234')
    assert x <= first_one[0], 'the first digit must be inside the corrected box'
    assert x + w > rest[-1][1], 'the last digit stays inside'
    assert x > ny_end, '"NY" must not be covered'
    assert y <= top and y + h > bottom, 'glyph tops and bottoms above/below the estimate are covered'


def test_correct_box_only_gets_a_small_margin():
    img = drawn('11530')
    runs = glyph_runs(img)
    top, bottom = ink_top_bottom(img)
    box = (runs[0][0], top, runs[-1][1] + 1 - runs[0][0], bottom + 1 - top)
    x, y, w, h = redaction._fit_box_to_ink(img, box, '11530')
    margin = max(1, round(0.08 * box[3]))
    assert (x, y, w, h) == (box[0] - margin, box[1] - margin, box[2] + 2 * margin, box[3] + 2 * margin)


def test_neighbouring_word_is_not_taken_when_nothing_is_missing():
    img = drawn('Alice Smith')
    runs = glyph_runs(img)
    top, bottom = ink_top_bottom(img)
    smith = [r for r in runs if r[0] > runs[4][1] + 5]       # glyphs after "Alice"
    box = (smith[0][0], top, smith[-1][1] + 1 - smith[0][0], bottom + 1 - top)
    x, _, _, _ = redaction._fit_box_to_ink(img, box, 'Smith')
    assert x > runs[4][1], '"Alice" stays visible'


def test_extension_is_capped_at_two_glyphs():
    img = drawn('ab cd ef gh')
    runs = glyph_runs(img)
    top, bottom = ink_top_bottom(img)
    box = (runs[-1][0], top, runs[-1][1] + 1 - runs[-1][0], bottom + 1 - top)   # just "h"
    x, _, _, _ = redaction._fit_box_to_ink(img, box, 'abcdefgh')                # claims 8 characters
    taken = [r for r in runs if r[0] >= x]
    assert len(taken) <= 3, 'at most 2 glyphs added however many seem missing'


def test_far_away_glyphs_are_not_taken():
    img = drawn('1                    1530', width=900)
    runs = glyph_runs(img)
    top, bottom = ink_top_bottom(img)
    box = (runs[1][0], top, runs[-1][1] + 1 - runs[1][0], bottom + 1 - top)
    x, _, _, _ = redaction._fit_box_to_ink(img, box, '11530')
    assert x > runs[0][1], 'a glyph far beyond half the text height is another word'


def test_vertical_text_is_measured_along_its_direction():
    horizontal = drawn('NY 11530-1234')
    runs = glyph_runs(horizontal)
    top, bottom = ink_top_bottom(horizontal)
    rest = runs[3:]
    box = (rest[0][0], top + 3, rest[-1][1] + 1 - rest[0][0], bottom - top - 6)
    expected = redaction._fit_box_to_ink(horizontal, box, '11530-1234')
    vertical = horizontal.transpose(Image.Transpose.TRANSPOSE)          # as on a page rotated 90°
    vx, vy, vw, vh = redaction._fit_box_to_ink(vertical, (box[1], box[0], box[3], box[2]), '11530-1234')
    assert (vy, vx, vh, vw) == expected


def test_box_on_blank_area_just_gets_margin():
    img = Image.new('L', (200, 100), 255)
    assert redaction._fit_box_to_ink(img, (50, 40, 60, 20), 'secret') == (48, 38, 64, 24)


def test_redacted_image_leaves_no_trace_of_misplaced_first_digit(tmp_path, monkeypatch):
    """End to end through image redaction with Tesseract's misplaced box."""
    import pytesseract
    import threading
    img = drawn('NY 11530-1234').convert('RGB')
    src = tmp_path / 'zip.png'
    img.save(src)
    runs = glyph_runs(img.convert('L'))
    top, bottom = ink_top_bottom(img.convert('L'))
    rest = runs[3:]
    monkeypatch.setattr(pytesseract, 'image_to_data', lambda *a, **k: {
        'text': ['NY', '11530-1234'], 'left': [runs[0][0], rest[0][0]], 'top': [top, top + 3],
        'width': [runs[1][1] - runs[0][0], rest[-1][1] + 1 - rest[0][0]], 'height': [bottom - top, bottom - top - 6],
        'block_num': [1, 1], 'par_num': [1, 1], 'line_num': [1, 1]})
    out = tmp_path / 'out.png'
    result, err = redaction._redact_image(src, ['11530-1234'], out, threading.Event())
    assert err is None and result['matches'] == 1
    redacted = Image.open(out).convert('L')
    first_one = runs[2]
    assert all(redacted.getpixel((x, y)) < 40 for x in range(first_one[0], first_one[1] + 1) for y in range(top, bottom + 1)
               if img.convert('L').getpixel((x, y)) < 160), 'every pixel of the first digit is blacked out'
    assert any(redacted.getpixel((x, y)) < 160 and redacted.getpixel((x, y)) > 0 or redacted.getpixel((x, y)) < 160
               for x in range(runs[0][0], runs[1][1] + 1) for y in range(top, bottom + 1)), '"NY" is still there'
