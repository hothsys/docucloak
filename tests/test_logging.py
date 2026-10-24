"""The server log: every user action, term counts, and each file's outcome.

Redaction terms are sensitive, so the log must contain counts only, never the
terms themselves.
"""

import logging
import logging.handlers
import os
import threading

import pytest

from lib import config, jobs, logging_setup, redaction, terms
from conftest import REPO_DIR, make_pdf, wait_for_job


@pytest.fixture
def log(caplog):
    caplog.set_level(logging.DEBUG, logger='docucloak')
    return caplog


def messages(caplog, level=None):
    return [r.getMessage() for r in caplog.records
            if r.name == 'docucloak' and (level is None or r.levelno == level)]


def payload(tmp_path, **kw):
    p = {'input_dir': str(tmp_path / 'in'), 'output_dir': str(tmp_path / 'out'), 'terms': ''}
    p.update(kw)
    return p


# ─── Log configuration ─────────────────────────────────────────────────────────

def test_log_lines_include_thread_name():
    assert all('[%(threadName)s]' in h.formatter._fmt for h in logging_setup._log_handlers)


def test_no_stdout_echo_when_not_a_terminal():
    # server.sh already sends stdout to server.log; echoing would duplicate lines.
    assert not any(type(h) is logging.StreamHandler for h in logging_setup._log_handlers)


def test_pil_debug_noise_suppressed():
    assert logging.getLogger('PIL').level == logging.INFO


# ─── Start Redaction: request, terms, files ────────────────────────────────────

def test_start_redaction_logs_click_term_counts_and_every_file(server, tmp_path, log):
    make_pdf(tmp_path / 'in' / 'hit.pdf', [['Alice Smith', 'John Doe']])
    make_pdf(tmp_path / 'in' / 'miss.pdf', [['nothing sensitive']])
    terms_file = tmp_path / 'terms.txt'
    terms_file.write_text('John Doe\nAlice Smith\n')
    server.redact_and_wait(payload(tmp_path, terms='SSN', terms_file_path=str(terms_file)))
    msgs = messages(log)
    text = '\n'.join(msgs)

    assert f"Start Redaction clicked: input directory='{tmp_path / 'in'}', output directory='{tmp_path / 'out'}', terms file='{terms_file}'" in text
    assert 'Terms from the text field: 1' in msgs
    assert f'Read terms file {terms_file} from disk: 2 term(s)' in msgs
    assert 'Redaction terms in use: 3' in msgs
    assert f'Found 2 supported file(s) in {tmp_path / "in"}' in text

    hit_in = (tmp_path / 'in' / 'hit.pdf').resolve()
    miss_in = (tmp_path / 'in' / 'miss.pdf').resolve()
    assert f'Redacting {hit_in}' in msgs
    assert f'Redacting {miss_in}' in msgs
    assert f"Redacted {hit_in} -> {tmp_path / 'out' / 'hit_redacted.pdf'} (2 match(es))" in msgs
    assert f'Skipped {miss_in}: nothing matched; file left untouched, no output written' in msgs
    assert any(m.startswith('Redaction job finished in') and '1 redacted, 1 skipped (no matches), 0 failed' in m
               for m in msgs)


def test_per_term_matches_logged_at_debug(server, tmp_path, log):
    make_pdf(tmp_path / 'in' / 'doc.pdf', [['Alice Smith'], ['Alice Smith twice Alice Smith']])
    server.redact_and_wait(payload(tmp_path, terms='Alice Smith'))
    debug = messages(log, logging.DEBUG)
    assert 'doc.pdf page 1: term #1 found 1 time(s)' in debug
    assert 'doc.pdf page 2: term #1 found 2 time(s)' in debug


SECRETS = ['Alice Smith', 'John Doe', '123-45-6789']


def assert_no_secrets(caplog):
    logged = '\n'.join(r.getMessage() for r in caplog.records)
    for secret in SECRETS + ['Alice', 'John', '6789']:
        assert secret not in logged, f'redaction term {secret!r} leaked into the log'


def test_terms_never_appear_in_log_during_full_run(server, tmp_path, log, monkeypatch):
    import pytesseract
    from PIL import Image
    make_pdf(tmp_path / 'in' / 'doc.pdf', [SECRETS])
    Image.new('RGB', (200, 50), 'white').save(tmp_path / 'in' / 'scan.png')
    monkeypatch.setattr(pytesseract, 'image_to_data', lambda img, output_type=None, **kw: {
        'text': ['123-45-6789'], 'left': [5], 'top': [5], 'width': [80], 'height': [20]})
    terms_file = tmp_path / 'terms.txt'
    terms_file.write_text('John Doe\n123-45-6789\n')
    server.post_json('/log', {'event': 'page_loaded', 'value': {'inputDir': '/in', 'terms': 'Alice Smith'}})
    server.post_json('/log', {'event': 'field_changed', 'field': 'terms', 'value': 'Alice Smith, John Doe'})
    server.post_json('/log', {'event': 'bogus', 'value': 'Alice Smith'})
    _, state = server.redact_and_wait(payload(tmp_path, terms='Alice Smith', terms_file_path=str(terms_file)))
    assert sum(r['result']['matches'] for r in state['results']) == 4  # 3 in the PDF, 1 in the image
    assert any('scan.png: term #3 matched OCR text at (5, 5, 80, 20)' == r.getMessage() for r in log.records)
    assert_no_secrets(log)


