"""Plan a redaction job and run its tasks in worker processes."""

import atexit
import heapq
import itertools
import logging
import multiprocessing
import os
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import pymupdf as fitz
from PIL import Image

from . import config, jobs, redaction, tesseract
from .logging_setup import logger

# ─── Parallel scheduler ────────────────────────────────────────────────────────
#
# PyMuPDF holds the GIL, so threads cannot run PDF work in parallel; work runs
# in a pool of worker processes (one per CPU core by default). A job is planned
# in three steps:
#   1. Survey: classify every file and estimate its cost (cheap: no text search).
#   2. Split: each scanned page is its own task, large text PDFs are searched in
#      chunks of pages, small files are one task; a PDF split into tasks gets a
#      final task that applies the redactions and saves.
#   3. Schedule: most expensive tasks first (so no big file is left running
#      alone at the end), and a file's final task as soon as it is ready. One
#      worker at a time serves an "express lane" of the cheapest files,
#      smallest first, so results start appearing within seconds. Workers stay
#      busy either way, so the express lane does not change the total time.

WORKERS = config._config_int('WORKERS', os.cpu_count() or 4)  # override with WORKERS= in app.conf
TEXT_CHUNK_PAGES = 25     # text pages searched per task when a PDF is split
EXPRESS_MAX_COST = 0.5    # whole-file tasks cheaper than this (seconds) may use the express lane
USE_PROCESSES = True      # tests use threads instead so monkeypatched functions apply
WORKER_IDLE_SECONDS = 120  # stop workers this long after the last job, to give their memory back
TASKS_PER_WORKER = 100     # then a worker process is replaced, so library leaks cannot build up
# Cost estimates in seconds, measured with all cores busy (OCR runs about 40%
# slower under full load than alone). They decide what to split and start first,
# and seed the time-remaining estimate until real task times calibrate it.
COST_TEXT_PAGE_PER_TERM = 0.0022
COST_OCR_PAGE = 1.4
COST_IMAGE_PER_MEGAPIXEL = 0.2
COST_FINALIZE_PER_PAGE = 0.01       # applying redactions and saving a split PDF...
COST_FINALIZE_PER_SCAN_PAGE = 0.05  # ...more where scanned page images are rewritten

_pool = None
_pool_kind = None
_pool_lock = threading.Lock()
_log_queue = None
_log_listener = None
_idle_timer = None

def _init_worker(job_id_value, log_queue):
    jobs._shared_job_id = job_id_value
    if log_queue is not None:
        # Worker process: send log records to the server process, which writes
        # them, so lines from many processes never interleave in the log file
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        root.addHandler(logging.handlers.QueueHandler(log_queue))
        root.setLevel(logging.DEBUG)
        threading.current_thread().name = f'redact-worker-{os.getpid()}'
        # Many Tesseract processes run at once: one thread each avoids oversubscribing the CPU
        os.environ['OMP_THREAD_LIMIT'] = '1'

class _Redispatch(logging.Handler):
    """Hand records from worker processes to this process's loggers."""

    def emit(self, record):
        logging.getLogger(record.name).handle(record)

def _get_pool():
    global _pool, _pool_kind, _log_queue, _log_listener, _idle_timer
    kind = 'process' if USE_PROCESSES else 'thread'
    with _pool_lock:
        if _idle_timer is not None:
            _idle_timer.cancel()
            _idle_timer = None
        if _pool is not None and _pool_kind == kind and not getattr(_pool, '_broken', False):
            return _pool
        _shutdown_pool_locked()
        ctx = multiprocessing.get_context('spawn')
        if jobs._shared_job_id is None:
            jobs._set_active_job(0)
        if kind == 'process':
            recycle = {'max_tasks_per_child': TASKS_PER_WORKER} if sys.version_info >= (3, 11) else {}
            if _log_queue is None:
                _log_queue = ctx.Queue()
                _log_listener = logging.handlers.QueueListener(_log_queue, _Redispatch())
                _log_listener.start()
            _pool = ProcessPoolExecutor(WORKERS, mp_context=ctx, initializer=_init_worker,
                                        initargs=(jobs._shared_job_id, _log_queue), **recycle)
        else:
            _pool = ThreadPoolExecutor(WORKERS, thread_name_prefix='redact-worker',
                                       initializer=_init_worker, initargs=(jobs._shared_job_id, None))
        _pool_kind = kind
        logger.info('Started %d %s worker(s)', WORKERS, kind)
        return _pool

