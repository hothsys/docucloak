#!/usr/bin/env python3
"""
DocuCloak — web-based PDF and image redactor.
Select input directory, output directory, and a list of terms.
Multithreaded file processing with black-fill redaction.
"""

import os
import re
import sys
import json
import threading
import time
import shutil
import subprocess
from pathlib import Path
import atexit
import heapq
import itertools
import multiprocessing
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool

APP_DIR = os.path.dirname(os.path.abspath(__file__))

import logging
import logging.handlers

LOG_FILE = os.path.join(APP_DIR, 'server.log')
LOG_MAX_BYTES = 5 * 1024 * 1024  # rotate at 5 MB: server.log, server.log.1 ... server.log.3
LOG_BACKUPS = 3

class _RotatingLog(logging.handlers.RotatingFileHandler):
    """Rotating log that also moves stdout/stderr along when it rotates.

    server.sh sends the process's stdout/stderr to server.log, so without this,
    tracebacks and PDF library warnings would keep going to the renamed file.
    """

    def doRollover(self):
        super().doRollover()
        if self.stream is not None and not sys.stdout.isatty():
            for fd in (1, 2):
                os.dup2(self.stream.fileno(), fd)

_log_handlers = [_RotatingLog(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS)]
# server.sh redirects stdout into LOG_FILE, so only echo to stdout when it is an
# interactive terminal; otherwise every line would be written to the log twice.
if sys.stdout.isatty():
    _log_handlers.append(logging.StreamHandler(sys.stdout))
logging.basicConfig(
    level=logging.DEBUG,
    # threadName shows which request/worker thread wrote each line
    format='%(asctime)s %(levelname)s [%(threadName)s] %(message)s',
    handlers=_log_handlers,
)
logging.getLogger('PIL').setLevel(logging.INFO)  # PIL logs every PNG chunk at DEBUG
logger = logging.getLogger('docucloak')

# ─── Auto-install dependencies ─────────────────────────────────────────────────

def _ensure_dependencies():
    required = {
        'pymupdf': 'PyMuPDF>=1.23.0',
        'PIL': 'Pillow>=10.0.0',
        'pytesseract': 'pytesseract>=0.3.10',
    }
    missing = []
    for module, package in required.items():
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    if missing:
        logger.info('Installing missing dependencies: %s', ', '.join(missing))
        subprocess.check_call(
            [sys.executable, '-m', 'pip', 'install', '--quiet'] + missing,
        )
        logger.info('Dependencies installed.')
    else:
        logger.debug('All dependencies present: %s', ', '.join(required))

_ensure_dependencies()

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
from urllib.parse import urlparse, parse_qs  # noqa: E402

# ─── Config ────────────────────────────────────────────────────────────────────

