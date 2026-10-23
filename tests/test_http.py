"""Handler: every HTTP route, exercised against an in-process server."""

import json
import os
import subprocess
import time
from types import SimpleNamespace

import pytest

import app
from conftest import REPO_DIR, SAMPLE_LINES, make_pdf, pdf_text, wait_for_job


def redact_payload(tmp_path, **overrides):
    payload = {
        'input_dir': str(tmp_path / 'in'),
        'output_dir': str(tmp_path / 'out'),
        'terms': '',
        'terms_file_content': '',
    }
    payload.update(overrides)
    return payload


# ─── GET ───────────────────────────────────────────────────────────────────────

def test_index_served_with_home_dir_substituted(server):
    for path in ('/', '/index.html'):
        status, headers, body = server.get(path)
        assert status == 200
        assert headers['Content-Type'] == 'text/html'
        html = body.decode()
        assert '{{HOME_DIR}}' not in html
        assert f'value="{os.path.expanduser("~")}"' in html
        assert 'Start Redaction' in html


def test_logo_served_as_svg(server):
    status, headers, body = server.get('/logo.svg')
    assert status == 200
    assert headers['Content-Type'] == 'image/svg+xml'
    assert headers['Cache-Control'] == 'no-cache', 'an updated logo must not be served stale from cache'
    assert body == (REPO_DIR / 'logo.svg').read_bytes()


def test_logo_url_changes_when_logo_is_replaced(server, isolated_app):
    import re
    logo = isolated_app / 'logo.svg'
    os.utime(logo, (1_700_000_000, 1_700_000_000))
    _, headers, body = server.get('/')
    assert headers['Cache-Control'] == 'no-cache'
    assert 'src="/logo.svg?v=1700000000"' in body.decode()
    os.utime(logo, (1_700_000_500, 1_700_000_500))  # user saves a new logo
    _, _, body = server.get('/')
    assert 'src="/logo.svg?v=1700000500"' in body.decode()
    assert '{{LOGO_VERSION}}' not in body.decode()
    status, _, served = server.get('/logo.svg?v=1700000500')
    assert status == 200 and served == logo.read_bytes()


def test_status_idle(server):
    status, _, body = server.get('/status')
    assert status == 200
    assert json.loads(body) == {'running': False, 'phase': 'idle', 'done': 0, 'total': 0, 'pages_done': 0,
                                'pages_total': 0, 'survey': None, 'cancelled': False, 'elapsed': None,
                                'remaining': None, 'current': [], 'results': [], 'errors': []}


def test_unknown_get_returns_404(server):
    # do_GET calls send_response(404) without end_headers(), so the client gets
    # an empty/aborted response instead of a 404 (Chrome logs
    # ERR_EMPTY_RESPONSE for /favicon.ico on every page load).
    status, _, _ = server.get('/favicon.ico')
    assert status == 404


# ─── POST /redact: validation ──────────────────────────────────────────────────

NO_TERMS = 'Enter at least one term to redact, or choose a terms file.'


def test_redact_rejects_missing_terms(server, tmp_path, sample_pdf):
    status, body = server.post_json('/redact', redact_payload(tmp_path))
    assert (status, body) == (400, {'error': NO_TERMS})


def test_redact_rejects_whitespace_only_terms(server, tmp_path, sample_pdf):
    status, body = server.post_json('/redact', redact_payload(tmp_path, terms=' , \n', terms_file_content='\n\n'))
    assert status == 400


def test_redact_rejects_malformed_json(server):
    status, body = server.post_json('/redact', raw=b'{not json')
    assert (status, body) == (400, {'error': NO_TERMS})


@pytest.mark.parametrize('input_dir', ['', '/definitely/not/here'])
def test_redact_rejects_invalid_input_dir(server, tmp_path, input_dir):
    status, body = server.post_json('/redact', redact_payload(tmp_path, terms='x', input_dir=input_dir))
    assert (status, body) == (400, {'error': "The input directory doesn't exist. Check the path, or click Browse to choose it."})


def test_redact_reports_uncreatable_output_dir(server, tmp_path, sample_pdf):
    blocker = tmp_path / 'file'
    blocker.write_text('')
    status, body = server.post_json('/redact', redact_payload(tmp_path, terms='x', output_dir=str(blocker / 'sub')))
    assert status == 400
    assert body['error'] == "The output directory couldn't be created. Check the path and that you're allowed to save files there."