def _shutdown_pool_locked():
    global _pool
    if _pool is not None:
        _pool.shutdown(wait=False, cancel_futures=True)
        _pool = None

def _shutdown_pool():
    with _pool_lock:
        _shutdown_pool_locked()

def _schedule_idle_shutdown():
    global _idle_timer

    def stop_if_idle():
        with jobs._job_lock:
            busy = jobs.job_state['running']
        if not busy and _pool is not None:
            _shutdown_pool()
            logger.info('Stopped idle workers to free memory')

    with _pool_lock:
        if _idle_timer is not None:
            _idle_timer.cancel()
        _idle_timer = threading.Timer(WORKER_IDLE_SECONDS, stop_if_idle)
        _idle_timer.daemon = True
        _idle_timer.start()

atexit.register(_shutdown_pool)

# Tasks run in worker processes: arguments and results must be picklable.

def _release_memory():
    # MuPDF keeps up to 256 MB of decoded pages and images cached per process
    fitz.TOOLS.store_shrink(100)

def _task_file(job_id, input_path, output_path, terms, pii_kinds):
    try:
        return redaction._redact_file(Path(input_path), Path(output_path), terms,
                                      jobs._JobCancel(job_id), pii_kinds)
    finally:
        _release_memory()

def _task_analyze(job_id, input_path, page_numbers, terms, pii_kinds):
    doc = redaction._open_pdf(input_path)
    try:
        return redaction._analyze_pages(doc, page_numbers, terms, jobs._JobCancel(job_id),
                                        Path(input_path).name, pii_kinds)
    finally:
        doc.close()
        _release_memory()

def _task_finalize(job_id, input_path, output_path, terms, rects_by_page, matches, ocr_pages, pii_counts, pii_kinds):
    input_path, output_path = Path(input_path), Path(output_path)
    if jobs._JobCancel(job_id).is_set():
        return None
    had_previous_output = redaction._remove_previous_output(output_path)
    doc = redaction._open_pdf(input_path)
    try:
        result = redaction._write_redacted_pdf(doc, rects_by_page, terms, output_path, input_path.name, matches,
                                               ocr_pages, pii_counts, pii_kinds)
    finally:
        doc.close()
        _release_memory()
    return redaction._file_info(input_path, output_path, result, None, had_previous_output)

def _survey_file(path: Path, n_terms: int) -> dict:
    """Classify a file and estimate its cost without searching it."""
    info = {'path': path, 'kind': 'image', 'pages': 1, 'scan_pages': []}
    if path.suffix.lower() == '.pdf':
        try:
            with fitz.open(str(path)) as doc:
                if doc.needs_pass:
                    return {**info, 'kind': 'protected PDF', 'error': redaction.MSG_PROTECTED, 'cost': 0}
                pages = len(doc)
                # No fonts but images: no text layer, so the page will need OCR
                scan_pages = [p.number for p in doc if not p.get_fonts() and p.get_images()]
        except Exception as e:
            logger.warning('Cannot open PDF %s: %s', path, e)
            return {**info, 'kind': 'unreadable PDF', 'error': redaction.MSG_UNREADABLE_PDF, 'cost': 0}
        kind = 'text PDF' if not scan_pages else ('scanned PDF' if len(scan_pages) == pages else 'mixed PDF')
        cost = (pages - len(scan_pages)) * n_terms * COST_TEXT_PAGE_PER_TERM + len(scan_pages) * COST_OCR_PAGE
        return {**info, 'kind': kind, 'pages': pages, 'scan_pages': scan_pages, 'cost': cost}
    try:
        with Image.open(str(path)) as img:
            megapixels = img.width * img.height / 1e6
    except Exception:
        megapixels = path.stat().st_size / 300_000  # unreadable here; the task reports the real error
    return {**info, 'cost': megapixels * COST_IMAGE_PER_MEGAPIXEL}