def _read_app_config():
    cfg = {}
    conf_path = os.path.join(APP_DIR, 'app.conf')
    try:
        with open(conf_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, val = line.split('=', 1)
                cfg[key.strip()] = val.strip().strip("\"'")
    except OSError as e:
        logger.debug('No app config loaded from %s: %s', conf_path, e)
        return cfg
    logger.debug('Loaded app config from %s: %r', conf_path, cfg)
    return cfg

_APP_CONFIG = _read_app_config()

def _code_fingerprint():
    """Hash of app.py on disk. Compared with the one taken at startup, it tells the
    page when the running server is older than the files (updated but not restarted)."""
    import hashlib
    try:
        with open(os.path.abspath(__file__), 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None

_STARTED_FINGERPRINT = _code_fingerprint()
APP_NAME = _APP_CONFIG.get('APP_NAME') or os.path.basename(APP_DIR)
APP_TITLE = _APP_CONFIG.get('APP_TITLE') or APP_NAME

# ─── Job state ──────────────────────────────────────────────────────────────────

job_state = {
    'running': False,
    'cancel': None,
    'phase': 'idle',      # idle | surveying | redacting
    'done': 0,
    'total': 0,
    'pages_done': 0,
    'pages_total': 0,
    'survey': None,
    'cancelled': False,
    'elapsed': None,
    'results': [],
    'errors': [],
}

_job_lock = threading.Lock()
_job_ids = itertools.count(1)

def _start_job():
    with _job_lock:
        if job_state['cancel'] is not None:
            logger.info('Cancelling the previous redaction job, which is still running')
            job_state['cancel'].set()
        cancel = threading.Event()
        cancel.job_id = next(_job_ids)
        job_state.update(cancel=cancel, running=True, phase='surveying', done=0, total=0,
                         pages_done=0, pages_total=0, survey=None, cancelled=False, elapsed=None, results=[], errors=[])
    _set_active_job(cancel.job_id)
    logger.debug('New redaction job started')
    return cancel

def _finish_job(cancel):
    with _job_lock:
        if job_state['cancel'] is cancel:
            job_state['running'] = False
            job_state['cancel'] = None
            job_state['phase'] = 'idle'
            logger.debug('Redaction job marked finished')
        else:
            logger.debug('Superseded redaction job finished; current job state left alone')

def _cancel_job():
    with _job_lock:
        if job_state['cancel'] is not None:
            job_state['cancel'].set()
            job_state['cancelled'] = True
            _set_active_job(0)  # tells worker processes to stop
            logger.info('Cancel signal sent to the running redaction job')
        else:
            logger.debug('Cancel requested but no redaction job is running')

def _update_job(done_delta=0):
    with _job_lock:
        job_state['done'] += done_delta
        done, total = job_state['done'], job_state['total']
    logger.debug('Progress: %d / %d files', done, total)

# ─── Tesseract detection ───────────────────────────────────────────────────────

# Apps launched from Finder don't get the shell's PATH, so also look where Homebrew installs
TESSERACT_DIRS = ('/opt/homebrew/bin', '/usr/local/bin')
_NOT_CHECKED = object()
_tesseract_seen = _NOT_CHECKED  # last detection result, so only changes are logged

def _find_tesseract():
    """Path of the tesseract program, or None. Cheap enough to call per request."""
    path = shutil.which('tesseract')
    if path is None:
        for folder in TESSERACT_DIRS:
            candidate = os.path.join(folder, 'tesseract')
            if os.access(candidate, os.X_OK):
                return candidate
    return path

def _tesseract_status():
    """Detect Tesseract and log when it appears, disappears or changes."""
    global _tesseract_seen
    path = _find_tesseract()
    if path != _tesseract_seen:
        _tesseract_seen = path
        if path:
            try:
                version = subprocess.run([path, '--version'], capture_output=True, text=True, timeout=10)
                version = (version.stdout or version.stderr).splitlines()[0]
            except (OSError, subprocess.SubprocessError, IndexError):
                version = 'unknown version'
            logger.info('Tesseract found: %s (%s)', path, version)
        else:
            logger.warning('Tesseract not found on PATH or in %s; scanned PDFs and images cannot be redacted '
                           '(install with: brew install tesseract)', ', '.join(TESSERACT_DIRS))
    return {'tesseract': path is not None}

# ─── Redaction logic ───────────────────────────────────────────────────────────

import pymupdf as fitz  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

SUPPORTED_EXTS = {'.pdf', '.jpg', '.jpeg', '.png', '.tif', '.tiff', '.bmp'}

def _parse_terms(raw_text: str) -> list[str]:
    terms = []
    seen = set()
    for line in raw_text.splitlines():
        for part in line.split(','):
            term = part.strip()
            if term and term not in seen:
                seen.add(term)
                terms.append(term)
    logger.debug('Parsed %d term(s) from %d characters of text', len(terms), len(raw_text))
    return terms

OCR_DPI = 300       # resolution scanned PDF pages are rendered at for OCR
OCR_TIMEOUT = 300   # seconds before Tesseract is abandoned on one page or image

class RedactionError(Exception):
    """A file that cannot be redacted. Reported as an error, never as a skip."""

# Shown to the user in the results list; technical details go to the server log
MSG_PROTECTED = 'This PDF is password-protected. Remove the password and try again.'
MSG_UNREADABLE_PDF = "This file couldn't be opened. It may be damaged or not a real PDF."
MSG_UNREADABLE_IMAGE = "This image couldn't be opened. It may be damaged."
MSG_NEEDS_TESSERACT = 'Reading text in scanned pages and images needs Tesseract. Install it with: brew install tesseract'
MSG_OCR_TIMEOUT = 'Reading the text in this file took too long, so it was stopped.'
MSG_CRASHED = "This file couldn't be processed. It may be damaged."
MSG_UNEXPECTED = 'Something went wrong with this file. Details are in the server log.'

def _ocr_matches(img, terms: list[str], cancel_event):
    """OCR a PIL image and find every term occurrence.

    Words are joined per OCR line, so multi-word terms ("Alice Smith") match
    across word boxes. Returns a list of (term_no, [(x, y, w, h), ...]) with the
    word boxes each occurrence covers, or None if cancelled.
    """
    import pytesseract
    tesseract = _find_tesseract()
    if tesseract:
        pytesseract.pytesseract.tesseract_cmd = tesseract
    try:
        data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT, timeout=OCR_TIMEOUT)
    except pytesseract.TesseractNotFoundError as e:
        logger.error('Tesseract not found: %s', e)
        raise RedactionError(MSG_NEEDS_TESSERACT) from e
    except RuntimeError as e:
        if 'timeout' in str(e).lower():
            logger.warning('OCR gave up after %ds', OCR_TIMEOUT)
            raise RedactionError(MSG_OCR_TIMEOUT) from e
        raise
    n = len(data['text'])
    lines = {}
    for i in range(n):
        if data['text'][i].strip():
            key = tuple(data.get(k, [0] * n)[i] for k in ('block_num', 'par_num', 'line_num'))
            lines.setdefault(key, []).append(i)
    found = []
    for word_indexes in lines.values():
        if cancel_event.is_set():
            return None
        line, spans = '', []
        for i in word_indexes:
            if line:
                line += ' '
            start = len(line)
            line += data['text'][i].strip()
            spans.append((start, len(line), i))
        lower = line.lower()
        for term_no, term in enumerate(terms, 1):
            needle = term.lower()
            pos = lower.find(needle)
            while pos != -1:
                stop = pos + len(needle)
                boxes = [(data['left'][i], data['top'][i], data['width'][i], data['height'][i])
                         for s, e, i in spans if s < stop and e > pos]
                boxes = [b for b in boxes if b[2] > 0 and b[3] > 0]
                if boxes:
                    found.append((term_no, boxes))
                pos = lower.find(needle, stop)
    return found

def _hidden_matches(doc, terms: list[str]) -> int:
    """Count terms found in document properties, XMP metadata and bookmarks."""
    hidden = [v for v in doc.metadata.values() if v]
    hidden.append(doc.get_xml_metadata() or '')
    hidden.extend(entry[1] for entry in doc.get_toc())
    hidden = [h.lower() for h in hidden]
    return sum(h.count(term.lower()) for h in hidden for term in terms)

def _open_pdf(path):
    doc = fitz.open(str(path))
    if doc.needs_pass:
        doc.close()
        raise RedactionError(MSG_PROTECTED)
    return doc

def _analyze_pages(doc, page_numbers, terms: list[str], cancel_event, label: str):
    """Find everything to redact on the given pages, without changing the document.

    Pages without a text layer but with images are scanned pages and are OCR'd.
    Returns ({page_number: [(x0, y0, x1, y1), ...]}, matches, ocr_pages) with
    rects in page coordinates, or None if cancelled.
    """
    rects_by_page, matches, ocr_pages = {}, 0, 0
    for pno in page_numbers:
        if cancel_event.is_set():
            return None
        page = doc[pno]
        rects = []
        textpage = page.get_textpage()  # extract the text once, search it for every term
        for term_no, term in enumerate(terms, 1):
            found = page.search_for(term, textpage=textpage)
            if found:
                # Terms are sensitive: log the term's position in the list, never its text
                logger.debug('%s page %d: term #%d found %d time(s)', label, pno + 1, term_no, len(found))
                rects.extend(tuple(r) for r in found)
                matches += len(found)
        if not textpage.extractText().strip() and page.get_images():
            try:
                import pytesseract  # noqa: F401
            except ImportError:
                logger.error('%s page %d is scanned and needs OCR, but pytesseract is not installed', label, pno + 1)
                raise RedactionError(MSG_NEEDS_TESSERACT)
            ocr_pages += 1
            logger.debug('%s page %d has no text layer; running OCR', label, pno + 1)
            pix = page.get_pixmap(dpi=OCR_DPI)
            found = _ocr_matches(Image.frombytes('RGB', (pix.width, pix.height), pix.samples), terms, cancel_event)
            if found is None:
                return None
            # Pixmap pixels -> page points, then undo any page rotation
            to_page = fitz.Matrix(72 / OCR_DPI, 72 / OCR_DPI) * page.derotation_matrix
            for term_no, boxes in found:
                logger.debug('%s page %d: term #%d matched OCR text (%d word box(es))', label, pno + 1, term_no, len(boxes))
                rects.extend(tuple(fitz.Rect(x, y, x + w, y + h) * to_page) for x, y, w, h in boxes)
            matches += len(found)
        if rects:
            rects_by_page[pno] = rects
    return rects_by_page, matches, ocr_pages

def _write_redacted_pdf(doc, rects_by_page, terms: list[str], output_path: Path, label: str, matches: int, ocr_pages: int):
    """Apply the redactions and save. If nothing matched anywhere, write nothing."""
    for pno, rects in rects_by_page.items():
        page = doc[pno]
        for r in rects:
            page.add_redact_annot(fitz.Rect(r), fill=(0, 0, 0))
        # Remove the text and blank the image pixels under each box, not just cover them
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)
    metadata_matches = _hidden_matches(doc, terms)
    result = {'matches': matches, 'pages': len(doc)}
    if ocr_pages:
        result['ocr_pages'] = ocr_pages
    if metadata_matches:
        result['metadata_matches'] = metadata_matches
    if matches == 0 and metadata_matches == 0:
        return {**result, 'skipped': True}
    # Properties, XMP metadata and bookmarks can repeat sensitive text: clear them
    bookmarks = len(doc.get_toc())
    doc.set_metadata({})
    doc.del_xml_metadata()
    doc.set_toc([])
    logger.debug('%s: cleared document metadata and %d bookmark(s)', label, bookmarks)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # garbage=3 drops objects no longer referenced (old images, metadata) so
    # redacted content cannot be recovered from the file
    doc.save(str(output_path), garbage=3, deflate=True)
    return result