def test_validation_failure_does_not_start_job(server, tmp_path):
    server.post_json('/redact', redact_payload(tmp_path, terms='x', input_dir='/nope'))
    assert app.job_state['cancel'] is None and app.job_state['running'] is False


# ─── POST /redact: the job ─────────────────────────────────────────────────────

def test_redact_end_to_end(server, tmp_path, sample_pdf):
    body, state = server.redact_and_wait(redact_payload(tmp_path, terms='Alice Smith'))
    assert body == {'status': 'started', 'files': 1}
    out = tmp_path / 'out' / 'sample_redacted.pdf'
    assert 'Alice' not in pdf_text(out)
    assert state['done'] == state['total'] == 1
    [result] = state['results']
    assert result == {'path': str(out), 'input': str(sample_pdf.resolve()),
                      'result': {'matches': 1, 'pages': 1}, 'error': None}

    _, _, status_body = server.get('/status')
    assert b'"running": false' in status_body and b'"done": 1' in status_body


def test_files_without_matches_are_skipped_and_left_untouched(server, tmp_path):
    hit = make_pdf(tmp_path / 'in' / 'hit.pdf', [['Alice Smith']])
    miss = make_pdf(tmp_path / 'in' / 'miss.pdf', [['nothing sensitive here']])
    before = (miss.read_bytes(), miss.stat().st_mtime_ns)
    _, state = server.redact_and_wait(redact_payload(tmp_path, terms='Alice Smith'))
    assert os.listdir(tmp_path / 'out') == ['hit_redacted.pdf']
    assert (miss.read_bytes(), miss.stat().st_mtime_ns) == before
    by_name = {os.path.basename(r['input']): r for r in state['results']}
    assert by_name['miss.pdf']['path'] is None
    assert by_name['miss.pdf']['result']['skipped'] is True
    assert by_name['hit.pdf']['path'] == str(tmp_path / 'out' / 'hit_redacted.pdf')


def test_rerun_after_removing_term_removes_stale_output(server, tmp_path, sample_pdf):
    out = tmp_path / 'out' / 'sample_redacted.pdf'
    server.redact_and_wait(redact_payload(tmp_path, terms='Alice Smith'))
    assert out.exists()
    server.redact_and_wait(redact_payload(tmp_path, terms='not in the file'))
    assert not out.exists()


def test_redact_creates_output_dir(server, tmp_path, sample_pdf):
    out = tmp_path / 'deep' / 'new' / 'out'
    server.redact_and_wait(redact_payload(tmp_path, terms='John Doe', output_dir=str(out)))
    assert (out / 'sample_redacted.pdf').exists()


def test_redact_combines_textarea_and_file_terms(server, tmp_path, sample_pdf):
    server.redact_and_wait(redact_payload(tmp_path, terms='Alice Smith', terms_file_content='John Doe\r\nsecret'))
    text = pdf_text(tmp_path / 'out' / 'sample_redacted.pdf')
    assert 'Alice' not in text and 'John' not in text and 'secret' not in text
    assert 'Phone' in text


def test_redact_with_only_file_terms(server, tmp_path, sample_pdf):
    _, state = server.redact_and_wait(redact_payload(tmp_path, terms_file_content='Alice Smith, John Doe'))
    assert state['results'][0]['result']['matches'] == 2


def test_redact_scans_recursively_and_filters_extensions(server, tmp_path, monkeypatch):
    monkeypatch.setattr(app, '_redact_image', lambda i, t, o, c: ({'matches': 0}, None))
    inp = tmp_path / 'in'
    make_pdf(inp / 'top.pdf', [SAMPLE_LINES])
    make_pdf(inp / 'a' / 'b' / 'nested.pdf', [SAMPLE_LINES])
    for name in ('x.jpg', 'x2.JPEG', 'y.png', 'z.tif', 'z2.tiff', 'w.bmp'):
        (inp / 'imgs').mkdir(exist_ok=True)
        (inp / 'imgs' / name).write_bytes(b'')
    (inp / 'notes.txt').write_text('Alice Smith')
    (inp / 'doc.docx').write_bytes(b'')
    body, state = server.redact_and_wait(redact_payload(tmp_path, terms='Alice Smith'))
    assert body['files'] == 8
    assert sorted(os.path.basename(r['path']) for r in state['results']) == sorted([
        'top_redacted.pdf', 'nested_redacted.pdf', 'x_redacted.jpg', 'x2_redacted.JPEG',
        'y_redacted.png', 'z_redacted.tif', 'z2_redacted.tiff', 'w_redacted.bmp'])


