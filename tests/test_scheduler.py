"""The parallel scheduler: survey, task splitting, ordering, worker processes."""

import logging
import os
import shutil
import signal
import threading
import time

import pymupdf as fitz
import pytest
from PIL import Image

from docucloak import config, jobs, redaction, scheduler
from conftest import make_pdf, pdf_text, wait_for_job
from test_redact_pdf import scanned_pdf

needs_tesseract = pytest.mark.skipif(not shutil.which('tesseract'), reason='tesseract binary not installed')


def payload(tmp_path, **kw):
    p = {'input_dir': str(tmp_path / 'in'), 'output_dir': str(tmp_path / 'out'), 'terms': 'Alice Smith'}
    p.update(kw)
    return p


def mixed_pdf(path, text_pages, scan_pages):
    make_pdf(path, [['Alice Smith text page']] * text_pages)
    doc = fitz.open(str(path))
    scans = fitz.open(str(scanned_pdf(path.with_suffix('.scan.pdf'), ['Alice Smith scanned'])))
    for _ in range(scan_pages):
        doc.insert_pdf(scans)
    doc.saveIncr()
    path.with_suffix('.scan.pdf').unlink()
    return path


# ─── Survey ────────────────────────────────────────────────────────────────────

def test_survey_text_pdf(tmp_path):
    sv = scheduler._survey_file(make_pdf(tmp_path / 't.pdf', [['a']] * 3), n_terms=10)
    assert (sv['kind'], sv['pages'], sv['scan_pages']) == ('text PDF', 3, [])
    assert sv['cost'] == pytest.approx(3 * 10 * scheduler.COST_TEXT_PAGE_PER_TERM)


def test_survey_scanned_and_mixed_pdfs(tmp_path):
    sv = scheduler._survey_file(scanned_pdf(tmp_path / 's.pdf', ['x']), n_terms=5)
    assert (sv['kind'], sv['scan_pages']) == ('scanned PDF', [0])
    sv = scheduler._survey_file(mixed_pdf(tmp_path / 'm.pdf', 2, 2), n_terms=5)
    assert (sv['kind'], sv['pages'], sv['scan_pages']) == ('mixed PDF', 4, [2, 3])
    assert sv['cost'] == pytest.approx(2 * 5 * scheduler.COST_TEXT_PAGE_PER_TERM + 2 * scheduler.COST_OCR_PAGE)


def test_survey_protected_and_unreadable_pdfs(tmp_path):
    doc = fitz.open()
    doc.new_page()
    doc.save(str(tmp_path / 'locked.pdf'), encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw='o', user_pw='u')
    sv = scheduler._survey_file(tmp_path / 'locked.pdf', 1)
    assert (sv['kind'], sv['error']) == ('protected PDF', redaction.MSG_PROTECTED)
    (tmp_path / 'bad.pdf').write_bytes(b'not a pdf')
    sv = scheduler._survey_file(tmp_path / 'bad.pdf', 1)
    assert (sv['kind'], sv['error']) == ('unreadable PDF', redaction.MSG_UNREADABLE_PDF)


def test_survey_image_cost_scales_with_megapixels(tmp_path):
    Image.new('RGB', (2000, 1500), 'white').save(tmp_path / 'a.png')
    sv = scheduler._survey_file(tmp_path / 'a.png', 1)
    assert (sv['kind'], sv['pages']) == ('image', 1)
    assert sv['cost'] == pytest.approx(3.0 * scheduler.COST_IMAGE_PER_MEGAPIXEL)
    (tmp_path / 'broken.jpg').write_bytes(b'x' * 600_000)  # unreadable: estimated from size
    assert scheduler._survey_file(tmp_path / 'broken.jpg', 1)['cost'] > 0


# ─── Splitting ─────────────────────────────────────────────────────────────────

def survey(kind, pages=1, scan_pages=(), cost=1.0):
    return {'kind': kind, 'pages': pages, 'scan_pages': list(scan_pages), 'cost': cost}


def test_small_files_are_one_task():
    assert scheduler._plan_tasks(survey('image', cost=0.7), 10) == [('file', [0], 0.7)]
    assert scheduler._plan_tasks(survey('text PDF', pages=25, cost=0.5), 10) == [('file', list(range(25)), 0.5)]
    assert scheduler._plan_tasks(survey('scanned PDF', pages=1, scan_pages=[0]), 10)[0][0] == 'file'