def _redact_pdf(input_path: Path, terms: list[str], output_path: Path, cancel_event):
    """Redact matches into output_path. If nothing matches, write nothing."""
    logger.debug('Searching PDF %s for %d term(s)', input_path, len(terms))
    try:
        doc = _open_pdf(input_path)
    except RedactionError as e:
        return None, str(e)
    try:
        analysis = _analyze_pages(doc, range(len(doc)), terms, cancel_event, input_path.name)
        if analysis is None:
            logger.info('Cancelled while searching %s', input_path)
            return None, 'cancelled'
        rects_by_page, matches, ocr_pages = analysis
        return _write_redacted_pdf(doc, rects_by_page, terms, output_path, input_path.name, matches, ocr_pages), None
    except RedactionError as e:
        return None, str(e)
    finally:
        doc.close()

def _redact_image(input_path: Path, terms: list[str], output_path: Path, cancel_event):
    """Redact OCR matches into output_path. If nothing matches, write nothing."""
    try:
        import pytesseract  # noqa: F401
    except ImportError:
        logger.error('Cannot redact %s: pytesseract not installed', input_path)
        return None, MSG_NEEDS_TESSERACT
    logger.debug('Running OCR on image %s', input_path)
    try:
        img = Image.open(str(input_path))
        # Convert to RGB for drawing
        if img.mode != 'RGB':
            img = img.convert('RGB')
    except (OSError, Image.DecompressionBombError) as e:
        logger.warning('Cannot open image %s: %s', input_path, e)
        return None, MSG_UNREADABLE_IMAGE
    try:
        found = _ocr_matches(img, terms, cancel_event)
    except RedactionError as e:
        return None, str(e)
    if found is None:
        logger.info('Cancelled while searching %s', input_path)
        return None, 'cancelled'
    draw = ImageDraw.Draw(img)
    for term_no, boxes in found:
        for x, y, w, h in boxes:
            logger.debug('%s: term #%d matched OCR text at (%d, %d, %d, %d)', input_path.name, term_no, x, y, w, h)
            draw.rectangle([x, y, x + w, y + h], fill=(0, 0, 0))
    if not found:
        return {'matches': 0, 'skipped': True}, None
    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(output_path))
    return {'matches': len(found)}, None