def test_redact_empty_input_dir(server, tmp_path):
    (tmp_path / 'in').mkdir()
    body, state = server.redact_and_wait(redact_payload(tmp_path, terms='x'))
    assert body == {'status': 'started', 'files': 0}
    assert state['results'] == []


def test_output_dir_inside_input_dir_is_not_rescanned(server, tmp_path, sample_pdf):
    out = tmp_path / 'in' / 'redacted'
    payload = redact_payload(tmp_path, terms='John Doe', output_dir=str(out))
    server.redact_and_wait(payload)
    body, _ = server.redact_and_wait(payload)
    assert body['files'] == 1
    assert os.listdir(out) == ['sample_redacted.pdf']


@pytest.mark.parametrize('output_dir', ['', '   '])
def test_blank_output_dir_is_rejected(server, tmp_path, sample_pdf, output_dir):
    # Writing next to the originals would rescan the _redacted copies on the next run
    status, body = server.post_json('/redact', redact_payload(tmp_path, terms='John Doe', output_dir=output_dir))
    assert (status, body) == (400, {'error': 'Choose an output directory.'})
    assert sorted(os.listdir(sample_pdf.parent)) == ['sample.pdf']


@pytest.mark.parametrize('same', ['{in}', '{in}/', '{in}/./'])
def test_output_dir_same_as_input_is_rejected(server, tmp_path, sample_pdf, same):
    out = same.format(**{'in': tmp_path / 'in'})
    status, body = server.post_json('/redact', redact_payload(tmp_path, terms='John Doe', output_dir=out))
    assert (status, body) == (400, {'error': "Choose an output directory that's different from the input directory."})


def test_same_filename_in_different_subdirs_produces_two_outputs(server, tmp_path):
    # Output is flat (<stem>_redacted<ext>), so in/a/report.pdf and
    # in/b/report.pdf race on the same output path and one is silently lost.
    make_pdf(tmp_path / 'in' / 'a' / 'report.pdf', [['Alice Smith']])
    make_pdf(tmp_path / 'in' / 'b' / 'report.pdf', [['John Doe']])
    _, state = server.redact_and_wait(redact_payload(tmp_path, terms='Alice Smith, John Doe'))
    paths = {r['path'] for r in state['results']}
    assert len(paths) == 2
    assert len(os.listdir(tmp_path / 'out')) == 2


def test_per_file_exception_is_recorded_and_job_continues(server, tmp_path, sample_pdf):
    bad = tmp_path / 'in' / 'broken.pdf'
    bad.write_bytes(b'garbage')
    _, state = server.redact_and_wait(redact_payload(tmp_path, terms='John Doe'))
    assert state['done'] == 2
    by_name = {os.path.basename(r['input']): r for r in state['results']}
    assert by_name['broken.pdf']['error']
    assert by_name['sample.pdf']['error'] is None
    assert 'John' not in pdf_text(tmp_path / 'out' / 'sample_redacted.pdf')


def test_scanned_pdf_without_tesseract_is_reported_as_error(server, tmp_path, monkeypatch):
    import pytesseract
    from test_redact_pdf import scanned_pdf

    def missing(*a, **k):
        raise pytesseract.TesseractNotFoundError()
    monkeypatch.setattr(pytesseract, 'image_to_data', missing)
    scanned_pdf(tmp_path / 'in' / 'scan.pdf', ['Alice Smith'])
    _, state = server.redact_and_wait(redact_payload(tmp_path, terms='Alice Smith'))
    [r] = state['results']
    assert r['error'] and 'tesseract' in r['error'].lower()
    assert not (tmp_path / 'out' / 'scan_redacted.pdf').exists()


