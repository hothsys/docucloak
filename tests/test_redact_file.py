"""_redact_file: dispatch by extension and overwrite semantics."""

import threading

import pytest

from docucloak import redaction
from conftest import pdf_text


def test_pdf_dispatch_returns_info_dict(sample_pdf, tmp_path):
    out = tmp_path / 'sample_redacted.pdf'
    info = redaction._redact_file(sample_pdf, out, ['John Doe'], threading.Event())
    assert info == {
        'path': str(out), 'input': str(sample_pdf),
        'result': {'matches': 1, 'pages': 1}, 'error': None,
    }


@pytest.mark.parametrize('ext', ['.jpg', '.JPEG', '.png', '.tif', '.tiff', '.bmp'])
def test_image_extensions_dispatch_to_image_redactor(monkeypatch, tmp_path, ext):
    calls = []
    monkeypatch.setattr(redaction, '_redact_image', lambda i, t, o, c, *a: calls.append(i) or ({'matches': 0}, None))
    monkeypatch.setattr(redaction, '_redact_pdf', lambda *a: pytest.fail('pdf redactor called for image'))
    src = tmp_path / f'img{ext}'
    info = redaction._redact_file(src, tmp_path / f'out{ext}', ['x'], threading.Event())
    assert calls == [src]
    assert info['result'] == {'matches': 0} and info['error'] is None


def test_uppercase_pdf_extension(sample_pdf, tmp_path):
    src = sample_pdf.rename(sample_pdf.with_suffix('.PDF'))
    info = redaction._redact_file(src, tmp_path / 'o.PDF', ['John Doe'], threading.Event())
    assert info['result']['matches'] == 1


def test_overwrites_existing_output(sample_pdf, tmp_path):
    out = tmp_path / 'sample_redacted.pdf'
    out.write_text('stale output from a previous run')
    redaction._redact_file(sample_pdf, out, ['John Doe'], threading.Event())
    assert 'John' not in pdf_text(out)


def test_skipped_file_has_no_output_path(sample_pdf, tmp_path):
    out = tmp_path / 'sample_redacted.pdf'
    info = redaction._redact_file(sample_pdf, out, ['not in the file'], threading.Event())
    assert info == {'path': None, 'input': str(sample_pdf),
                    'result': {'matches': 0, 'pages': 1, 'skipped': True}, 'error': None}
    assert not out.exists()


def test_skipped_file_removes_stale_output_from_previous_run(sample_pdf, tmp_path):
    out = tmp_path / 'sample_redacted.pdf'
    redaction._redact_file(sample_pdf, out, ['John Doe'], threading.Event())
    assert out.exists()
    redaction._redact_file(sample_pdf, out, ['not in the file'], threading.Event())
    assert not out.exists(), 'output must reflect the current terms, not a previous run'


def test_error_is_reported_in_info(monkeypatch, tmp_path):
    monkeypatch.setattr(redaction, '_redact_image', lambda *a: (None, 'pytesseract not installed'))
    info = redaction._redact_file(tmp_path / 'a.png', tmp_path / 'b.png', ['x'], threading.Event())
    assert info['result'] is None and info['error'] == 'pytesseract not installed'
    assert info['path'] is None


def test_unsupported_extension_returns_info_dict(tmp_path):
    # Every other branch returns a dict; this one returns a (None, msg) tuple,
    # which would crash the caller's `info.get('error')`.
    info = redaction._redact_file(tmp_path / 'a.docx', tmp_path / 'b.docx', ['x'], threading.Event())
    assert isinstance(info, dict)
    assert info['error'] == "This file type isn't supported."


def test_corrupt_pdf_raises(tmp_path):
    bad = tmp_path / 'bad.pdf'
    bad.write_bytes(b'not a pdf')
    with pytest.raises(Exception):
        redaction._redact_file(bad, tmp_path / 'out.pdf', ['x'], threading.Event())