def _remove_previous_output(output_path: Path) -> bool:
    """Drop output from a previous run so the output dir always reflects the
    current terms (do not invent a new name)."""
    if output_path.exists():
        output_path.unlink()
        return True
    return False

def _file_info(input_path, output_path, result, err, had_previous_output):
    """Log a file's outcome and build its entry for the results list."""
    skipped = bool(result and result.get('skipped'))
    if err:
        logger.warning('Not redacted %s: %s', input_path, err)
    elif skipped:
        logger.info('Skipped %s: no redaction terms found; file left untouched, no output written%s',
                    input_path, ' (removed stale output from a previous run)' if had_previous_output else '')
    else:
        extra = ''
        if result.get('metadata_matches'):
            extra += f", {result['metadata_matches']} in metadata/bookmarks"
        if result.get('ocr_pages'):
            extra += f", {result['ocr_pages']} scanned page(s) OCR'd"
        logger.info('Redacted %s -> %s (%d match(es)%s)', input_path, output_path, result['matches'], extra)
    info = {
        'path': None if (err or skipped) else str(output_path),
        'input': str(input_path),
        'result': result,
        'error': err,
    }
    if err == MSG_NEEDS_TESSERACT:
        info['needs_tesseract'] = True  # lets the UI explain the cause in its summary
    return info

def _redact_file(input_path: Path, output_path: Path, terms: list[str], cancel_event):
    logger.info('Redacting %s', input_path)
    had_previous_output = _remove_previous_output(output_path)
    suffix = input_path.suffix.lower()
    if suffix == '.pdf':
        result, err = _redact_pdf(input_path, terms, output_path, cancel_event)
    elif suffix in {'.jpg', '.jpeg', '.png', '.tif', '.tiff', '.bmp'}:
        result, err = _redact_image(input_path, terms, output_path, cancel_event)
    else:
        result, err = None, "This file type isn't supported."
    return _file_info(input_path, output_path, result, err, had_previous_output)

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

def _config_int(key, default):
    try:
        return max(1, int(_APP_CONFIG.get(key) or default))
    except ValueError:
        return default

WORKERS = _config_int('WORKERS', os.cpu_count() or 4)  # override with WORKERS= in app.conf
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

_shared_job_id = None  # multiprocessing.Value: id of the job workers should be running
_pool = None
_pool_kind = None
_pool_lock = threading.Lock()
_log_queue = None
_log_listener = None
_idle_timer = None

class _JobCancel:
    """Cancel flag that works in worker processes: a job counts as cancelled
    once the shared active-job id no longer matches it."""

    def __init__(self, job_id):
        self.job_id = job_id

    def is_set(self):
        return _shared_job_id is None or _shared_job_id.value != self.job_id

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.is_set() and (deadline is None or time.monotonic() < deadline):
            time.sleep(0.05)
        return self.is_set()

def _set_active_job(job_id):
    global _shared_job_id
    if _shared_job_id is None:
        _shared_job_id = multiprocessing.get_context('spawn').Value('q', 0)
    _shared_job_id.value = job_id

def _init_worker(job_id_value, log_queue):
    global _shared_job_id
    _shared_job_id = job_id_value
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
    global _pool, _pool_kind, _shared_job_id, _log_queue, _log_listener, _idle_timer
    kind = 'process' if USE_PROCESSES else 'thread'
    with _pool_lock:
        if _idle_timer is not None:
            _idle_timer.cancel()
            _idle_timer = None
        if _pool is not None and _pool_kind == kind and not getattr(_pool, '_broken', False):
            return _pool
        _shutdown_pool_locked()
        ctx = multiprocessing.get_context('spawn')
        if _shared_job_id is None:
            _set_active_job(0)
        if kind == 'process':
            recycle = {'max_tasks_per_child': TASKS_PER_WORKER} if sys.version_info >= (3, 11) else {}
            if _log_queue is None:
                _log_queue = ctx.Queue()
                _log_listener = logging.handlers.QueueListener(_log_queue, _Redispatch())
                _log_listener.start()
            _pool = ProcessPoolExecutor(WORKERS, mp_context=ctx, initializer=_init_worker,
                                        initargs=(_shared_job_id, _log_queue), **recycle)
        else:
            _pool = ThreadPoolExecutor(WORKERS, thread_name_prefix='redact-worker',
                                       initializer=_init_worker, initargs=(_shared_job_id, None))
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
        with _job_lock:
            busy = job_state['running']
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

def _task_file(job_id, input_path, output_path, terms):
    try:
        return _redact_file(Path(input_path), Path(output_path), terms, _JobCancel(job_id))
    finally:
        _release_memory()