def test_new_redact_request_cancels_running_job(server, tmp_path, sample_pdf, monkeypatch):
    real = app._redact_file
    gate = []

    def slow(fp, out, terms, cancel):
        if not gate:
            gate.append(cancel)
            cancel.wait(5)
        return real(fp, out, terms, cancel)
    monkeypatch.setattr(app, '_redact_file', slow)
    server.post_json('/redact', redact_payload(tmp_path, terms='Alice Smith'))
    while not gate:
        time.sleep(0.01)
    server.post_json('/redact', redact_payload(tmp_path, terms='John Doe'))
    assert gate[0].is_set(), 'first job should be cancelled'
    state = wait_for_job()
    text = pdf_text(tmp_path / 'out' / 'sample_redacted.pdf')
    assert 'John' not in text and 'Alice Smith' in text
    assert state['done'] == 1


# ─── The reported bug: re-running after editing the terms file ────────────────

def test_rerun_with_edited_terms_file_content_uses_new_terms(server, tmp_path, sample_pdf):
    """Steps 1-7 at the HTTP layer: when the browser *does* send the edited file
    content, the server redacts with the new terms."""
    out = tmp_path / 'out' / 'sample_redacted.pdf'
    server.redact_and_wait(redact_payload(tmp_path, terms_file_content='Alice Smith\nJohn Doe'))
    text = pdf_text(out)
    assert 'Alice' not in text and 'John' not in text

    server.redact_and_wait(redact_payload(tmp_path, terms_file_content='Phone'))
    text = pdf_text(out)
    assert 'Phone' not in text
    assert 'Alice Smith' in text and 'John Doe' in text


def test_empty_terms_fall_back_to_app_dir_terms_file(server, tmp_path, sample_pdf, isolated_app):
    (isolated_app / 'redact_terms.txt').write_text('John Doe\n')
    _, state = server.redact_and_wait(redact_payload(tmp_path))
    assert state['results'][0]['result']['matches'] == 1
    assert 'John' not in pdf_text(tmp_path / 'out' / 'sample_redacted.pdf')


def test_supplied_terms_take_precedence_over_app_dir_terms_file(server, tmp_path, sample_pdf, isolated_app):
    (isolated_app / 'redact_terms.txt').write_text('John Doe\n')
    server.redact_and_wait(redact_payload(tmp_path, terms_file_content='Alice Smith'))
    text = pdf_text(tmp_path / 'out' / 'sample_redacted.pdf')
    assert 'Alice' not in text and 'John Doe' in text


# ─── POST /redact: terms_file_path (server reads the file from disk) ──────────

def test_terms_file_is_reread_from_disk_on_every_request(server, tmp_path, sample_pdf):
    terms = tmp_path / 'terms.txt'
    out = tmp_path / 'out' / 'sample_redacted.pdf'
    payload = redact_payload(tmp_path, terms_file_path=str(terms))

    terms.write_text('Alice Smith\n')
    server.redact_and_wait(payload)
    text = pdf_text(out)
    assert 'Alice' not in text and 'John Doe' in text

    terms.write_text('Alice Smith\nJohn Doe\n')  # user adds a term
    server.redact_and_wait(payload)
    text = pdf_text(out)
    assert 'Alice' not in text and 'John' not in text

    terms.write_text('Phone\n')  # user replaces the terms
    server.redact_and_wait(payload)
    text = pdf_text(out)
    assert 'Phone' not in text and 'Alice Smith' in text and 'John Doe' in text


def test_terms_file_combined_with_textarea_terms(server, tmp_path, sample_pdf):
    terms = tmp_path / 'terms.txt'
    terms.write_text('John Doe')
    server.redact_and_wait(redact_payload(tmp_path, terms='Alice Smith', terms_file_path=str(terms)))
    text = pdf_text(tmp_path / 'out' / 'sample_redacted.pdf')
    assert 'Alice' not in text and 'John' not in text