def _plan_tasks(survey: dict, n_terms: int):
    """Split a file into (kind, pages, cost) tasks."""
    if survey['kind'] == 'image':
        return [('file', [0], survey['cost'])]
    scan = set(survey['scan_pages'])
    text_pages = [p for p in range(survey['pages']) if p not in scan]
    if len(scan) <= 1 and len(text_pages) <= TEXT_CHUNK_PAGES:
        return [('file', list(range(survey['pages'])), survey['cost'])]
    tasks = [('analyze', [p], COST_OCR_PAGE) for p in sorted(scan)]
    for i in range(0, len(text_pages), TEXT_CHUNK_PAGES):
        chunk = text_pages[i:i + TEXT_CHUNK_PAGES]
        tasks.append(('analyze', chunk, len(chunk) * n_terms * COST_TEXT_PAGE_PER_TERM))
    return tasks

def _summarize_survey(surveys):
    counts = {}
    for sv in surveys:
        counts[sv['kind']] = counts.get(sv['kind'], 0) + 1
    return {
        'counts': counts,
        'pages': sum(sv['pages'] for sv in surveys if 'error' not in sv and sv['kind'] != 'image'),
        'scan_pages': sum(len(sv['scan_pages']) for sv in surveys),
    }

class _Progress:
    """Elapsed time and an estimate of the time remaining for one job.

    The estimate is the finish time of a simulated run of the remaining work:
    - every task has a cost estimate from the survey (text pages, OCR pages,
      images, the final save of a split PDF);
    - estimates are calibrated per kind of work from tasks that already
      finished, which learns this machine's real speed;
    - running tasks count with the time they have already spent;
    - the remaining tasks are replayed on the real number of workers, largest
      first like the scheduler, each split PDF's final save after its parts, so
      cores going idle near the end are accounted for.
    """

    PRIOR_SECONDS = 2.0  # pseudo-evidence that the uncalibrated estimates are right

    def __init__(self, workers):
        self.lock = threading.Lock()
        self.workers = workers
        self.started = time.monotonic()
        self.finished = None
        self.pending = {}   # task id -> (kind, cost, file)
        self.running = {}   # task id -> (kind, cost, file, started)
        self.done = {}      # kind -> [estimated total, actual total]
        self.completed = 0

    def elapsed(self):
        return (self.finished or time.monotonic()) - self.started

    def add(self, task_id, kind, cost, file):
        with self.lock:
            self.pending[task_id] = (kind, cost, file)

    def start(self, task_id):
        with self.lock:
            kind, cost, file = self.pending.pop(task_id, ('text', 0.0, None))
            self.running[task_id] = (kind, cost, file, time.monotonic())

    def finish(self, task_id, ok=True):
        with self.lock:
            entry = self.running.pop(task_id, None)
            if entry and ok:
                kind, cost, _, started = entry
                totals = self.done.setdefault(kind, [0.0, 0.0])
                totals[0] += cost
                totals[1] += time.monotonic() - started
                self.completed += 1

    def retry(self, task_id):
        with self.lock:
            entry = self.running.pop(task_id, None)
            if entry:
                self.pending[task_id] = entry[:3]

    def drop_file(self, file):
        with self.lock:
            for table in (self.pending, self.running):
                for task_id in [k for k, v in table.items() if v[2] == file]:
                    del table[task_id]

    def current_files(self):
        """Base names of the files being worked on right now, without duplicates."""
        with self.lock:
            names = {os.path.basename(str(entry[2])) for entry in self.running.values() if entry[2] is not None}
        return sorted(names)

    def end(self):
        with self.lock:
            self.finished = time.monotonic()
            self.pending.clear()
            self.running.clear()

    def _factor(self, kind):
        est_all = sum(e for e, _ in self.done.values())
        act_all = sum(a for _, a in self.done.values())
        overall = (act_all + self.PRIOR_SECONDS) / (est_all + self.PRIOR_SECONDS)
        est, act = self.done.get(kind, (0.0, 0.0))
        return (act + self.PRIOR_SECONDS * overall) / (est + self.PRIOR_SECONDS)

    def remaining(self):
        """Seconds left, or None while there isn't enough evidence yet."""
        with self.lock:
            if self.finished is not None or (not self.pending and not self.running):
                return 0.0
            total_tasks = self.completed + len(self.pending) + len(self.running)
            if self.completed < min(2, total_tasks):
                return None
            now = time.monotonic()
            # Running tasks: when their worker becomes free (time already spent counts)
            free_at, part_end, makespan = [], {}, 0.0
            for kind, cost, file, started in self.running.values():
                expected = cost * self._factor(kind)
                end = max(expected - (now - started), 0.1 * expected)
                free_at.append(end)
                makespan = max(makespan, end)
                if kind != 'finalize':
                    part_end[file] = max(part_end.get(file, 0.0), end)
            free_at += [0.0] * max(0, self.workers - len(free_at))
            heapq.heapify(free_at)
            # Pending work, as the scheduler takes it: a split PDF's final save as soon
            # as its parts are done, otherwise the largest task first
            pending = sorted(self.pending.values(), key=lambda t: -t[1])
            finals = {file: cost * self._factor(kind) for kind, cost, file in pending if kind == 'finalize'}
            normals = [t for t in pending if t[0] != 'finalize']
            parts_left = {file: 0 for file in finals}
            for _, _, file in normals:
                if file in parts_left:
                    parts_left[file] += 1
            ready = [(part_end.get(f, 0.0), f) for f, n in parts_left.items() if n == 0]
            heapq.heapify(ready)
            next_normal = 0
            while next_normal < len(normals) or ready:
                t = heapq.heappop(free_at)
                if ready and (ready[0][0] <= t or next_normal >= len(normals)):
                    ready_at, file = heapq.heappop(ready)
                    end = max(t, ready_at) + finals[file]
                else:
                    kind, cost, file = normals[next_normal]
                    next_normal += 1
                    end = t + cost * self._factor(kind)
                    if file in parts_left:
                        parts_left[file] -= 1
                        part_end[file] = max(part_end.get(file, 0.0), end)
                        if parts_left[file] == 0:
                            heapq.heappush(ready, (part_end[file], file))
                heapq.heappush(free_at, end)
                makespan = max(makespan, end)
            return makespan

