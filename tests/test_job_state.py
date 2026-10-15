"""_start_job, _finish_job, _cancel_job, _update_job."""

import threading

import app


def test_start_job_resets_state_and_returns_fresh_event():
    app.job_state.update(done=5, total=9, results=['x'], errors=['y'])
    cancel = app._start_job()
    assert isinstance(cancel, threading.Event) and not cancel.is_set()
    assert app.job_state == {
        'running': True, 'cancel': cancel, 'phase': 'surveying', 'done': 0, 'total': 0,
        'pages_done': 0, 'pages_total': 0, 'survey': None, 'cancelled': False, 'elapsed': None,
        'results': [], 'errors': [],
    }


def test_each_job_gets_a_new_id_shared_with_workers():
    first = app._start_job()
    second = app._start_job()
    assert second.job_id > first.job_id
    assert app._shared_job_id.value == second.job_id
    assert app._JobCancel(first.job_id).is_set(), 'workers of the old job see it as cancelled'
    assert not app._JobCancel(second.job_id).is_set()
    app._cancel_job()
    assert app._JobCancel(second.job_id).is_set()


def test_start_job_cancels_previous_job():
    first = app._start_job()
    second = app._start_job()
    assert first.is_set()
    assert not second.is_set()
    assert app.job_state['cancel'] is second


def test_finish_job_clears_running_for_current_job():
    cancel = app._start_job()
    app._finish_job(cancel)
    assert app.job_state['running'] is False
    assert app.job_state['cancel'] is None


def test_finish_job_from_stale_job_does_not_clobber_new_job():
    old = app._start_job()
    new = app._start_job()
    app._finish_job(old)
    assert app.job_state['running'] is True
    assert app.job_state['cancel'] is new


def test_cancel_job_sets_event_and_is_safe_with_no_job():
    app._cancel_job()  # no job: must not raise
    cancel = app._start_job()
    app._cancel_job()
    assert cancel.is_set()


def test_update_job_is_thread_safe():
    app._start_job()
    threads = [threading.Thread(target=lambda: [app._update_job(1) for _ in range(500)]) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert app.job_state['done'] == 4000


def test_update_job_default_delta_is_zero():
    app._start_job()
    app._update_job()
    assert app.job_state['done'] == 0