def _task_analyze(job_id, input_path, page_numbers, terms):
    doc = _open_pdf(input_path)
    try:
        return _analyze_pages(doc, page_numbers, terms, _JobCancel(job_id), Path(input_path).name)
    finally:
        doc.close()
        _release_memory()

def _task_finalize(job_id, input_path, output_path, terms, rects_by_page, matches, ocr_pages):
    input_path, output_path = Path(input_path), Path(output_path)
    if _JobCancel(job_id).is_set():
        return None
    had_previous_output = _remove_previous_output(output_path)
    doc = _open_pdf(input_path)
    try:
        result = _write_redacted_pdf(doc, rects_by_page, terms, output_path, input_path.name, matches, ocr_pages)
    finally:
        doc.close()
        _release_memory()
    return _file_info(input_path, output_path, result, None, had_previous_output)

def _survey_file(path: Path, n_terms: int) -> dict:
    """Classify a file and estimate its cost without searching it."""
    info = {'path': path, 'kind': 'image', 'pages': 1, 'scan_pages': []}
    if path.suffix.lower() == '.pdf':
        try:
            with fitz.open(str(path)) as doc:
                if doc.needs_pass:
                    return {**info, 'kind': 'protected PDF', 'error': MSG_PROTECTED, 'cost': 0}
                pages = len(doc)
                # No fonts but images: no text layer, so the page will need OCR
                scan_pages = [p.number for p in doc if not p.get_fonts() and p.get_images()]
        except Exception as e:
            logger.warning('Cannot open PDF %s: %s', path, e)
            return {**info, 'kind': 'unreadable PDF', 'error': MSG_UNREADABLE_PDF, 'cost': 0}
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