def test_large_text_pdf_is_split_into_chunks():
    tasks = scheduler._plan_tasks(survey('text PDF', pages=60), 10)
    assert [t[0] for t in tasks] == ['analyze'] * 3
    assert [t[1] for t in tasks] == [list(range(0, 25)), list(range(25, 50)), list(range(50, 60))]


def test_each_scanned_page_is_its_own_task():
    tasks = scheduler._plan_tasks(survey('mixed PDF', pages=5, scan_pages=[1, 3]), 10)
    assert [(t[0], t[1]) for t in tasks] == [('analyze', [1]), ('analyze', [3]), ('analyze', [0, 2, 4])]
    assert tasks[0][2] == scheduler.COST_OCR_PAGE


# ─── Ordering (one worker, so the start order is observable) ──────────────────

@pytest.fixture
def start_order(monkeypatch):
    order = []
    for name in ('_task_file', '_task_analyze', '_task_finalize'):
        real = getattr(scheduler, name)

        def wrapped(*a, _real=real, _name=name):
            order.append((_name[6:], os.path.basename(a[1]), tuple(a[2]) if _name == '_task_analyze' else None))
            return _real(*a)
        monkeypatch.setattr(scheduler, name, wrapped)
    monkeypatch.setattr(scheduler, 'WORKERS', 1)
    scheduler._shutdown_pool()
    yield order
    scheduler._shutdown_pool()


def test_order_express_lane_then_largest_first_and_finalize_asap(server, tmp_path, start_order, monkeypatch):
    monkeypatch.setattr(scheduler, 'TEXT_CHUNK_PAGES', 2)
    make_pdf(tmp_path / 'in' / 'tiny.pdf', [['Alice Smith']])            # express lane
    make_pdf(tmp_path / 'in' / 'big.pdf', [['Alice Smith']] * 6)         # 3 chunks
    make_pdf(tmp_path / 'in' / 'medium.pdf', [['Alice Smith']] * 4)      # 2 chunks
    _, state = server.redact_and_wait(payload(tmp_path))
    assert start_order[0] == ('file', 'tiny.pdf', None), 'cheapest file goes first in the express lane'
    rest = [(kind, name) for kind, name, _ in start_order[1:]]
    big_done = rest.index(('finalize', 'big.pdf'))
    assert rest[:big_done] == [('analyze', 'big.pdf')] * 3, 'largest file first, finalized as soon as ready'
    assert rest[big_done + 1:] == [('analyze', 'medium.pdf')] * 2 + [('finalize', 'medium.pdf')]
    assert all(r['error'] is None for r in state['results'])


# ─── Split results equal single-task results ──────────────────────────────────

def test_split_pdf_redacts_exactly_like_single_task(server, tmp_path, monkeypatch):
    pages = [[f'page {i} Alice Smith', 'keep this line', 'John Doe'] for i in range(30)]
    make_pdf(tmp_path / 'in' / 'long.pdf', pages)
    single_out = tmp_path / 'single.pdf'
    single = redaction._redact_file(tmp_path / 'in' / 'long.pdf', single_out, ['Alice Smith', 'John Doe'], threading.Event())
    monkeypatch.setattr(scheduler, 'TEXT_CHUNK_PAGES', 7)  # 5 chunks + finalize
    _, state = server.redact_and_wait(payload(tmp_path, terms='Alice Smith, John Doe'))
    [r] = state['results']
    assert r['result'] == single['result'] == {'matches': 60, 'pages': 30}
    assert pdf_text(tmp_path / 'out' / 'long_redacted.pdf') == pdf_text(single_out)


def test_progress_survey_and_page_counts(server, tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, 'TEXT_CHUNK_PAGES', 3)
    make_pdf(tmp_path / 'in' / 'a.pdf', [['Alice Smith']] * 7)
    make_pdf(tmp_path / 'in' / 'b.pdf', [['nothing']])
    Image.new('RGB', (100, 100), 'white').save(tmp_path / 'in' / 'c.png')
    _, state = server.redact_and_wait(payload(tmp_path))
    assert state['survey'] == {'counts': {'text PDF': 2, 'image': 1}, 'pages': 8, 'scan_pages': 0}, 'no worker count for the client'
    assert state['pages_total'] == 9 and state['pages_done'] == 9
    assert state['done'] == state['total'] == 3


