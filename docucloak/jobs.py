"""Shared job state and the cross-process cancellation flag."""

import itertools
import multiprocessing
import threading
import time

from .logging_setup import logger

_shared_job_id = None  # multiprocessing.Value: id of the job workers should be running

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