def _run_job(job_id, cancel, files, output_for, terms):
    """Survey, plan and run one redaction job. Runs on the 'redaction-job' thread."""
    started = time.time()
    progress = cancel.progress = _Progress(WORKERS)
    _set_active_job(job_id)

    def record(info):
        with _job_lock:
            if job_state['cancel'] is not cancel:
                return
            job_state['results'].append(info)
            job_state['errors'].append(info.get('error'))
        _update_job(1)

    def add_pages(n):
        with _job_lock:
            if job_state['cancel'] is cancel:
                job_state['pages_done'] += n

    try:
        # 1. Survey
        surveys = []
        for fp in files:
            if cancel.is_set():
                return
            surveys.append(_survey_file(fp, len(terms)))
        summary = _summarize_survey(surveys)
        with _job_lock:
            if job_state['cancel'] is cancel:
                job_state.update(phase='redacting', survey=summary, pages_total=summary['pages'] + sum(
                    1 for sv in surveys if sv['kind'] == 'image'))
        logger.info('Surveyed %d file(s) in %.2fs: %s; %d PDF page(s), %d to OCR; using %d %s worker(s)',
                    len(files), time.time() - started,
                    ', '.join(f'{n} {k}' for k, n in sorted(summary['counts'].items())) or 'nothing',
                    summary['pages'], summary['scan_pages'], WORKERS, 'process' if USE_PROCESSES else 'thread')

        if not _tesseract_status()['tesseract']:
            # Without Tesseract, scanned PDFs and images can't be read: report them now
            # rather than sending them to workers only to fail
            for sv in surveys:
                if 'error' not in sv and sv['kind'] in ('image', 'scanned PDF', 'mixed PDF'):
                    sv['error'] = MSG_NEEDS_TESSERACT

        # 2. Split into tasks
        seq = itertools.count()
        queue = []    # heap of (rank, -cost, seq, task): rank 0 = finalize, so it goes first
        express = []  # heap of (cost, seq, task): cheap whole files, smallest first
        state = {}
        for sv in surveys:
            fp = sv['path']
            if 'error' in sv:
                record(_file_info(fp, output_for(fp), None, sv['error'], False))
                continue
            plan = _plan_tasks(sv, len(terms))
            state[fp] = {'pending': sum(1 for t in plan if t[0] == 'analyze'), 'rects': {}, 'matches': 0,
                         'ocr_pages': 0, 'failed': False, 'pages': sv['pages'] if sv['kind'] != 'image' else 1,
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
                record(_file_info(fp, output_for(fp), None, err, False))

        def submit(pool, task):
            fp = task['path']
            if task['kind'] == 'file':
                return pool.submit(_task_file, job_id, str(fp), str(output_for(fp)), terms)
            if task['kind'] == 'analyze':
                return pool.submit(_task_analyze, job_id, str(fp), task['pages'], terms)
            st = state[fp]
            return pool.submit(_task_finalize, job_id, str(fp), str(output_for(fp)), terms,
                               st['rects'], st['matches'], st['ocr_pages'])

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
                except RedactionError as e:
                    fail(fp, str(e))
                    continue
                except Exception as e:
                    logger.error('Redaction error for %s: %s', fp, e, exc_info=e)
                    fail(fp, MSG_UNEXPECTED)
                    continue
                if st['failed'] or out is None:  # file already failed, or task saw the cancel
                    continue
                if task['kind'] == 'analyze':
                    rects, matches, ocr_pages = out
                    st['rects'].update(rects)
                    st['matches'] += matches
                    st['ocr_pages'] += ocr_pages
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
                        fail(task['path'], MSG_CRASHED)
                    else:
                        task.pop('express', None)
                        progress.retry(task['id'])
                        heapq.heappush(queue, (0 if task['kind'] == 'finalize' else 1, 0, next(seq), task))
        if cancel.is_set():
            for fut in inflight:
                fut.cancel()
            wait(inflight, timeout=30)
    finally:
        with _job_lock:
            results = list(job_state['results']) if job_state['cancel'] is cancel else []
        redacted = sum(1 for r in results if r.get('path'))
        skipped = sum(1 for r in results if (r.get('result') or {}).get('skipped'))
        failed = sum(1 for r in results if r.get('error'))
        logger.info('Redaction job finished in %.1fs: %d redacted, %d skipped (no matches), %d failed%s',
                    time.time() - started, redacted, skipped, failed,
                    ' (cancelled)' if cancel.is_set() else '')
        progress.end()
        with _job_lock:
            if job_state['cancel'] is cancel:
                job_state['elapsed'] = round(progress.elapsed(), 1)
        _finish_job(cancel)
        _schedule_idle_shutdown()

# ─── HTTP Handler ───────────────────────────────────────────────────────────────

class Handler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Suppress noisy access logs; keep errors visible
        pass

    def _json_response(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == '/tools':
            self._json_response(_tesseract_status())
            return
        if path == '/version':
            current = _code_fingerprint()
            outdated = current is not None and current != _STARTED_FINGERPRINT
            if outdated:
                logger.warning('app.py changed since this server started; restart it to use the new version')
            self._json_response({'outdated': outdated})
            return
        if path == '/list_dir':
            query = parse_qs(parsed.query)
            arg = lambda key: (query.get(key) or [''])[0]
            self._json_response(*_list_dir(arg('path'), arg('start'), arg('mode') == 'file', arg('field')))
            return
        if path == '/status':
            with _job_lock:
                self._json_response({
                    'running': job_state['running'],
                    'phase': job_state['phase'],
                    'done': job_state['done'],
                    'total': job_state['total'],
                    'pages_done': job_state['pages_done'],
                    'pages_total': job_state['pages_total'],
                    'survey': job_state['survey'],
                    'cancelled': job_state['cancelled'],
                    **_timing(job_state),
                    'results': job_state['results'],
                    'errors': job_state['errors'],
                })
            return
        if path == '/' or path == '/index.html':
            logger.info('UI page requested by %s', self.client_address[0])
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            # The logo URL carries logo.svg's modification time, so any browser
            # (Safari included) fetches a replaced logo instead of a cached one.
            try:
                logo_version = str(int(os.path.getmtime(os.path.join(APP_DIR, 'logo.svg'))))
            except OSError:
                logo_version = '0'
            with open(os.path.join(APP_DIR, 'index.html'), 'r') as f:
                content = (f.read()
                           .replace('{{HOME_DIR}}', os.path.expanduser('~'))
                           .replace('{{LOGO_VERSION}}', logo_version)
                           .replace('{{ASSET_VERSION}}', _asset_version()))
            self.wfile.write(content.encode())
            return
        asset = _STATIC_PATH.match(path)
        if asset:
            self._send_static(asset.group(1), asset.group(2))
            return
        if path == '/logo.svg':
            with open(os.path.join(APP_DIR, 'logo.svg'), 'rb') as f:
                content = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'image/svg+xml')
            # Revalidate every load so an updated logo.svg shows up immediately
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(content)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length).decode('utf-8', 'ignore')
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            logger.warning('Ignoring malformed JSON body on %s', path)
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        if path == '/log':
            self._log_ui_event(payload)
            return

        if path == '/redact':
            terms_raw = payload.get('terms', '')
            terms_file_content = payload.get('terms_file_content', '')
            terms_file_path = payload.get('terms_file_path', '').strip()
            input_dir = payload.get('input_dir', '')
            output_dir = payload.get('output_dir', '')
            logger.info('Start Redaction clicked: input directory=%r, output directory=%r, terms file=%r',
                        input_dir, output_dir, terms_file_path)
            text_terms = _parse_terms(terms_raw)
            # Only counts are logged; the terms themselves are sensitive.
            logger.info('Terms from the text field: %d', len(text_terms))
            if terms_file_path:
                # Read the terms file from disk on every request so edits made
                # between runs are always picked up.
                terms_file_path = os.path.expanduser(terms_file_path)
                try:
                    with open(terms_file_path, 'r', encoding='utf-8-sig', errors='ignore') as f:
                        file_text = f.read()
                except OSError as e:
                    logger.error('Cannot read terms file %s: %s', terms_file_path, e)
                    self._json_response({'error': "The terms file couldn't be read. Check that it still exists at that location, or choose it again."}, 400)
                    return
                file_terms = _parse_terms(file_text)
                logger.info('Read terms file %s from disk: %d term(s)', terms_file_path, len(file_terms))
                terms_file_content = file_text + '\n' + terms_file_content if terms_file_content else file_text
            combined_raw = terms_raw
            if terms_file_content and terms_file_content.strip():
                combined_raw = terms_raw + '\n' + terms_file_content if terms_raw else terms_file_content
            terms = _parse_terms(combined_raw)
            if not terms:
                default_terms_path = os.path.join(APP_DIR, 'redact_terms.txt')
                if os.path.isfile(default_terms_path):
                    try:
                        with open(default_terms_path, 'r', encoding='utf-8', errors='ignore') as f:
                            combined_raw = f.read()
                        terms = _parse_terms(combined_raw)
                        logger.info('No terms supplied; using fallback file %s', default_terms_path)
                    except Exception as e:
                        logger.warning('Could not read fallback terms file %s: %s', default_terms_path, e)
            if not terms:
                logger.warning('Redaction not started: no terms provided')
                self._json_response({'error': 'Enter at least one term to redact, or choose a terms file.'}, 400)
                return
            logger.info('Redaction terms in use: %d', len(terms))
            if not input_dir or not os.path.isdir(input_dir):
                logger.warning('Redaction not started: invalid input directory %r', input_dir)
                self._json_response({'error': "The input directory doesn't exist. Check the path, or click Browse to choose it."}, 400)
                return
            if not output_dir.strip():
                logger.warning('Redaction not started: no output directory')
                self._json_response({'error': 'Choose an output directory.'}, 400)
                return
            in_resolved = Path(input_dir).resolve()
            out_resolved = Path(output_dir).expanduser().resolve()
            if out_resolved == in_resolved:
                logger.warning('Redaction not started: output directory is the input directory')
                self._json_response({'error': "Choose an output directory that's different from the input directory."}, 400)
                return
            if not os.path.exists(output_dir):
                try:
                    os.makedirs(output_dir, exist_ok=True)
                    logger.info('Created output directory %s', output_dir)
                except Exception as e:
                    logger.error('Redaction not started: cannot create output directory %s: %s', output_dir, e)
                    self._json_response({'error': "The output directory couldn't be created. Check the path and that you're allowed to save files there."}, 400)
                    return
            cancel = _start_job()

            # Collect files, skipping the output directory if it lives inside the input directory
            files = []
            for root, dirs, filenames in os.walk(input_dir):
                root_path = Path(root).resolve()
                if root_path == out_resolved or out_resolved in root_path.parents:
                    dirs[:] = []
                    continue
                files.extend(root_path / fn for fn in sorted(filenames) if Path(fn).suffix.lower() in SUPPORTED_EXTS)
            with _job_lock:
                job_state['total'] = len(files)
            logger.info('Found %d supported file(s) in %s', len(files), input_dir)
            output_root = Path(output_dir).expanduser()

            def output_for(fp):
                # Mirror the input's subfolders so same-named files never collide
                rel = fp.relative_to(in_resolved)
                return output_root / rel.parent / (fp.stem + '_redacted' + fp.suffix)

            threading.Thread(target=_run_job, args=(cancel.job_id, cancel, files, output_for, terms),
                             name='redaction-job', daemon=True).start()
            self._json_response({'status': 'started', 'files': len(files)})
            return

        if path == '/cancel':
            logger.info('Cancel clicked')
            _cancel_job()
            self._json_response({'status': 'cancelled'})
            return

        if path == '/reveal':
            self._reveal(payload.get('path'))
            return

        logger.warning('Unknown POST %s', path)
        self.send_response(404)
        self.end_headers()

    def _send_static(self, folder, name):
        """Serve a file from css/ or js/ (nothing else in the app folder is served)."""
        file_path = os.path.join(APP_DIR, folder, name)
        try:
            with open(file_path, 'rb') as f:
                content = f.read()
        except OSError:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header('Content-Type', _STATIC_TYPES[folder])
        # Revalidate every load so an updated file is picked up immediately
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(content)

    def _reveal(self, target):
        """Show a file (selected) or folder in Finder, or the platform's file manager."""
        target = str(target or '')
        if not os.path.isabs(target) or not os.path.exists(target):
            logger.warning('Show in Finder: not found: %r', target)
            self._json_response({'error': "That file or folder no longer exists."}, 404)
            return
        if sys.platform == 'darwin':
            cmd = ['open', target] if os.path.isdir(target) else ['open', '-R', target]
        elif sys.platform == 'win32':
            cmd = ['explorer', target] if os.path.isdir(target) else ['explorer', '/select,', target]
        else:
            cmd = ['xdg-open', target if os.path.isdir(target) else os.path.dirname(target)]
        try:
            subprocess.Popen(cmd)
        except OSError as e:
            logger.error('Show in Finder failed for %s: %s', target, e)
            self._json_response({'error': "Couldn't open Finder."}, 500)
            return
        logger.info('Show in Finder: %s', target)
        self._json_response({'status': 'shown'})

    def _log_ui_event(self, payload):
        """Record what the user did in the browser (typed into a field, page load)."""
        event = payload.get('event')
        value = payload.get('value')
        if event == 'page_loaded' and isinstance(value, dict):
            logger.info('UI loaded with saved settings: %s',
                        ', '.join(f'{_UI_FIELD_NAMES.get(k, k)}={_describe_ui_value(k, v)}' for k, v in value.items()))
        elif event == 'browse_chosen' and payload.get('field') in _UI_FIELD_NAMES:
            logger.info('%s set to %r (via Browse)', _UI_FIELD_NAMES[payload['field']].capitalize(), value)
        elif event == 'browse_cancelled' and payload.get('field') in _UI_FIELD_NAMES:
            logger.info('Browse window for %s closed; field unchanged', _UI_FIELD_NAMES[payload['field']])
        elif event == 'field_changed' and payload.get('field') in _UI_FIELD_NAMES:
            field = payload['field']
            count = payload.get('count')
            if field == 'terms' and isinstance(count, int):
                desc = f'{count} term(s)'
            else:
                desc = _describe_ui_value(field, value)
            logger.info('%s set to %s (typed)', _UI_FIELD_NAMES[field].capitalize(), desc)
        else:
            # Do not echo the payload: it may carry redaction terms
            logger.warning('Unknown UI event: event=%r field=%r', event, payload.get('field'))
            self._json_response({'error': 'unknown event'}, 400)
            return
        self._json_response({'status': 'logged'})

