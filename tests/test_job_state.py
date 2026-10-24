"""_start_job, _finish_job, _cancel_job, _update_job."""

import threading

from lib import jobs


def test_start_job_resets_state_and_returns_fresh_event():
    jobs.job_state.update(done=5, total=9, results=['x'], errors=['y'])
    cancel = jobs._start_job()
    assert isinstance(cancel, threading.Event) and not cancel.is_set()
    assert jobs.job_state == {
        'running': True, 'cancel': cancel, 'phase': 'surveying', 'done': 0, 'total': 0,
        'pages_done': 0, 'pages_total': 0, 'survey': None, 'cancelled': False, 'elapsed': None,
        'results': [], 'errors': [],
    }


def test_each_job_gets_a_new_id_shared_with_workers():
    first = jobs._start_job()
    second = jobs._start_job()
    assert second.job_id > first.job_id
    assert jobs._shared_job_id.value == second.job_id
    assert jobs._JobCancel(first.job_id).is_set(), 'workers of the old job see it as cancelled'
    assert not jobs._JobCancel(second.job_id).is_set()
    jobs._cancel_job()
    assert jobs._JobCancel(second.job_id).is_set()


def test_start_job_cancels_previous_job():
    first = jobs._start_job()
    second = jobs._start_job()
    assert first.is_set()
    assert not second.is_set()
    assert jobs.job_state['cancel'] is second


def test_finish_job_clears_running_for_current_job():
    cancel = jobs._start_job()
    jobs._finish_job(cancel)
    assert jobs.job_state['running'] is False
    assert jobs.job_state['cancel'] is None


def test_finish_job_from_stale_job_does_not_clobber_new_job():
    old = jobs._start_job()
    new = jobs._start_job()
    jobs._finish_job(old)
    assert jobs.job_state['running'] is True
    assert jobs.job_state['cancel'] is new


def test_cancel_job_sets_event_and_is_safe_with_no_job():
    jobs._cancel_job()  # no job: must not raise
    cancel = jobs._start_job()
    jobs._cancel_job()
    assert cancel.is_set()


def test_update_job_is_thread_safe():
    jobs._start_job()
    threads = [threading.Thread(target=lambda: [jobs._update_job(1) for _ in range(500)]) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert jobs.job_state['done'] == 4000


def test_update_job_default_delta_is_zero():
    jobs._start_job()
    jobs._update_job()
    assert jobs.job_state['done'] == 0