def test_output_mirrors_input_subfolders(server, tmp_path):
    make_pdf(tmp_path / 'in' / 'a' / 'report.pdf', [['Alice Smith']])
    make_pdf(tmp_path / 'in' / 'b' / 'c' / 'report.pdf', [['Alice Smith']])
    make_pdf(tmp_path / 'in' / 'top.pdf', [['Alice Smith']])
    server.redact_and_wait(payload(tmp_path))
    out = tmp_path / 'out'
    assert sorted(str(p.relative_to(out)) for p in out.rglob('*.pdf')) == [
        'a/report_redacted.pdf', 'b/c/report_redacted.pdf', 'top_redacted.pdf']


def test_failed_split_file_reports_one_error_and_others_continue(server, tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, 'TEXT_CHUNK_PAGES', 2)
    make_pdf(tmp_path / 'in' / 'big.pdf', [['Alice Smith']] * 6)
    make_pdf(tmp_path / 'in' / 'ok.pdf', [['Alice Smith']])
    real = scheduler._task_analyze

    def flaky(*a):
        if a[2] == [2, 3]:
            raise redaction.RedactionError('page 3 is damaged')
        return real(*a)
    monkeypatch.setattr(scheduler, '_task_analyze', flaky)
    _, state = server.redact_and_wait(payload(tmp_path))
    by_name = {os.path.basename(r['input']): r for r in state['results']}
    assert len(state['results']) == 2
    assert by_name['big.pdf']['error'] == 'page 3 is damaged'
    assert by_name['ok.pdf']['error'] is None
    assert not (tmp_path / 'out' / 'big_redacted.pdf').exists()


def test_cancel_stops_split_job(server, tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, 'TEXT_CHUNK_PAGES', 1)
    make_pdf(tmp_path / 'in' / 'big.pdf', [['Alice Smith']] * 40)
    real = scheduler._task_analyze
    started = threading.Event()
    release = threading.Event()

    def slow(*a):
        started.set()
        release.wait(5)
        return real(*a)
    monkeypatch.setattr(scheduler, '_task_analyze', slow)
    server.post_json('/redact', payload(tmp_path))
    assert started.wait(5), 'no analyze task started'
    server.post_json('/cancel')
    release.set()
    state = wait_for_job(10)
    assert state['results'] == [] and not (tmp_path / 'out' / 'big_redacted.pdf').exists()


# ─── Real worker processes ─────────────────────────────────────────────────────

@pytest.fixture
def processes(monkeypatch):
    monkeypatch.setattr(scheduler, 'USE_PROCESSES', True)
    monkeypatch.setattr(scheduler, 'WORKERS', 3)
    scheduler._shutdown_pool()
    yield
    jobs._cancel_job()
    scheduler._shutdown_pool()


def results_by_name(state):
    return {os.path.basename(r['input']): (r['error'], r['result']) for r in state['results']}


@needs_tesseract
def test_processes_give_same_results_as_threads(server, tmp_path, processes, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger='docucloak')
    monkeypatch.setattr(scheduler, 'TEXT_CHUNK_PAGES', 3)
    make_pdf(tmp_path / 'in' / 'text.pdf', [['Alice Smith', 'John Doe']] * 8)
    mixed_pdf(tmp_path / 'in' / 'mixed.pdf', 2, 3)
    scanned_pdf(tmp_path / 'in' / 'scan.pdf', ['Statement for Alice Smith'])
    make_pdf(tmp_path / 'in' / 'clean.pdf', [['nothing here']])
    _, in_processes = server.redact_and_wait(payload(tmp_path, terms='Alice Smith, John Doe'), timeout=120)
    by_process = results_by_name(in_processes)
    assert by_process['text.pdf'] == (None, {'matches': 16, 'pages': 8})
    assert by_process['mixed.pdf'] == (None, {'matches': 5, 'pages': 5, 'ocr_pages': 3})
    assert by_process['scan.pdf'] == (None, {'matches': 1, 'pages': 1, 'ocr_pages': 1})
    assert by_process['clean.pdf'][1]['skipped'] is True
    # log records from worker processes reach this process's log, tagged with the worker
    worker_threads = {r.threadName for r in caplog.records if r.getMessage().startswith('Redacted ')}
    assert worker_threads and all(t.startswith('redact-worker-') for t in worker_threads)
    assert {int(t.rsplit('-', 1)[1]) for t in worker_threads} != {os.getpid()}

    monkeypatch.setattr(scheduler, 'USE_PROCESSES', False)
    _, in_threads = server.redact_and_wait(payload(tmp_path, terms='Alice Smith, John Doe'), timeout=120)
    assert results_by_name(in_threads) == by_process