BROWSE_MAX_ENTRIES = 5000  # per folder, so a huge folder can't stall the page

def _list_dir(path, start='', want_file=False, field=''):
    """List a folder for the in-page file browser. Returns (payload, status).

    The browser lives in the page instead of a native macOS dialog: a dialog
    opened by the background server process appears behind the browser window
    on recent macOS, where the user can't even switch to it.
    """
    opening = not path
    if opening:
        # Opening: start where the field points, or the home folder (it may be mistyped)
        path = _browse_start_dir(start, want_file)
        logger.info('Browse window opened for %s at %s', _UI_FIELD_NAMES.get(field, field or 'a path'), path)
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(path):
        return {'error': "That folder doesn't exist any more."}, 404
    dirs, files = [], []
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.name.startswith('.'):
                    continue  # hidden files and folders
                try:
                    if entry.is_dir():
                        dirs.append(entry.name)
                    elif want_file and entry.is_file():
                        files.append(entry.name)
                except OSError:
                    continue
    except PermissionError:
        logger.warning('Browse: no permission to list %s', path)
        return {'error': "DocuCloak doesn't have permission to open this folder."}, 403
    except OSError as e:
        logger.warning('Browse: cannot list %s: %s', path, e)
        return {'error': "This folder couldn't be opened."}, 500
    dirs.sort(key=str.casefold)
    files.sort(key=str.casefold)
    truncated = len(dirs) + len(files) > BROWSE_MAX_ENTRIES
    files = files[:max(0, BROWSE_MAX_ENTRIES - len(dirs))]
    dirs = dirs[:BROWSE_MAX_ENTRIES]
    crumbs, current = [], Path(path)
    for part in [current, *current.parents]:
        crumbs.insert(0, {'name': part.name or part.anchor or os.sep, 'path': str(part)})
    home = Path.home()
    places = [{'name': 'Home', 'path': str(home)}] + [
        {'name': name, 'path': str(home / name)} for name in ('Desktop', 'Documents', 'Downloads')
        if (home / name).is_dir()]
    return {
        'path': path,
        'parent': str(current.parent) if current.parent != current else None,
        'crumbs': crumbs,
        'dirs': dirs,
        'files': files,
        'truncated': truncated,
        'places': places,
    }, 200

