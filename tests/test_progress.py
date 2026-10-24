"""Elapsed time and the time-remaining estimate (_Progress)."""

import json
import time
from pathlib import Path

import pytest

from docucloak import redaction, scheduler
from conftest import make_pdf, wait_for_job


def progress_with(workers, done=(), running=(), pending=()):
    """done: (kind, estimated, actual); running: (id, kind, cost, file, seconds ago); pending: (id, kind, cost, file)."""
    p = scheduler._Progress(workers)
    for kind, est, act in done:
        totals = p.done.setdefault(kind, [0.0, 0.0])
        totals[0] += est
        totals[1] += act
        p.completed += 1
    now = time.monotonic()
    for task_id, kind, cost, file, ago in running:
        p.running[task_id] = (kind, cost, file, now - ago)
    for task_id, kind, cost, file in pending:
        p.pending[task_id] = (kind, cost, file)
    return p


CALIBRATED = [('text', 1.0, 1.0), ('ocr', 1.0, 1.0), ('finalize', 1.0, 1.0)]  # estimates exactly right


def test_no_estimate_until_two_tasks_finished():
    p = progress_with(4, pending=[(1, 'text', 1, 'a'), (2, 'text', 1, 'b'), (3, 'text', 1, 'c')])
    assert p.remaining() is None
    p.completed = 1
    assert p.remaining() is None
    p.completed = 2
    assert p.remaining() is not None


def test_nothing_left_is_zero():
    p = progress_with(4, done=CALIBRATED)
    assert p.remaining() == 0.0
    p.end()
    assert p.remaining() == 0.0


def test_parallel_workers_share_the_work():
    p = progress_with(2, done=CALIBRATED, pending=[(i, 'text', 1.0, f'f{i}') for i in range(4)])
    assert p.remaining() == pytest.approx(2.0)   # 4 one-second tasks on 2 workers
    p = progress_with(4, done=CALIBRATED, pending=[(i, 'text', 1.0, f'f{i}') for i in range(4)])
    assert p.remaining() == pytest.approx(1.0)


def test_idle_cores_at_the_end_are_accounted_for():
    # one 10s task and nine 1s tasks on 4 workers: the big task alone decides
    p = progress_with(4, done=CALIBRATED, pending=[(0, 'ocr', 10.0, 'big')] + [(i, 'text', 1.0, f'f{i}') for i in range(1, 10)])
    assert p.remaining() == pytest.approx(10.0)


def test_running_tasks_count_with_time_already_spent():
    p = progress_with(1, done=CALIBRATED, running=[(1, 'text', 4.0, 'a', 3.0)])
    assert p.remaining() == pytest.approx(1.0, abs=0.05)


def test_overdue_running_task_still_counts_a_little():
    p = progress_with(1, done=CALIBRATED, running=[(1, 'text', 1.0, 'a', 5.0)])
    assert 0 < p.remaining() <= 0.15


def test_calibration_learns_the_real_speed_per_kind():
    slow_ocr = [('text', 10.0, 10.0), ('ocr', 10.0, 20.0)]  # OCR takes twice the estimate on this machine
    p = progress_with(1, done=slow_ocr, pending=[(1, 'ocr', 1.0, 'a')])
    assert p.remaining() == pytest.approx(1.0 * (20 + 2 * (30 + 2) / (20 + 2)) / (10 + 2), rel=1e-6)
    assert p.remaining() > 1.7


def test_unseen_kind_uses_overall_speed():
    p = progress_with(1, done=[('text', 5.0, 7.5), ('text', 5.0, 7.5)], pending=[(1, 'image', 1.0, 'a')])
    assert p.remaining() == pytest.approx((15 + 2) / (10 + 2), rel=1e-6)


def test_final_save_waits_for_its_parts():
    p = progress_with(4, done=CALIBRATED, pending=[(1, 'text', 1.0, 'doc'), (2, 'text', 1.0, 'doc'),
                                                   (3, 'finalize', 0.5, 'doc')])
    assert p.remaining() == pytest.approx(1.5)


def test_final_save_starts_as_soon_as_ready_not_at_the_end():
    # B is a 3s task; A's part finishes at 1s, so A's 1s save runs from 1s to 2s, well before B ends
    p = progress_with(2, done=CALIBRATED, pending=[(1, 'text', 3.0, 'B'), (2, 'text', 1.0, 'A'),
                                                   (3, 'finalize', 1.0, 'A')])
    assert p.remaining() == pytest.approx(3.0)