def test_terms_file_path_expands_tilde(server, tmp_path, sample_pdf, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    (tmp_path / 'terms.txt').write_text('John Doe')
    server.redact_and_wait(redact_payload(tmp_path, terms_file_path='~/terms.txt'))
    assert 'John' not in pdf_text(tmp_path / 'out' / 'sample_redacted.pdf')


def test_terms_file_with_bom_and_crlf(server, tmp_path, sample_pdf):
    terms = tmp_path / 'terms.txt'
    terms.write_bytes('\ufeffAlice Smith\r\nJohn Doe\r\n'.encode('utf-8'))
    _, state = server.redact_and_wait(redact_payload(tmp_path, terms_file_path=str(terms)))
    assert state['results'][0]['result']['matches'] == 2


@pytest.mark.parametrize('name', ['missing.txt', ''])
def test_unreadable_terms_file_is_rejected(server, tmp_path, sample_pdf, name):
    bad = tmp_path / name if name else tmp_path  # a missing file, or a directory
    status, body = server.post_json('/redact', redact_payload(tmp_path, terms='x', terms_file_path=str(bad)))
    assert status == 400 and body['error'].startswith("The terms file couldn't be read.")
    assert app.job_state['cancel'] is None


def test_blank_terms_file_path_is_ignored(server, tmp_path, sample_pdf):
    _, state = server.redact_and_wait(redact_payload(tmp_path, terms='John Doe', terms_file_path='   '))
    assert state['results'][0]['result']['matches'] == 1


def test_empty_terms_file_falls_back_to_app_dir_terms_file(server, tmp_path, sample_pdf, isolated_app):
    (isolated_app / 'redact_terms.txt').write_text('John Doe\n')
    terms = tmp_path / 'terms.txt'
    terms.write_text('')
    server.redact_and_wait(redact_payload(tmp_path, terms_file_path=str(terms)))
    assert 'John' not in pdf_text(tmp_path / 'out' / 'sample_redacted.pdf')


# ─── POST /cancel, /browse, unknown ────────────────────────────────────────────

def test_cancel_with_no_job(server):
    assert server.post_json('/cancel') == (200, {'status': 'cancelled'})


def test_cancel_stops_running_job(server, tmp_path, monkeypatch):
    for i in range(6):
        make_pdf(tmp_path / 'in' / f'f{i}.pdf', [['x']])
    started = []

    def blocking(fp, out, terms, cancel):
        started.append(fp)
        cancel.wait(5)
        return {'input': str(fp), 'path': str(out), 'result': None, 'error': 'cancelled'}
    monkeypatch.setattr(app, '_redact_file', blocking)
    server.post_json('/redact', redact_payload(tmp_path, terms='x'))
    while not started:
        time.sleep(0.01)
    assert server.post_json('/cancel') == (200, {'status': 'cancelled'})
    state = wait_for_job(timeout=5)
    assert state['running'] is False
    assert state['done'] < 6


# ─── Browse start location ─────────────────────────────────────────────────────

@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    (home / 'Documents').mkdir(parents=True)
    monkeypatch.setenv('HOME', str(home))
    return home


@pytest.mark.parametrize('current, expected', [
    ('{home}/Documents', '{home}/Documents'),     # existing folder: open there
    ('{home}/Documents/', '{home}/Documents'),
    ('~/Documents', '{home}/Documents'),          # ~ is expanded
    ('  {home}/Documents  ', '{home}/Documents'), # surrounding spaces ignored
    ('{home}/Documnets', '{home}'),               # mistyped: home folder
    ('/no/such/dir', '{home}'),
    ('{home}/Documents/report.pdf', '{home}'),    # not a folder: home folder
    ('', '{home}'),
    (None, '{home}'),
])
def test_browse_start_dir_for_directories(fake_home, current, expected):
    current = current.format(home=fake_home) if current else current
    assert app._browse_start_dir(current) == expected.format(home=fake_home)


@pytest.mark.parametrize('current, expected', [
    ('{home}/Documents/terms.txt', '{home}/Documents'),  # existing file: its folder
    ('{home}/Documents', '{home}/Documents'),            # a folder is fine too
    ('{home}/Documents/missing.txt', '{home}'),          # missing file: home folder
    ('', '{home}'),
])
def test_browse_start_dir_for_terms_file(fake_home, current, expected):
    (fake_home / 'Documents' / 'terms.txt').write_text('x')
    current = current.format(home=fake_home)
    assert app._browse_start_dir(current, want_file=True) == expected.format(home=fake_home)


# ─── In-page file browser (/list_dir) ──────────────────────────────────────────

def list_dir(server, **params):
    from urllib.parse import urlencode
    status, _, body = server.get('/list_dir?' + urlencode(params))
    return status, json.loads(body)


@pytest.fixture
def tree(fake_home):
    docs = fake_home / 'Documents'
    for name in ('beta', 'Alpha', 'gamma', '.hidden_dir'):
        (docs / name).mkdir()
    for name in ('terms.txt', 'Zed.csv', 'a.md', '.DS_Store'):
        (docs / name).write_text('x')
    (fake_home / 'Desktop').mkdir()
    return docs


def test_list_folder_sorted_without_hidden_entries(server, tree):
    status, d = list_dir(server, path=str(tree))
    assert status == 200
    assert d['path'] == str(tree)
    assert d['dirs'] == ['Alpha', 'beta', 'gamma'], 'case-insensitive order, hidden folders left out'
    assert d['files'] == [], 'folder mode lists folders only'
    assert d['parent'] == str(tree.parent)
    assert [c['path'] for c in d['crumbs']][-2:] == [str(tree.parent), str(tree)]
    assert d['crumbs'][0]['path'] == '/'


def test_list_files_in_file_mode(server, tree):
    _, d = list_dir(server, path=str(tree), mode='file')
    assert d['files'] == ['a.md', 'terms.txt', 'Zed.csv']
    assert d['dirs'] == ['Alpha', 'beta', 'gamma']


def test_places_list_home_and_existing_standard_folders(server, tree, fake_home):
    _, d = list_dir(server, path=str(tree))
    assert d['places'] == [{'name': 'Home', 'path': str(fake_home)},
                           {'name': 'Desktop', 'path': str(fake_home / 'Desktop')},
                           {'name': 'Documents', 'path': str(tree)}]  # no Downloads folder here


@pytest.mark.parametrize('start, mode, expected', [
    ('{home}/Documents', 'dir', '{home}/Documents'),          # opens where the field points
    ('{home}/Documnets', 'dir', '{home}'),                    # mistyped: home folder
    ('', 'dir', '{home}'),
    ('{home}/Documents/terms.txt', 'file', '{home}/Documents'),  # a file: its folder
    ('~/Documents', 'dir', '{home}/Documents'),
])
def test_opening_starts_at_the_fields_location(server, tree, fake_home, start, mode, expected):
    _, d = list_dir(server, start=start.format(home=fake_home), mode=mode, field='inputDir')
    assert d['path'] == expected.format(home=fake_home)


def test_root_has_no_parent(server):
    _, d = list_dir(server, path='/')
    assert d['parent'] is None and d['crumbs'] == [{'name': '/', 'path': '/'}]


def test_missing_folder(server):
    assert list_dir(server, path='/no/such/folder') == (404, {'error': "That folder doesn't exist any more."})


def test_unreadable_folder(server, tmp_path):
    locked = tmp_path / 'locked'
    locked.mkdir()
    locked.chmod(0)
    try:
        status, d = list_dir(server, path=str(locked))
    finally:
        locked.chmod(0o755)
    assert (status, d) == (403, {'error': "DocuCloak doesn't have permission to open this folder."})


def test_huge_folder_is_capped(server, tmp_path, monkeypatch):
    monkeypatch.setattr(app, 'BROWSE_MAX_ENTRIES', 5)
    box = tmp_path / 'box'
    for i in range(4):
        (box / f'd{i}').mkdir(parents=True)
        (box / f'f{i}.txt').write_text('x')
    _, d = list_dir(server, path=str(box), mode='file')
    assert len(d['dirs']) + len(d['files']) == 5 and d['truncated'] is True
    assert d['dirs'] == ['d0', 'd1', 'd2', 'd3'], 'folders are kept first'


def test_names_with_quotes_and_unicode(server, tmp_path):
    (tmp_path / 'box' / 'it\'s "quoted" — résumé').mkdir(parents=True)
    _, d = list_dir(server, path=str(tmp_path / 'box'))
    assert d['dirs'] == ['it\'s "quoted" — résumé']


def test_old_native_dialog_endpoints_are_gone(server):
    assert server.post_json('/browse', {})[0] == 404
    assert server.post_json('/browse_file', {})[0] == 404


def test_unknown_post_returns_404(server):
    status, _ = server.post_json('/nope', {})
    assert status == 404


# ─── Show in Finder ────────────────────────────────────────────────────────────

@pytest.fixture
def popen_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, 'Popen', lambda cmd, **kw: calls.append(cmd))
    monkeypatch.setattr(app.sys, 'platform', 'darwin')
    return calls