# Static assets: only /css/<name>.css and /js/<name>.js, no subfolders or other files,
# so the rest of the app folder (app.py, server.log, ...) is never served
_STATIC_PATH = re.compile(r'^/(css|js)/([A-Za-z0-9_-]+\.(?:css|js))$')
_STATIC_TYPES = {'css': 'text/css; charset=utf-8', 'js': 'text/javascript; charset=utf-8'}

def _asset_version():
    """Newest modification time of css/ and js/, added to their URLs in index.html so
    browsers (Safari included) fetch updated files instead of cached ones."""
    newest = 0
    for folder in ('css', 'js'):
        try:
            with os.scandir(os.path.join(APP_DIR, folder)) as entries:
                newest = max([newest] + [int(e.stat().st_mtime) for e in entries if e.is_file()])
        except OSError:
            pass
    return str(newest)

def _timing(state):
    """Elapsed seconds and estimated seconds left for /status (call with _job_lock held)."""
    cancel = state['cancel']
    progress = getattr(cancel, 'progress', None)
    if state['running'] and progress is not None:
        remaining = progress.remaining() if state['phase'] == 'redacting' else None
        return {'elapsed': round(progress.elapsed(), 1),
                'remaining': None if remaining is None else round(remaining, 1)}
    return {'elapsed': state['elapsed'], 'remaining': None}

def _browse_start_dir(current, want_file=False):
    """Folder the file browser opens in: the field's current location if it
    exists (for a file, its parent folder), otherwise the user's home folder,
    since a typed path may be mistyped."""
    home = os.path.expanduser('~')
    current = str(current or '').strip()
    if current:
        candidate = os.path.expanduser(current)
        if want_file and os.path.isfile(candidate):
            candidate = os.path.dirname(candidate)
        if os.path.isdir(candidate):
            return os.path.abspath(candidate)
        logger.debug('Browse start location %r does not exist; using home folder', current)
    return home

def _describe_ui_value(field, value):
    """Paths are logged verbatim; the terms text field only as a count."""
    if field == 'terms':
        return f'{len(_parse_terms(str(value or "")))} term(s)'
    return repr(value)

_UI_FIELD_NAMES = {
    'inputDir': 'input directory',
    'outputDir': 'output directory',
    'termsFile': 'redact terms file',
    'terms': 'redaction terms text field',
}

# ─── Main ───────────────────────────────────────────────────────────────────────

def main():
    port = int(os.environ.get('PORT', 5903))
    # Allow overriding port via app.conf
    cfg_port = _APP_CONFIG.get('PORT')
    if cfg_port:
        try:
            port = int(cfg_port)
        except ValueError:
            pass
    # Loopback only: the app reads and writes local files, so other devices on
    # the network must not be able to reach it
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    logger.info('%s running at http://localhost:%d (log file: %s)', APP_NAME, port, LOG_FILE)
    _tesseract_status()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info('Shutting down...')
        server.shutdown()

if __name__ == '__main__':
    main()