def test_worker_threads_are_named(server, tmp_path, log):
    make_pdf(tmp_path / 'in' / 'doc.pdf', [['Alice Smith']])
    server.redact_and_wait(payload(tmp_path, terms='Alice Smith'))
    threads = {r.threadName for r in log.records if r.name == 'docucloak'}
    assert 'redaction-job' in threads
    assert any(t.startswith('redact-worker') for t in threads)


def test_enabled_pii_types_are_logged_without_values(server, tmp_path, log):
    make_pdf(tmp_path / 'in' / 'doc.pdf', [['alice@example.com and SSN 123-45-6789']])
    server.redact_and_wait(payload(tmp_path, pii_kinds=['email', 'ssn']))
    msgs = messages(log)
    assert 'Automatic PII detection: email address, Social Security number' in msgs
    redacted = [m for m in msgs if m.startswith('Redacted ')]
    assert redacted and '1 email address' in redacted[0] and '1 Social Security number' in redacted[0]
    assert not any('alice@example.com' in m or '123-45-6789' in m for m in msgs)


def test_fallback_terms_file_use_is_logged(server, tmp_path, log, isolated_app):
    (isolated_app / 'redact_terms.txt').write_text('John Doe')
    make_pdf(tmp_path / 'in' / 'doc.pdf', [['John Doe']])
    server.redact_and_wait(payload(tmp_path))
    assert f"No terms supplied; using fallback file {isolated_app / 'redact_terms.txt'}" in messages(log)
    assert 'Redaction terms in use: 1' in messages(log)


@pytest.mark.parametrize('kw, expected', [
    ({}, 'Redaction not started: no terms and no PII types provided'),
    ({'terms': 'x', 'input_dir': '/nope'}, "Redaction not started: invalid input directory '/nope'"),
    ({'terms': 'x', 'terms_file_path': '/nope/terms.txt'}, 'Cannot read terms file /nope/terms.txt'),
])
def test_rejected_requests_are_logged(server, tmp_path, log, kw, expected):
    (tmp_path / 'in').mkdir()
    status, _ = server.post_json('/redact', payload(tmp_path, **kw))
    assert status == 400
    assert any(m.startswith(expected) for m in messages(log))


def test_unreadable_file_is_logged(server, tmp_path, log):
    (tmp_path / 'in').mkdir()
    (tmp_path / 'in' / 'broken.pdf').write_bytes(b'garbage')
    server.redact_and_wait(payload(tmp_path, terms='x'))
    broken = (tmp_path / 'in' / 'broken.pdf').resolve()
    assert any(m.startswith(f'Cannot open PDF {broken}: ') for m in messages(log)), 'technical detail is logged'
    assert f"Not redacted {broken}: This file couldn't be opened. It may be damaged or not a real PDF." in messages(log)


def test_unexpected_task_exception_is_logged_with_traceback(server, tmp_path, log, monkeypatch):
    make_pdf(tmp_path / 'in' / 'doc.pdf', [['x']])

    def boom(*a):
        raise RuntimeError('library blew up')
    monkeypatch.setattr(redaction, '_redact_file', boom)
    _, state = server.redact_and_wait(payload(tmp_path, terms='x'))
    assert state['results'][0]['error'] == redaction.MSG_UNEXPECTED, 'the user sees a friendly message'
    assert any('library blew up' in r.getMessage() for r in log.records), 'the log keeps the detail'
    assert any('Redaction error for' in r.getMessage() and r.exc_info for r in log.records)


def test_new_job_while_running_logs_cancellation(log):
    jobs._start_job()
    jobs._start_job()
    assert 'Cancelling the previous redaction job, which is still running' in messages(log)


# ─── UI events: /log, /browse, /cancel, page load ──────────────────────────────

@pytest.mark.parametrize('field, label', [
    ('inputDir', 'Input directory'),
    ('outputDir', 'Output directory'),
    ('termsFile', 'Redact terms file'),
])
def test_typed_path_change_is_logged(server, log, field, label):
    assert server.post_json('/log', {'event': 'field_changed', 'field': field, 'value': '/a/b'}) == (200, {'status': 'logged'})
    assert f"{label} set to '/a/b' (typed)" in messages(log)


@pytest.mark.parametrize('body, expected', [
    ({'count': 3}, 3),                               # what index.html sends
    ({'value': 'Alice Smith, John Doe\nSSN'}, 3),     # text is counted, never logged
    ({'value': ''}, 0),
])
def test_terms_field_change_logs_count_only(server, log, body, expected):
    server.post_json('/log', {'event': 'field_changed', 'field': 'terms', **body})
    assert f'Redaction terms text field set to {expected} term(s) (typed)' in messages(log)
    assert_no_secrets(log)