def test_reveal_file_selects_it_in_finder(server, sample_pdf, popen_calls):
    assert server.post_json('/reveal', {'path': str(sample_pdf)}) == (200, {'status': 'shown'})
    assert popen_calls == [['open', '-R', str(sample_pdf)]]


def test_reveal_folder_opens_it(server, tmp_path, popen_calls):
    assert server.post_json('/reveal', {'path': str(tmp_path)}) == (200, {'status': 'shown'})
    assert popen_calls == [['open', str(tmp_path)]]


@pytest.mark.parametrize('path', ['/no/such/file.pdf', 'relative/path.pdf', '', None, '-R'])
def test_reveal_rejects_missing_or_relative_paths(server, popen_calls, path):
    status, body = server.post_json('/reveal', {'path': path})
    assert (status, body) == (404, {'error': 'That file or folder no longer exists.'})
    assert popen_calls == []


def test_reveal_on_other_platforms(server, sample_pdf, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, 'Popen', lambda cmd, **kw: calls.append(cmd))
    monkeypatch.setattr(app.sys, 'platform', 'win32')
    server.post_json('/reveal', {'path': str(sample_pdf)})
    monkeypatch.setattr(app.sys, 'platform', 'linux')
    server.post_json('/reveal', {'path': str(sample_pdf)})
    assert calls == [['explorer', '/select,', str(sample_pdf)], ['xdg-open', str(sample_pdf.parent)]]


