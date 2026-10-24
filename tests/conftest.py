"""Shared fixtures for the DocuCloak test suite."""

import json
import logging
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))
sys.dont_write_bytecode = True  # keep __pycache__ out of the project
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'  # ...in worker processes too

from docucloak import config, jobs, scheduler, tesseract, web  # noqa: E402
import pymupdf as fitz  # noqa: E402

# app.py logs to <repo>/server.log on import; keep test runs out of it.
for _h in list(logging.getLogger().handlers):
    if isinstance(_h, logging.FileHandler):
        logging.getLogger().removeHandler(_h)
        _h.close()
logging.getLogger().setLevel(logging.INFO)


def make_pdf(path: Path, pages: list[list[str]]) -> Path:
    """Create a PDF where each inner list is the lines of one page."""
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    for lines in pages:
        page = doc.new_page(width=612, height=792)
        y = 72
        for line in lines:
            page.insert_text((72, y), line, fontsize=12)
            y += 30
    doc.save(str(path))
    doc.close()
    return path


def pdf_text(path: Path) -> str:
    doc = fitz.open(str(path))
    try:
        return '\n'.join(page.get_text() for page in doc)
    finally:
        doc.close()


SAMPLE_LINES = [
    'Alice Smith',
    'SSN: 123-45-6789',
    'John Doe',
    'Password: secret123',
    'Phone: (555) 123-4567',
]


def wait_for_job(timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with jobs._job_lock:
            if not jobs.job_state['running']:
                return dict(jobs.job_state)
        time.sleep(0.02)
    raise AssertionError('redaction job did not finish in time')


@pytest.fixture(autouse=True)
def isolated_app(tmp_path, monkeypatch):
    """Point APP_DIR at a scratch dir and reset global job state per test.

    The scratch APP_DIR gets copies of index.html, logo.svg, css/ and js/ but no redact_terms.txt, so the
    developer's real terms file never leaks into a test.
    """
    app_dir = tmp_path / 'appdir'
    app_dir.mkdir()
    shutil.copy(REPO_DIR / 'index.html', app_dir / 'index.html')
    shutil.copy(REPO_DIR / 'logo.svg', app_dir / 'logo.svg')
    shutil.copytree(REPO_DIR / 'css', app_dir / 'css')
    shutil.copytree(REPO_DIR / 'js', app_dir / 'js')
    monkeypatch.setattr(config, 'APP_DIR', str(app_dir))
    # Run the scheduler on threads so tests can monkeypatch the redaction
    # functions; tests/test_scheduler.py covers the real worker processes.
    monkeypatch.setattr(scheduler, 'USE_PROCESSES', False)
    # Pretend Tesseract is installed (tests stub OCR); tests of the missing case override this
    real_find = tesseract._find_tesseract
    monkeypatch.setattr(tesseract, '_find_tesseract', lambda: real_find() or '/usr/bin/false')
    with jobs._job_lock:
        jobs.job_state.update(running=False, cancel=None, phase='idle', done=0, total=0, pages_done=0,
                              pages_total=0, survey=None, cancelled=False, elapsed=None, results=[], errors=[])
    yield app_dir
    jobs._cancel_job()


def _cancel_and_drain():
    jobs._cancel_job()
    try:
        wait_for_job(timeout=10)
    except AssertionError:
        pass


@pytest.fixture
def sample_pdf(tmp_path):
    return make_pdf(tmp_path / 'in' / 'sample.pdf', [SAMPLE_LINES])


class Client:
    """Tiny HTTP client for the in-process server."""

    def __init__(self, base):
        self.base = base

    def request(self, method, path, payload=None, raw=None):
        data = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header('Content-Type', 'application/json')
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def get(self, path):
        return self.request('GET', path)

    def post_json(self, path, payload=None, raw=None):
        status, _, body = self.request('POST', path, payload, raw)
        return status, (json.loads(body) if body else None)

    def redact_and_wait(self, payload, timeout=60.0):
        status, body = self.post_json('/redact', payload)
        assert status == 200, body
        return body, wait_for_job(timeout)


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield Client(f'http://127.0.0.1:{srv.server_address[1]}')
    finally:
        _cancel_and_drain()
        srv.shutdown()
        srv.server_close()


def pytest_configure(config):
    config.addinivalue_line('markers', 'browser: drives index.html in Google Chrome via Playwright (skipped if unavailable)')
    # Don't leave a .pytest_cache/ directory in the project root
    for name in ('lfplugin', 'nfplugin', 'stepwise', 'cacheprovider'):
        plugin = config.pluginmanager.get_plugin(name)
        if plugin is not None:
            config.pluginmanager.unregister(plugin)