def _run_job(job_id, cancel, files, output_for, terms, pii_kinds=()):
    """Survey, plan and run one redaction job. Runs on the 'redaction-job' thread."""
    started = time.time()
    progress = cancel.progress = _Progress(WORKERS)
    jobs._set_active_job(job_id)
    # Each detector costs about as much as one term search on a text page.
    work_terms = len(terms) + len(pii_kinds)

    def record(info):
        with jobs._job_lock:
            if jobs.job_state['cancel'] is not cancel:
                return
            jobs.job_state['results'].append(info)
            jobs.job_state['errors'].append(info.get('error'))
        jobs._update_job(1)

    def add_pages(n):
        with jobs._job_lock:
            if jobs.job_state['cancel'] is cancel:
                jobs.job_state['pages_done'] += n

    try:
        # 1. Survey
        surveys = []
        for fp in files:
            if cancel.is_set():
                return
            surveys.append(_survey_file(fp, work_terms))
        summary = _summarize_survey(surveys)
        with jobs._job_lock:
            if jobs.job_state['cancel'] is cancel:
                jobs.job_state.update(phase='redacting', survey=summary, pages_total=summary['pages'] + sum(
                    1 for sv in surveys if sv['kind'] == 'image'))
        logger.info('Surveyed %d file(s) in %.2fs: %s; %d PDF page(s), %d to OCR; using %d %s worker(s)',
                    len(files), time.time() - started,
                    ', '.join(f'{n} {k}' for k, n in sorted(summary['counts'].items())) or 'nothing',
                    summary['pages'], summary['scan_pages'], WORKERS, 'process' if USE_PROCESSES else 'thread')

        if not tesseract._tesseract_status()['tesseract']:
            # Without Tesseract, scanned PDFs and images can't be read: report them now
            # rather than sending them to workers only to fail
            for sv in surveys:
                if 'error' not in sv and sv['kind'] in ('image', 'scanned PDF', 'mixed PDF'):
                    sv['error'] = redaction.MSG_NEEDS_TESSERACT

        # 2. Split into tasks
        seq = itertools.count()
        queue = []    # heap of (rank, -cost, seq, task): rank 0 = finalize, so it goes first
        express = []  # heap of (cost, seq, task): cheap whole files, smallest first
        state = {}
        for sv in surveys:
            fp = sv['path']
            if 'error' in sv:
                record(redaction._file_info(fp, output_for(fp), None, sv['error'], False))
                continue
            plan = _plan_tasks(sv, work_terms)
            state[fp] = {'pending': sum(1 for t in plan if t[0] == 'analyze'), 'rects': {}, 'matches': 0,
                         'ocr_pages': 0, 'pii': {}, 'failed': False,
                         'pages': sv['pages'] if sv['kind'] != 'image' else 1,
                         'finalize_id': ('finalize', fp)}
            if state[fp]['pending']:  # split: its final save is still to come
                progress.add(('finalize', fp), 'finalize', sv['pages'] * COST_FINALIZE_PER_PAGE +
                             len(sv['scan_pages']) * COST_FINALIZE_PER_SCAN_PAGE, fp)
            scan = set(sv['scan_pages'])
            for kind, pages, cost in plan:
                task = {'kind': kind, 'path': fp, 'pages': pages, 'tries': 0, 'id': next(seq)}
                if sv['kind'] == 'image':
                    work = 'image'
                else:
                    work = 'ocr' if any(p in scan for p in pages) else 'text'
                progress.add(task['id'], work, cost, fp)
                if kind == 'file' and cost < EXPRESS_MAX_COST:
                    heapq.heappush(express, (cost, next(seq), task))
                else:
                    heapq.heappush(queue, (1, -cost, next(seq), task))

        def fail(fp, err):
            st = state[fp]
            if not st['failed']:
                st['failed'] = True
                progress.drop_file(fp)
                record(redaction._file_info(fp, output_for(fp), None, err, False))

        def submit(pool, task):
            fp = task['path']
            if task['kind'] == 'file':
                return pool.submit(_task_file, job_id, str(fp), str(output_for(fp)), terms, pii_kinds)
            if task['kind'] == 'analyze':
                return pool.submit(_task_analyze, job_id, str(fp), task['pages'], terms, pii_kinds)
            st = state[fp]
            return pool.submit(_task_finalize, job_id, str(fp), str(output_for(fp)), terms,
                               st['rects'], st['matches'], st['ocr_pages'], st['pii'], pii_kinds)

        # 3. Schedule
        pool = _get_pool()
        inflight = {}

        def next_task():
            # Finalize tasks first; then keep one express task running; then largest first
            if queue and queue[0][0] == 0:
                return heapq.heappop(queue)[3]
            if express and (not queue or not any(t.get('express') for t in inflight.values())):
                task = heapq.heappop(express)[2]
                task['express'] = True
                return task
            if queue:
                return heapq.heappop(queue)[3]
            return None

        while (queue or express or inflight) and not cancel.is_set():
            while len(inflight) < WORKERS:
                task = next_task()
                if task is None:
                    break
                if state[task['path']]['failed']:
                    continue
                if task['kind'] != 'finalize' and task['tries'] == 0:
                    logger.debug('Starting %s task for %s (%d page(s))', task['kind'], task['path'].name, len(task['pages']))
                progress.start(task['id'])
                inflight[submit(pool, task)] = task
            if not inflight:
                continue
            done, _ = wait(inflight, timeout=0.25, return_when=FIRST_COMPLETED)
            crashed = []
            for fut in done:
                if cancel.is_set():
                    break  # don't record outcomes of a cancelled job
                task = inflight.pop(fut)
                fp, st = task['path'], state[task['path']]
                exc = fut.exception()
                if not isinstance(exc, BrokenProcessPool):  # crashed tasks are retried below
                    progress.finish(task['id'], ok=exc is None)
                try:
                    out = fut.result()
                except BrokenProcessPool:
                    crashed.append(task)
                    continue
                except redaction.RedactionError as e:
                    fail(fp, str(e))
                    continue
                except Exception as e:
                    logger.error('Redaction error for %s: %s', fp, e, exc_info=e)
                    fail(fp, redaction.MSG_UNEXPECTED)
                    continue
                if st['failed'] or out is None:  # file already failed, or task saw the cancel
                    continue
                if task['kind'] == 'analyze':
                    rects, matches, ocr_pages, pii_counts = out
                    st['rects'].update(rects)
                    st['matches'] += matches
                    st['ocr_pages'] += ocr_pages
                    for kind, count in pii_counts.items():
                        st['pii'][kind] = st['pii'].get(kind, 0) + count
                    st['pending'] -= 1
                    add_pages(len(task['pages']))
                    if st['pending'] == 0:
                        heapq.heappush(queue, (0, 0, next(seq), {'kind': 'finalize', 'path': fp, 'pages': [], 'tries': 0,
                                                                 'id': st['finalize_id']}))
                else:
                    if task['kind'] == 'file':
                        add_pages(st['pages'])
                    record(out)
            if crashed:
                # A worker process died (e.g. a malformed file crashed the PDF
                # library); every task it was sharing the pool with fails too
                for fut, task in list(inflight.items()):
                    crashed.append(task)
                inflight.clear()
                logger.warning('A worker process crashed; restarting workers and retrying %d task(s)', len(crashed))
                pool = _get_pool()
                for task in crashed:
                    task['tries'] += 1
                    if task['tries'] >= 2:
                        logger.error('Giving up on %s: a worker process crashed on it twice', task['path'])
                        fail(task['path'], redaction.MSG_CRASHED)
                    else:
                        task.pop('express', None)
                        progress.retry(task['id'])
                        heapq.heappush(queue, (0 if task['kind'] == 'finalize' else 1, 0, next(seq), task))
        if cancel.is_set():
            for fut in inflight:
                fut.cancel()
            wait(inflight, timeout=30)
    finally:
        with jobs._job_lock:
            results = list(jobs.job_state['results']) if jobs.job_state['cancel'] is cancel else []
        redacted = sum(1 for r in results if r.get('path'))
        skipped = sum(1 for r in results if (r.get('result') or {}).get('skipped'))
        failed = sum(1 for r in results if r.get('error'))
        logger.info('Redaction job finished in %.1fs: %d redacted, %d skipped (no matches), %d failed%s',
                    time.time() - started, redacted, skipped, failed,
                    ' (cancelled)' if cancel.is_set() else '')
        progress.end()
        with jobs._job_lock:
            if jobs.job_state['cancel'] is cancel:
                jobs.job_state['elapsed'] = round(progress.elapsed(), 1)
        jobs._finish_job(cancel)
        _schedule_idle_shutdown()

