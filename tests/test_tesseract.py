"""Detecting Tesseract and telling the user."""

import logging
import os
import stat
import threading

import pytest

from docucloak import redaction, tesseract
from conftest import make_pdf
from test_redact_pdf import scanned_pdf
from PIL import Image


# conftest replaces tesseract._find_tesseract with a stub that pretends Tesseract is installed
_REAL_FIND = None


@pytest.fixture(autouse=True)
def _capture_real_find():
    global _REAL_FIND
    _REAL_FIND = tesseract._find_tesseract.__closure__[0].cell_contents


def real_find():
    """Call the real detection function."""
    return _REAL_FIND()


@pytest.fixture
def no_tesseract(monkeypatch):
    monkeypatch.setattr(tesseract, '_find_tesseract', lambda: None)


# ─── Detection ─────────────────────────────────────────────────────────────────

def fake_program(folder):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / 'tesseract'
    path.write_text('#!/bin/sh\necho "tesseract 9.9.9"\n')
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_found_on_path(monkeypatch, tmp_path):
    program = fake_program(tmp_path / 'bin')
    monkeypatch.setenv('PATH', str(tmp_path / 'bin'))
    monkeypatch.setattr(tesseract, 'TESSERACT_DIRS', ())
    assert real_find() == str(program)


def test_found_in_homebrew_folder_when_not_on_path(monkeypatch, tmp_path):
    # Apps launched from Finder don't get the shell's PATH
    program = fake_program(tmp_path / 'homebrew' / 'bin')
    monkeypatch.setenv('PATH', '/usr/bin:/bin')
    monkeypatch.setattr(tesseract, 'TESSERACT_DIRS', (str(tmp_path / 'nope'), str(tmp_path / 'homebrew' / 'bin')))
    assert real_find() == str(program)


def test_not_found(monkeypatch, tmp_path):
    monkeypatch.setenv('PATH', str(tmp_path))
    monkeypatch.setattr(tesseract, 'TESSERACT_DIRS', (str(tmp_path / 'nope'),))
    assert real_find() is None


def test_status_logs_only_changes_with_version(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='docucloak')
    program = fake_program(tmp_path / 'bin')
    monkeypatch.setattr(tesseract, '_tesseract_seen', 'something else')
    monkeypatch.setattr(tesseract, '_find_tesseract', lambda: str(program))
    assert tesseract._tesseract_status() == {'tesseract': True}
    assert tesseract._tesseract_status() == {'tesseract': True}
    found = [r.getMessage() for r in caplog.records if r.getMessage().startswith('Tesseract found')]
    assert found == [f'Tesseract found: {program} (tesseract 9.9.9)'], 'logged once, with the version'
    monkeypatch.setattr(tesseract, '_find_tesseract', lambda: None)
    assert tesseract._tesseract_status() == {'tesseract': False}
    assert any(r.levelno == logging.WARNING and 'brew install tesseract' in r.getMessage() for r in caplog.records)


def test_tools_endpoint(server, monkeypatch):
    status, headers, body = server.get('/tools')
    assert (status, body) == (200, b'{"tesseract": true}')
    monkeypatch.setattr(tesseract, '_find_tesseract', lambda: None)
    assert server.get('/tools')[2] == b'{"tesseract": false}'


def test_ocr_uses_the_detected_program(monkeypatch, tmp_path):
    """Workers started before Tesseract was installed, or without Homebrew on PATH, still find it."""
    import pytesseract
    used = []
    monkeypatch.setattr(tesseract, '_find_tesseract', lambda: '/opt/somewhere/tesseract')
    monkeypatch.setattr(pytesseract, 'image_to_data', lambda img, output_type=None, **kw: (
        used.append(pytesseract.pytesseract.tesseract_cmd), {'text': [], 'left': [], 'top': [], 'width': [], 'height': []})[1])
    monkeypatch.setattr(pytesseract.pytesseract, 'tesseract_cmd', 'tesseract')
    redaction._ocr_matches(Image.new('RGB', (10, 10)), ['x'], threading.Event())
    assert used == ['/opt/somewhere/tesseract']


# ─── Jobs without Tesseract ────────────────────────────────────────────────────

def test_without_tesseract_ocr_files_are_reported_without_running_ocr(server, tmp_path, no_tesseract, monkeypatch):
    import pytesseract

    def must_not_run(*a, **k):
        raise AssertionError('OCR should not be attempted without Tesseract')
    monkeypatch.setattr(pytesseract, 'image_to_data', must_not_run)
    make_pdf(tmp_path / 'in' / 'text.pdf', [['Alice Smith']])
    scanned_pdf(tmp_path / 'in' / 'scan.pdf', ['Alice Smith'])
    Image.new('RGB', (40, 40), 'white').save(tmp_path / 'in' / 'photo.png')
    _, state = server.redact_and_wait({'input_dir': str(tmp_path / 'in'), 'output_dir': str(tmp_path / 'out'),
                                       'terms': 'Alice Smith'})
    by_name = {os.path.basename(r['input']): r for r in state['results']}
    assert by_name['text.pdf']['error'] is None, 'regular PDFs are still redacted'
    assert by_name['scan.pdf']['error'] == redaction.MSG_NEEDS_TESSERACT
    assert by_name['photo.png']['error'] == redaction.MSG_NEEDS_TESSERACT
    assert by_name['scan.pdf']['needs_tesseract'] is True and by_name['photo.png']['needs_tesseract'] is True
    assert 'needs_tesseract' not in by_name['text.pdf'], 'only files that failed for lack of Tesseract are tagged'


def test_missing_at_startup_is_logged(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger='docucloak')
    monkeypatch.setattr(tesseract, '_tesseract_seen', tesseract._NOT_CHECKED)  # as when the server starts
    monkeypatch.setattr(tesseract, '_find_tesseract', lambda: None)
    tesseract._tesseract_status()
    assert any(r.levelno == logging.WARNING and 'Tesseract not found' in r.getMessage() for r in caplog.records)