# ─── Outdated server detection ─────────────────────────────────────────────────

def test_version_reports_current_code(server):
    assert server.get('/version')[2] == b'{"outdated": false}'


def test_version_reports_outdated_when_app_py_changed(server, monkeypatch):
    monkeypatch.setattr(app, '_code_fingerprint', lambda: 'a different hash')
    assert server.get('/version')[2] == b'{"outdated": true}'


def test_fingerprint_is_content_based(tmp_path, monkeypatch):
    fake = tmp_path / 'app.py'
    fake.write_text('one')
    monkeypatch.setattr(app, '__file__', str(fake))
    first = app._code_fingerprint()
    fake.write_text('two')
    assert app._code_fingerprint() != first


# ─── Static assets: css/ and js/ ───────────────────────────────────────────────

@pytest.mark.parametrize('path, content_type', [
    ('/css/styles.css', 'text/css; charset=utf-8'),
    ('/js/core.js', 'text/javascript; charset=utf-8'),
    ('/js/main.js', 'text/javascript; charset=utf-8'),
])
def test_static_files_served_with_type_and_no_cache(server, isolated_app, path, content_type):
    status, headers, body = server.get(path)
    assert status == 200
    assert headers['Content-Type'] == content_type
    assert headers['Cache-Control'] == 'no-cache'
    assert body == (isolated_app / path.lstrip('/')).read_bytes()


@pytest.mark.parametrize('path', [
    '/app.py', '/server.log', '/server.sh', '/app.conf', '/.server.pid',
    '/js/../app.py', '/css/..%2Fapp.py', '/js/%2e%2e/app.py',
    '/js/core.py', '/css/styles.js', '/js/sub/core.js', '/js/', '/css/missing.css',
])
def test_nothing_else_in_the_app_folder_is_served(server, isolated_app, path):
    (isolated_app / 'server.log').write_text('secret log')
    (isolated_app / 'app.py').write_text('source')
    status, _, body = server.get(path)
    assert status == 404 and b'secret' not in body and b'source' not in body


def test_index_links_versioned_css_and_js(server, isolated_app):
    import re
    html = server.get('/')[2].decode()
    assert '{{ASSET_VERSION}}' not in html
    version = re.search(r'/css/styles\.css\?v=(\d+)"', html).group(1)
    scripts = re.findall(r'<script src="/js/(\w+)\.js\?v=(\d+)"></script>', html)
    assert [name for name, _ in scripts] == ['core', 'tesseract', 'browser', 'redact', 'main'], 'load order matters'
    assert {v for _, v in scripts} == {version}
    os.utime(isolated_app / 'js' / 'redact.js', (2_000_000_000, 2_000_000_000))  # an update to one file
    assert '?v=2000000000' in server.get('/')[2].decode(), 'version changes so browsers refetch'


def test_index_has_no_inline_script_or_style():
    from conftest import REPO_DIR
    html = (REPO_DIR / 'index.html').read_text()
    assert '<style' not in html and 'style>' not in html and 'style="' not in html, 'styling lives in css/'
    assert '<script>' not in html, 'JavaScript lives in js/'