def test_processes_report_protected_pdf(server, tmp_path, processes):
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), 'Alice Smith')
    (tmp_path / 'in').mkdir()
    doc.save(str(tmp_path / 'in' / 'locked.pdf'), encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw='o', user_pw='u')
    _, state = server.redact_and_wait(payload(tmp_path))
    assert results_by_name(state) == {'locked.pdf': (redaction.MSG_PROTECTED, None)}


@needs_tesseract
def test_processes_cancel_reaches_workers(server, tmp_path, processes):
    scanned_pdf(tmp_path / 'in' / 'scan.pdf', ['Alice Smith'] * 3)
    doc = fitz.open(str(tmp_path / 'in' / 'scan.pdf'))
    for _ in range(4):
        doc.insert_pdf(fitz.open(str(tmp_path / 'in' / 'scan.pdf')))  # 2^4 = 16 scanned pages
    doc.save(str(tmp_path / 'in' / 'big_scan.pdf'))
    (tmp_path / 'in' / 'scan.pdf').unlink()
    server.post_json('/redact', payload(tmp_path))
    deadline = time.time() + 30
    while jobs.job_state['pages_done'] == 0 and time.time() < deadline:
        time.sleep(0.05)
    t = time.time()
    server.post_json('/cancel')
    state = wait_for_job(30)
    assert time.time() - t < 10, 'cancel should not wait for all 16 pages'
    assert state['results'] == []


@needs_tesseract
def test_crashed_worker_is_replaced_and_job_completes(server, tmp_path, processes, caplog):
    caplog.set_level(logging.INFO, logger='docucloak')
    doc = fitz.open()
    src = fitz.open(str(scanned_pdf(tmp_path / 'one.pdf', ['Statement for Alice Smith'])))
    for _ in range(12):
        doc.insert_pdf(src)
    (tmp_path / 'in').mkdir()
    doc.save(str(tmp_path / 'in' / 'big_scan.pdf'))
    make_pdf(tmp_path / 'in' / 'letter.pdf', [['Alice Smith']])
    server.post_json('/redact', payload(tmp_path))
    deadline = time.time() + 30
    while not getattr(scheduler._pool, '_processes', None) and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(1.0)  # let OCR tasks get going
    victim = next(iter(scheduler._pool._processes))
    os.kill(victim, signal.SIGKILL)  # simulate the PDF library crashing a worker
    state = wait_for_job(120)
    assert any('worker process crashed' in r.getMessage() for r in caplog.records)
    by_name = results_by_name(state)
    assert by_name['big_scan.pdf'] == (None, {'matches': 12, 'pages': 12, 'ocr_pages': 12})
    assert by_name['letter.pdf'][0] is None
    # the replacement pool serves the next job
    _, again = server.redact_and_wait(payload(tmp_path), timeout=120)
    assert all(r['error'] is None for r in again['results'])


def test_idle_workers_are_stopped_to_free_memory(server, tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, 'WORKER_IDLE_SECONDS', 0.3)
    make_pdf(tmp_path / 'in' / 'a.pdf', [['Alice Smith']])
    server.redact_and_wait(payload(tmp_path))
    assert scheduler._pool is not None
    deadline = time.time() + 5
    while scheduler._pool is not None and time.time() < deadline:
        time.sleep(0.05)
    assert scheduler._pool is None
    _, state = server.redact_and_wait(payload(tmp_path))  # a new job starts workers again
    assert state['results'][0]['error'] is None


def test_workers_setting_from_app_conf(monkeypatch):
    monkeypatch.setattr(config, '_APP_CONFIG', {'WORKERS': '3'})
    assert config._config_int('WORKERS', 8) == 3
    monkeypatch.setattr(config, '_APP_CONFIG', {'WORKERS': 'lots'})
    assert config._config_int('WORKERS', 8) == 8
    monkeypatch.setattr(config, '_APP_CONFIG', {})
    assert config._config_int('WORKERS', 8) == 8