def test_page_load_logs_saved_settings(server, log):
    server.post_json('/log', {'event': 'page_loaded', 'value': {'inputDir': '/in', 'outputDir': '/out', 'termsFile': ''}})
    assert ("UI loaded with saved settings: input directory='/in', output directory='/out', "
            "redact terms file=''") in messages(log)


@pytest.mark.parametrize('body', [{'event': 'bogus'}, {'event': 'field_changed', 'field': 'nope'}, {}])
def test_unknown_ui_event_rejected_and_logged(server, log, body):
    status, _ = server.post_json('/log', body)
    assert status == 400
    assert any(m.startswith('Unknown UI event') for m in messages(log))


def test_browse_logs_open_choice_and_cancel(server, log):
    import os
    from urllib.parse import urlencode
    server.get('/list_dir?' + urlencode({'start': '/no/such/dir', 'field': 'inputDir'}))
    assert f"Browse window opened for input directory at {os.path.expanduser('~')}" in messages(log)
    server.post_json('/log', {'event': 'browse_chosen', 'field': 'inputDir', 'value': '/Users/me/in'})
    assert "Input directory set to '/Users/me/in' (via Browse)" in messages(log)
    server.post_json('/log', {'event': 'browse_cancelled', 'field': 'termsFile'})
    assert 'Browse window for redact terms file closed; field unchanged' in messages(log)
    server.get('/list_dir?' + urlencode({'path': '/'}))
    assert sum('Browse window opened' in m for m in messages(log)) == 1, 'navigating is not logged as opening'


def test_cancel_click_is_logged(server, log):
    server.post_json('/cancel')
    assert 'Cancel clicked' in messages(log)
    assert 'Cancel requested but no redaction job is running' in messages(log)


def test_page_request_is_logged(server, log):
    server.get('/')
    assert 'UI page requested by 127.0.0.1' in messages(log)


# ─── Internal functions ────────────────────────────────────────────────────────

def test_internal_functions_log_at_debug(log, isolated_app):
    terms._parse_terms('a, b')
    config._read_app_config()
    cancel = jobs._start_job()
    jobs._update_job(1)
    jobs._cancel_job()
    jobs._finish_job(cancel)
    jobs._finish_job(threading.Event())
    debug = messages(log, logging.DEBUG)
    assert 'Parsed 2 term(s) from 4 characters of text' in debug
    assert any(m.startswith('No app config loaded from') for m in debug)
    assert 'New redaction job started' in debug
    assert 'Progress: 1 / 0 files' in debug
    assert 'Redaction job marked finished' in debug
    assert 'Superseded redaction job finished; current job state left alone' in debug
    assert 'Cancel signal sent to the running redaction job' in messages(log, logging.INFO)


# ─── Log rotation ──────────────────────────────────────────────────────────────

def test_log_rotates_at_5mb_keeping_3_old_files():
    [handler] = logging_setup._log_handlers
    assert isinstance(handler, logging.handlers.RotatingFileHandler)
    assert handler.baseFilename.endswith('/server.log')
    assert (handler.maxBytes, handler.backupCount) == (5 * 1024 * 1024, 3)


def test_rotation_moves_stdout_and_stderr_to_the_new_log(tmp_path):
    """server.sh sends stdout/stderr to server.log; after a rotation, tracebacks and
    library warnings must land in the new server.log, not the renamed old one."""
    import subprocess
    import sys
    script = tmp_path / 'rotate.py'
    script.write_text(f'''
import logging, os, sys
sys.path.insert(0, {str(REPO_DIR)!r})
sys.dont_write_bytecode = True
log = {str(tmp_path / "server.log")!r}
fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND)   # what server.sh's >> does
os.dup2(fd, 1); os.dup2(fd, 2)
from lib import config, jobs, logging_setup, redaction, terms
handler = logging_setup._RotatingLog(log, maxBytes=2000, backupCount=2)
handler.setFormatter(logging.Formatter('%(message)s'))
logger = logging.getLogger('rotation-test'); logger.propagate = False; logger.addHandler(handler)
for i in range(100):
    logger.warning('line %03d ' + 'x' * 40, i)
print('STDOUT AFTER ROTATION', flush=True)
os.write(2, b'C-LEVEL STDERR AFTER ROTATION\\n')
''')
    subprocess.run([sys.executable, str(script)], check=True, capture_output=True, timeout=60,
                   env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    current = (tmp_path / 'server.log').read_text()
    assert 'line 099' in current
    assert 'STDOUT AFTER ROTATION' in current and 'C-LEVEL STDERR AFTER ROTATION' in current
    assert (tmp_path / 'server.log.1').exists() and (tmp_path / 'server.log.2').exists()
    assert not (tmp_path / 'server.log.3').exists(), 'only 2 old files kept in this test'
    assert 'AFTER ROTATION' not in (tmp_path / 'server.log.1').read_text()
