"""server.sh: restarting automatically when backend files changed since the server started."""

import os
import shutil
import socket
import subprocess
import time

import pytest

from conftest import REPO_DIR


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@pytest.fixture
def copy(tmp_path):
    """A copy of the app on a free port, with `open` stubbed so no browser window opens."""
    app_dir = tmp_path / 'docucloak'
    app_dir.mkdir()
    for name in ('app.py', 'index.html', 'logo.svg', 'server.sh', 'app.conf'):
        shutil.copy2(REPO_DIR / name, app_dir / name)
    shutil.copytree(REPO_DIR / 'lib', app_dir / 'lib',
                    ignore=shutil.ignore_patterns('__pycache__'))
    for folder in ('css', 'js'):
        shutil.copytree(REPO_DIR / folder, app_dir / folder)
    conf = (app_dir / 'app.conf').read_text().replace('PORT=5903', f'PORT={free_port()}')
    (app_dir / 'app.conf').write_text(conf)
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    (bin_dir / 'open').write_text('#!/bin/sh\n')
    (bin_dir / 'open').chmod(0o755)
    env = {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}', 'PYTHONDONTWRITEBYTECODE': '1'}

    def sh(*args):
        out = subprocess.run(['./server.sh', *args], cwd=app_dir, env=env, capture_output=True, text=True, timeout=60)
        return out.stdout + out.stderr

    yield app_dir, sh
    sh('stop')


def pid(app_dir):
    return (app_dir / '.server.pid').read_text().strip()


def test_start_when_running_and_unchanged_does_not_restart(copy):
    app_dir, sh = copy
    assert 'started at' in sh('start')
    time.sleep(1)
    first = pid(app_dir)
    out = sh('start')
    assert 'running (PID' in out and pid(app_dir) == first


def test_start_restarts_when_app_module_changed_even_with_old_date(copy):
    """AirDrop and copies keep the old modification date, so the check uses content."""
    app_dir, sh = copy
    sh('start')
    time.sleep(1)
    first = pid(app_dir)
    module = app_dir / 'lib' / 'main.py'
    stat = module.stat()
    module.write_text(module.read_text() + '\n# updated\n')
    os.utime(module, (stat.st_atime, stat.st_mtime))  # keep the old date
    out = sh('start')
    assert 'was updated since it started; restarting it' in out
    time.sleep(1)
    assert pid(app_dir) != first


def test_start_restarts_when_pii_py_changed(copy):
    app_dir, sh = copy
    sh('start')
    time.sleep(1)
    pii_py = app_dir / 'lib' / 'pii.py'
    pii_py.write_text(pii_py.read_text() + '\n# updated\n')
    assert 'was updated since it started; restarting it' in sh('start')


def test_server_started_by_an_older_server_sh_is_restarted(copy):
    """A server started before this check existed has no recorded checksum."""
    app_dir, sh = copy
    sh('start')
    time.sleep(1)
    (app_dir / '.server.code').unlink()
    assert 'restarting it' in sh('start')


def test_stop_removes_runtime_files(copy):
    app_dir, sh = copy
    sh('start')
    time.sleep(1)
    sh('stop')
    assert not any((app_dir / f).exists() for f in ('.server.pid', '.server.port', '.server.code'))


# ─── Opening the browser only when the server is ready ─────────────────────────

def configured_port(app_dir):
    return int(next(line.split('=', 1)[1] for line in (app_dir / 'app.conf').read_text().splitlines()
                    if line.startswith('PORT=')))


def record_browser_opening(app_dir):
    """Replace the stubbed `open` with one that records whether the page answered at that moment."""
    log = app_dir.parent / 'browser.log'
    stub = app_dir.parent / 'bin' / 'open'
    stub.write_text(f'#!/bin/sh\nif curl --noproxy "*" -s -o /dev/null -m 2 "$1"; then echo ready >> {log}; '
                    f'else echo unreachable >> {log}; fi\n')
    return log


def test_browser_opens_only_after_a_slow_start_is_ready(copy):
    """Restarting showed "can't be reached" for a few seconds: the wait loop's port check was
    inverted, so it opened the browser before the server was listening."""
    app_dir, sh = copy
    app_py = app_dir / 'app.py'
    app_py.write_text('import time; time.sleep(2.5)  # a slow start, like a cold restart\n' + app_py.read_text())
    log = record_browser_opening(app_dir)
    out = sh('start')
    assert 'started at' in out
    assert log.read_text().split() == ['ready']


def test_start_refuses_a_port_held_by_another_program(copy):
    app_dir, sh = copy
    holder = socket.socket()
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(('127.0.0.1', configured_port(app_dir)))
    holder.listen()
    log = record_browser_opening(app_dir)
    try:
        out = sh('start')
    finally:
        holder.close()
    assert 'already in use by another program' in out
    assert not log.exists(), 'no browser for a server that never started'
    assert not any((app_dir / f).exists() for f in ('.server.pid', '.server.port', '.server.code'))


def test_start_reports_a_server_that_crashes(copy):
    app_dir, sh = copy
    app_py = app_dir / 'app.py'
    app_py.write_text('raise SystemExit("broken")\n' + app_py.read_text())
    log = record_browser_opening(app_dir)
    out = sh('start')
    assert 'failed to start' in out and 'server.log' in out
    assert not log.exists()
    assert not any((app_dir / f).exists() for f in ('.server.pid', '.server.port', '.server.code'))