def test_final_save_waits_for_a_running_part():
    p = progress_with(2, done=CALIBRATED, running=[(1, 'text', 2.0, 'doc', 0.0)], pending=[(2, 'finalize', 1.0, 'doc')])
    assert p.remaining() == pytest.approx(3.0, abs=0.05)


def test_lifecycle_start_finish_retry_and_drop():
    p = scheduler._Progress(2)
    p.add(1, 'text', 1.0, 'a')
    p.add(2, 'text', 1.0, 'b')
    p.start(1)
    assert 1 in p.running and 1 not in p.pending
    p.finish(1)
    assert p.completed == 1 and p.done['text'][0] == 1.0
    p.start(2)
    p.retry(2)                      # a worker crashed: back to pending with its estimate
    assert p.pending[2] == ('text', 1.0, 'b')
    p.drop_file('b')                # the file failed: its work no longer counts
    assert not p.pending and not p.running


def test_current_files_lists_running_files_once():
    p = scheduler._Progress(3)
    for task_id, file in [(1, Path('/in/a.pdf')), (2, Path('/in/a.pdf')), (3, Path('/in/b.png'))]:
        p.add(task_id, 'text', 1.0, file)
    assert p.current_files() == []
    p.start(1)
    p.start(2)
    p.start(3)
    assert p.current_files() == ['a.pdf', 'b.png'], 'two parts of a.pdf count as one file'
    p.finish(1)
    assert p.current_files() == ['a.pdf', 'b.png'], 'the other part of a.pdf is still running'
    p.finish(2)
    p.finish(3)
    assert p.current_files() == []


def test_elapsed_freezes_when_finished():
    p = scheduler._Progress(1)
    p.started -= 5
    p.end()
    first = p.elapsed()
    time.sleep(0.05)
    assert p.elapsed() == first and first >= 5


# ─── Through /status ───────────────────────────────────────────────────────────

def test_status_reports_elapsed_and_remaining_during_a_run(server, tmp_path, monkeypatch):
    for i in range(6):
        make_pdf(tmp_path / 'in' / f'f{i}.pdf', [['Alice Smith']])
    real = redaction._redact_file

    def slow(*a):
        time.sleep(0.4)
        return real(*a)
    monkeypatch.setattr(redaction, '_redact_file', slow)
    monkeypatch.setattr(scheduler, 'WORKERS', 2)
    scheduler._shutdown_pool()
    server.post_json('/redact', {'input_dir': str(tmp_path / 'in'), 'output_dir': str(tmp_path / 'out'), 'terms': 'Alice Smith'})
    seen = []
    while True:
        st = json.loads(server.get('/status')[2])
        if not st['running']:
            break
        seen.append((st['elapsed'], st['remaining']))
        time.sleep(0.1)
    scheduler._shutdown_pool()
    assert seen and all(e is not None for e, _ in seen)
    estimates = [r for _, r in seen if r is not None]
    assert estimates, 'an estimate appears once two files are done'
    assert max(estimates) < 2.0, '6 files x 0.4s on 2 workers: about 1.2s in total'
    assert st['elapsed'] >= 1.0 and st['remaining'] is None


def test_status_reports_the_files_in_progress(server, tmp_path, monkeypatch):
    names = ['alpha.pdf', 'beta.pdf', 'gamma.pdf']
    for name in names:
        make_pdf(tmp_path / 'in' / name, [['Alice Smith']])
    real = redaction._redact_file

    def slow(*a):
        time.sleep(0.5)
        return real(*a)
    monkeypatch.setattr(redaction, '_redact_file', slow)
    monkeypatch.setattr(scheduler, 'WORKERS', 3)
    scheduler._shutdown_pool()
    server.post_json('/redact', {'input_dir': str(tmp_path / 'in'), 'output_dir': str(tmp_path / 'out'),
                                 'terms': 'Alice Smith'})
    seen = set()
    while True:
        st = json.loads(server.get('/status')[2])
        if not st['running']:
            break
        seen.update(st['current'])
        time.sleep(0.05)
    scheduler._shutdown_pool()
    assert set(names) <= seen, 'each file showed up while it was being worked on'
    assert st['current'] == [], 'nothing is in progress once the job finishes'
