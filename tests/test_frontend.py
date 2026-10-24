"""index.html JavaScript, driven in the locally installed Google Chrome.

Covers pickDir, saveFields/restoreFields, readTermsFile, startRedact,
cancelRedact, pollStatus and pickFile, plus the reported edit-the-terms-file-and-rerun
bug end to end. Skipped when Playwright or Chrome is unavailable.
"""

import json
import os
import time

import pytest

from conftest import pdf_text

pytestmark = pytest.mark.browser
sync_api = pytest.importorskip('playwright.sync_api')


@pytest.fixture(scope='module')
def browser():
    try:
        pw = sync_api.sync_playwright().start()
    except Exception as e:  # pragma: no cover
        pytest.skip(f'playwright unavailable: {e}')
    try:
        b = pw.chromium.launch(channel='chrome', headless=True)
    except Exception as e:
        pw.stop()
        pytest.skip(f'Google Chrome not available: {e}')
    yield b
    b.close()
    pw.stop()


@pytest.fixture
def page(browser, server):
    ctx = browser.new_context()
    pg = ctx.new_page()
    pg.goto(server.base + '/')
    yield pg
    ctx.close()


def fill_dirs(page, tmp_path):
    page.fill('#inputDir', str(tmp_path / 'in'))
    page.fill('#outputDir', str(tmp_path / 'out'))


def disable_pii(page):
    """Turn off automatic detection for tests that exercise term-only behaviour."""
    for field in ('piiEmail', 'piiCreditCard', 'piiSsn', 'piiPhone'):
        page.uncheck('#' + field)


def start_and_wait(page):
    page.evaluate("() => { const el = document.getElementById('status'); el.textContent = ''; el.dataset.state = ''; }")
    page.click('text=Start Redaction')
    page.wait_for_function(
        "() => ['done', 'error'].includes(document.getElementById('status').dataset.state)",
        timeout=20000)
    return page.inner_text('#status')


def choose_terms_file(page, path):
    """Pick `path` with Choose File: the in-page browser opens in its folder, click it, Choose file."""
    if not path.exists():
        path.write_text('')
    page.fill('#termsFile', str(path))
    page.click('#chooseTermsFile')
    item = page.locator(f'#browser .browser-item.file[data-file="{path}"]')
    item.click()
    page.click('#browserChoose')
    page.wait_for_function("p => document.getElementById('termsFile').value === p", arg=str(path))
    assert not browser_open(page)


def browser_open(page):
    return page.evaluate("document.getElementById('browser').classList.contains('open')")


def open_browser_at(page, field_selector, button_selector, folder):
    page.fill(field_selector, str(folder))
    page.click(button_selector)
    page.wait_for_function("f => document.getElementById('browserSelection').textContent === f || "
                           "document.getElementById('browserSelection').textContent === 'Select a file'", arg=str(folder))
    page.wait_for_selector('#browser .browser-item')


def capture_redact_requests(page):
    bodies = []
    page.on('request', lambda r: bodies.append(json.loads(r.post_data)) if r.url.endswith('/redact') else None)
    return bodies


# ─── saveFields / restoreFields ────────────────────────────────────────────────

def test_logo_shown_next_to_app_name(page):
    page.wait_for_function("document.querySelector('.brand img').naturalWidth > 0")
    logo = page.locator('.brand img').bounding_box()
    title = page.locator('.brand h1').bounding_box()
    subtitle = page.locator('.brand p').bounding_box()
    assert logo['height'] >= 100, 'logo should be large'
    assert logo['x'] + logo['width'] <= title['x'], 'logo should sit left of the name'
    assert subtitle['y'] >= title['y'] + title['height'] - 1, 'subtitle on the row below the name'
    assert abs(subtitle['x'] - title['x']) < 1, 'name and subtitle left-aligned with each other'
    text_mid = (title['y'] + subtitle['y'] + subtitle['height']) / 2
    assert abs(text_mid - (logo['y'] + logo['height'] / 2)) < 8, 'text block vertically centred on the logo'
    card = page.locator('main.card').bounding_box()
    assert abs((title['x'] + title['width'] / 2) - (card['x'] + card['width'] / 2)) < 2, 'app name centred with the app'


def test_fields_default_to_home_dir(page):
    home = os.path.expanduser('~')
    assert page.input_value('#inputDir') == home
    assert page.input_value('#outputDir') == home


def test_directories_persist_across_reload_but_terms_do_not(page):
    page.fill('#inputDir', '/tmp/a')
    page.fill('#outputDir', '/tmp/b')
    page.fill('#terms', 'Alice Smith, SSN')
    assert page.evaluate("localStorage.getItem('dc_terms')") is None
    page.reload()
    assert page.input_value('#inputDir') == '/tmp/a'
    assert page.input_value('#outputDir') == '/tmp/b'
    assert page.input_value('#terms') == ''
    assert 'Alice' not in page.evaluate("JSON.stringify(localStorage)")


def test_terms_saved_by_older_versions_are_purged(page):
    page.evaluate("localStorage.setItem('dc_terms', 'Alice Smith')")
    page.reload()
    assert page.evaluate("localStorage.getItem('dc_terms')") is None
    assert page.input_value('#terms') == ''


def test_pii_checkboxes_default_to_recommended_types(page):
    assert page.is_checked('#piiEmail')
    assert page.is_checked('#piiCreditCard')
    assert page.is_checked('#piiSsn')
    assert not page.is_checked('#piiPhone')


def test_pii_checkbox_choices_persist_across_reload(page):
    page.uncheck('#piiSsn')
    page.check('#piiPhone')
    page.wait_for_function("localStorage.getItem('dc_piiSsn') === '0' && localStorage.getItem('dc_piiPhone') === '1'")
    page.reload()
    assert not page.is_checked('#piiSsn')
    assert page.is_checked('#piiPhone')


# ─── Layout ────────────────────────────────────────────────────────────────────

def test_input_and_output_directories_are_on_separate_full_width_rows(page):
    page.set_viewport_size({'width': 1200, 'height': 900})
    inp = page.locator('#inputDir').bounding_box()
    out = page.locator('#outputDir').bounding_box()
    card = page.locator('main.card').bounding_box()
    assert out['y'] >= inp['y'] + inp['height'], 'output directory must be below input directory'
    for box in (inp, out):
        assert box['width'] > card['width'] * 0.75


# ─── UI event logging ──────────────────────────────────────────────────────────

def capture_log_events(page):
    events = []
    page.on('request', lambda r: events.append(json.loads(r.post_data)) if r.url.endswith('/log') else None)
    return events


def test_page_load_reports_saved_settings(browser, server):
    ctx = browser.new_context()
    pg = ctx.new_page()
    events = capture_log_events(pg)
    pg.goto(server.base + '/')
    pg.wait_for_function("() => true")
    pg.wait_for_timeout(300)
    ctx.close()
    assert events[0]['event'] == 'page_loaded'
    assert set(events[0]['value']) == {'inputDir', 'outputDir', 'termsFile'}  # never the terms


def test_editing_fields_reports_each_change(page):
    events = capture_log_events(page)
    for field, value in [('#inputDir', '/in'), ('#outputDir', '/out'), ('#termsFile', '/t.txt'), ('#terms', 'Alice Smith, SSN\nSSN')]:
        page.fill(field, value)
        page.press(field, 'Tab')  # leaving the field fires 'change', as for a real user
    page.wait_for_function("() => true")
    page.wait_for_timeout(300)
    changes = [(e['field'], e.get('value', e.get('count'))) for e in events if e['event'] == 'field_changed']
    assert changes == [('inputDir', '/in'), ('outputDir', '/out'), ('termsFile', '/t.txt'), ('terms', 2)]
    assert 'Alice' not in json.dumps(events), 'terms text must never be sent to the log'



def list_dir_requests(page):
    from urllib.parse import parse_qs, urlparse
    seen = []
    page.on('request', lambda r: seen.append({k: v[0] for k, v in parse_qs(urlparse(r.url).query).items()})
            if '/list_dir' in r.url else None)
    return seen


def test_browse_opens_where_the_field_points(page):
    seen = list_dir_requests(page)
    page.fill('#inputDir', '  /Users/someone/In  ')
    page.click('#inputDir ~ button')
    page.wait_for_selector('#browser.open')
    page.wait_for_timeout(300)
    assert seen[0] == {'mode': 'dir', 'field': 'inputDir', 'start': '/Users/someone/In'}
    page.keyboard.press('Escape')
    page.fill('#termsFile', '/Users/someone/terms.txt')
    page.click('#chooseTermsFile')
    page.wait_for_timeout(300)
    assert seen[-1] == {'mode': 'file', 'field': 'termsFile', 'start': '/Users/someone/terms.txt'}


def test_start_redaction_does_not_send_spurious_cancel(page, tmp_path, sample_pdf):
    calls = []
    page.on('request', lambda r: calls.append(r.url.rsplit('/', 1)[-1]) if r.method == 'POST' else None)
    fill_dirs(page, tmp_path)
    page.fill('#terms', 'Alice Smith')
    start_and_wait(page)
    assert 'cancel' not in calls


def test_skipped_file_is_shown_as_skipped(page, tmp_path, sample_pdf):
    fill_dirs(page, tmp_path)
    disable_pii(page)
    page.fill('#terms', 'not in the document')
    start_and_wait(page)
    page.wait_for_function("document.querySelectorAll('.result-item').length === 1")
    item = page.locator('.result-item.skip')
    assert item.inner_text().split('\n') == ['sample.pdf', 'No matches, nothing to redact']
    assert not (tmp_path / 'out' / 'sample_redacted.pdf').exists()


# ─── In-page file browser ─────────────────────────────────────────────────────

@pytest.fixture
def folders(tmp_path):
    root = tmp_path / 'root'
    for sub in ('Clients/Acme', 'Clients/Beta', 'Statements', '.secret'):
        (root / sub).mkdir(parents=True)
    (root / 'Statements' / 'terms.txt').write_text('x')
    (root / 'Statements' / 'notes.md').write_text('x')
    return root


def test_browse_navigates_and_fills_the_matching_field(page, folders):
    open_browser_at(page, '#outputDir', '#outputDir ~ button', folders)
    assert page.inner_text('#browserTitle') == 'Choose the output directory'
    assert page.locator('#browser .browser-item.dir span').all_inner_texts() == ['..', 'Clients', 'Statements']
    page.click('#browser .browser-item.dir:has-text("Clients")')
    page.wait_for_function("f => document.getElementById('browserSelection').textContent === f", arg=str(folders / 'Clients'))
    page.click('#browser .browser-item.dir:has-text("Acme")')
    page.wait_for_function("f => document.getElementById('browserSelection').textContent === f", arg=str(folders / 'Clients' / 'Acme'))
    page.click('#browser .browser-crumbs button:has-text("Clients")')        # breadcrumb back up
    page.wait_for_function("f => document.getElementById('browserSelection').textContent === f", arg=str(folders / 'Clients'))
    page.click('#browserChoose')
    assert page.input_value('#outputDir') == str(folders / 'Clients')
    assert page.input_value('#inputDir') == os.path.expanduser('~'), 'only the matching field changes'
    assert not browser_open(page)
    assert page.evaluate("localStorage.getItem('dc_outputDir')") == str(folders / 'Clients')


def test_browse_parent_row_and_places(page, folders):
    open_browser_at(page, '#inputDir', '#inputDir ~ button', folders / 'Statements')
    page.click('#browser .browser-item.dir:has-text("..")')
    page.wait_for_function("f => document.getElementById('browserSelection').textContent === f", arg=str(folders))
    page.click('#browser .browser-places button:has-text("Home")')
    page.wait_for_function("h => document.getElementById('browserSelection').textContent === h", arg=os.path.expanduser('~'))


@pytest.mark.parametrize('how', ['cancel', 'escape', 'outside'])
def test_closing_the_browser_leaves_field_unchanged(page, folders, how):
    open_browser_at(page, '#inputDir', '#inputDir ~ button', folders)
    page.click('#browser .browser-item.dir:has-text("Clients")')
    page.wait_for_timeout(200)
    {'cancel': lambda: page.click('#browser >> text=Cancel'),
     'escape': lambda: page.keyboard.press('Escape'),
     'outside': lambda: page.mouse.click(5, 5)}[how]()
    assert not browser_open(page)
    assert page.input_value('#inputDir') == str(folders)


def test_enter_chooses_the_current_folder(page, folders):
    open_browser_at(page, '#inputDir', '#inputDir ~ button', folders / 'Clients')
    page.keyboard.press('Enter')
    assert page.input_value('#inputDir') == str(folders / 'Clients') and not browser_open(page)


def test_file_mode_needs_a_selection(page, folders):
    open_browser_at(page, '#termsFile', '#chooseTermsFile', folders / 'Statements')
    assert page.inner_text('#browserTitle') == 'Choose a terms file'
    assert page.inner_text('#browserChoose') == 'Choose file'
    assert page.is_disabled('#browserChoose') and page.inner_text('#browserSelection') == 'Select a file'
    assert page.locator('#browser .browser-item.file span').all_inner_texts() == ['notes.md', 'terms.txt']
    page.click('#browser .browser-item.file:has-text("terms.txt")')
    assert page.is_enabled('#browserChoose')
    assert page.inner_text('#browserSelection') == str(folders / 'Statements' / 'terms.txt')


def test_double_click_chooses_a_file(page, folders):
    open_browser_at(page, '#termsFile', '#chooseTermsFile', folders / 'Statements')
    page.dblclick('#browser .browser-item.file:has-text("notes.md")')
    assert page.input_value('#termsFile') == str(folders / 'Statements' / 'notes.md') and not browser_open(page)


def test_browse_shows_folder_errors(page, folders):
    page.route('**/list_dir?*path=*', lambda route: route.fulfill(
        status=403, json={'error': "DocuCloak doesn't have permission to open this folder."}))
    open_browser_at(page, '#inputDir', '#inputDir ~ button', folders)
    page.click('#browser .browser-item.dir:has-text("Clients")')
    page.wait_for_selector('#browserError:not([hidden])')
    assert page.inner_text('#browserError') == "DocuCloak doesn't have permission to open this folder."
    assert page.inner_text('#browserSelection') == str(folders), 'stays in the current folder'


def test_reopening_shows_no_stale_listing(page, folders):
    open_browser_at(page, '#termsFile', '#chooseTermsFile', folders / 'Statements')
    page.click('#browser .browser-item.file:has-text("terms.txt")')
    page.keyboard.press('Escape')
    page.route('**/list_dir*', lambda route: None)   # hold the next listing so we can look at the open state
    page.click('#inputDir ~ button')
    assert page.inner_text('#browserSelection') == '' or page.inner_text('#browserSelection') == 'Select a file'
    assert page.locator('#browser .browser-item').count() == 0


# ─── startRedact / pollStatus ─────────────────────────────────

def test_redact_with_textarea_terms(page, tmp_path, sample_pdf):
    fill_dirs(page, tmp_path)
    disable_pii(page)
    page.fill('#terms', 'Alice Smith')
    status = start_and_wait(page)
    assert status == 'Done in under 1 sec. 1 file redacted.'
    assert 'Alice' not in pdf_text(tmp_path / 'out' / 'sample_redacted.pdf')
    item = page.locator('.result-item')
    assert item.count() == 1
    assert 'ok' in item.get_attribute('class')
    assert item.inner_text().split('\n') == ['sample.pdf', 'Saved as sample_redacted.pdf']


def test_redact_pii_without_terms(page, tmp_path):
    from conftest import make_pdf
    make_pdf(tmp_path / 'in' / 'pii.pdf', [['Email alice@example.com']])
    fill_dirs(page, tmp_path)
    status = start_and_wait(page)
    assert 'Automatically found 1 email address.' in status
    assert 'alice@example.com' not in pdf_text(tmp_path / 'out' / 'pii_redacted.pdf')


def test_start_redaction_sends_enabled_pii_kinds(page, tmp_path, sample_pdf):
    fill_dirs(page, tmp_path)
    page.fill('#terms', 'Alice Smith')
    page.uncheck('#piiSsn')
    page.check('#piiPhone')
    bodies = capture_redact_requests(page)
    start_and_wait(page)
    assert bodies[-1]['pii_kinds'] == ['email', 'credit_card', 'phone']


def test_redact_with_terms_file(page, tmp_path, sample_pdf):
    terms = tmp_path / 'terms.txt'
    terms.write_text('John Doe\n')
    fill_dirs(page, tmp_path)
    choose_terms_file(page, terms)
    bodies = capture_redact_requests(page)
    start_and_wait(page)
    assert bodies[-1]['terms_file_path'] == str(terms)
    assert 'John' not in pdf_text(tmp_path / 'out' / 'sample_redacted.pdf')


def test_dismissing_choose_file_keeps_previous_selection(page, tmp_path):
    terms = tmp_path / 'terms.txt'
    choose_terms_file(page, terms)
    page.click('#chooseTermsFile')
    page.wait_for_selector('#browser.open .browser-item')
    page.click('#browser >> text=Cancel')
    assert page.input_value('#termsFile') == str(terms)


def test_terms_file_path_persists_across_reload(page, tmp_path):
    terms = tmp_path / 'terms.txt'
    choose_terms_file(page, terms)
    page.reload()
    assert page.input_value('#termsFile') == str(terms)


def test_missing_terms_file_shows_error(page, tmp_path, sample_pdf):
    fill_dirs(page, tmp_path)
    page.fill('#termsFile', str(tmp_path / 'gone.txt'))
    assert start_and_wait(page) == "The terms file couldn't be read. Check that it still exists at that location, or choose it again."


def test_no_terms_file_sends_empty_path(page, tmp_path, sample_pdf):
    fill_dirs(page, tmp_path)
    page.fill('#terms', 'x')
    bodies = capture_redact_requests(page)
    start_and_wait(page)
    assert bodies[-1] == {'input_dir': str(tmp_path / 'in'), 'output_dir': str(tmp_path / 'out'),
                          'terms': 'x', 'terms_file_path': '',
                          'pii_kinds': ['email', 'credit_card', 'ssn']}


def test_server_error_is_shown(page, tmp_path):
    page.fill('#inputDir', '/no/such/dir')
    page.fill('#terms', 'x')
    assert start_and_wait(page) == "The input directory doesn't exist. Check the path, or click Browse to choose it."


def test_failed_file_is_rendered_as_error(page, tmp_path, sample_pdf):
    (tmp_path / 'in' / 'broken.pdf').write_bytes(b'garbage')
    fill_dirs(page, tmp_path)
    page.fill('#terms', 'x')
    start_and_wait(page)
    page.wait_for_function("document.querySelectorAll('.result-item').length === 2")
    err = page.locator('.result-item.err')
    assert err.count() == 1 and err.inner_text().startswith('broken.pdf')
    assert "This file couldn't be opened. It may be damaged or not a real PDF." in err.inner_text()


# ─── cancelRedact ──────────────────────────────────────────────────────────────

def test_cancel_button_posts_cancel(page):
    calls = []
    page.on('request', lambda r: calls.append(r.url) if r.url.endswith('/cancel') else None)
    page.click('.actions >> text=Cancel')
    page.wait_for_timeout(300)
    assert len(calls) == 1
    assert page.inner_text('#status') == 'Ready.', 'nothing was running, so the status is unchanged'


# ─── The reported bug ──────────────────────────────────────────────────────────

def test_rerun_after_editing_terms_file_uses_edited_terms(page, tmp_path, sample_pdf, isolated_app):
    """Repro steps from the bug report: choose a terms file, redact, add more
    terms to the file on disk, redact again. The second output must reflect the
    edited file. A stale redact_terms.txt sits in APP_DIR, as in the real app
    folder, so a failed re-read cannot pass unnoticed."""
    (isolated_app / 'redact_terms.txt').write_text('Alice Smith\n')
    terms = tmp_path / 'terms.txt'
    terms.write_text('Alice Smith\n')
    out = tmp_path / 'out' / 'sample_redacted.pdf'

    fill_dirs(page, tmp_path)
    choose_terms_file(page, terms)
    start_and_wait(page)
    text = pdf_text(out)
    assert 'Alice' not in text and 'John Doe' in text

    terms.write_text('Alice Smith\nJohn Doe\nPhone\n')  # user adds more terms
    assert start_and_wait(page).startswith('Done in ')
    text = pdf_text(out)
    assert 'Alice' not in text
    assert 'John' not in text, 'added term "John Doe" was not redacted'
    assert 'Phone' not in text, 'added term "Phone" was not redacted'

    terms.write_text('Phone\n')  # and removals are honored too
    start_and_wait(page)
    text = pdf_text(out)
    assert 'Phone' not in text
    assert 'Alice Smith' in text and 'John Doe' in text


# ─── Survey, progress and escaping ─────────────────────────────────────────────

def test_status_shows_survey_and_progress(page, tmp_path, sample_pdf):
    from PIL import Image
    Image.new('RGB', (50, 50), 'white').save(tmp_path / 'in' / 'blank.png')
    fill_dirs(page, tmp_path)
    disable_pii(page)
    page.fill('#terms', 'Alice Smith')
    status = start_and_wait(page)
    assert status == 'Done in under 1 sec. 1 file redacted and 1 had no matches.'
    assert page.evaluate("document.getElementById('progressBar').style.width") == '100%'


def test_file_names_are_escaped_in_results(page, tmp_path):
    from conftest import make_pdf
    make_pdf(tmp_path / 'in' / '<b>bold & "quoted".pdf', [['Alice Smith']])
    fill_dirs(page, tmp_path)
    page.fill('#terms', 'Alice Smith')
    start_and_wait(page)
    page.wait_for_function("document.querySelectorAll('.result-item').length === 1")
    assert page.locator('.result-item b').count() == 0, 'file name must not be rendered as HTML'
    assert page.locator('.result-item .reveal').first.inner_text() == '<b>bold & "quoted".pdf'
    assert page.get_attribute('.result-item .reveal', 'data-reveal').endswith('<b>bold & "quoted"_redacted.pdf')


# ─── Status messages (user facing) ─────────────────────────────────────────────

def render(page, data):
    page.evaluate("d => renderStatus(d)", data)
    return page.inner_text('#status'), page.evaluate("document.getElementById('status').dataset.state")


def status_data(**kw):
    data = {'running': False, 'phase': 'idle', 'done': 0, 'total': 0, 'pages_done': 0, 'pages_total': 0,
            'survey': None, 'cancelled': False, 'current': [], 'results': []}
    data.update(kw)
    return data


def ok(name):
    return {'input': f'/in/{name}', 'path': f'/out/{name}', 'result': {'matches': 1}, 'error': None}


def skip(name):
    return {'input': f'/in/{name}', 'path': None, 'result': {'matches': 0, 'skipped': True}, 'error': None}


def bad(name):
    return {'input': f'/in/{name}', 'path': None, 'result': None, 'error': 'This PDF is password-protected.'}


def no_ocr(name):
    return {'input': f'/in/{name}', 'path': None, 'result': None, 'needs_tesseract': True,
            'error': 'Reading text in scanned pages and images needs Tesseract.'}


TESSERACT_HINT = ("couldn't be redacted because Tesseract isn't installed. Install Tesseract to redact "
                  "scanned PDFs and images (click the badge in the top-right corner for help).")


def test_status_while_surveying(page):
    assert render(page, status_data(running=True, phase='surveying', total=48)) == ('Looking through 48 files…', 'busy')


def test_status_while_redacting_never_mentions_workers_or_ocr(page):
    survey = {'counts': {'text PDF': 30, 'mixed PDF': 2, 'scanned PDF': 5, 'image': 10, 'protected PDF': 1},
              'pages': 566, 'scan_pages': 60}
    text, state = render(page, status_data(running=True, phase='redacting', done=20, total=48, pages_done=300,
                                           pages_total=576, survey=survey))
    assert text == ('Found 30 PDFs, 7 scanned PDFs, 10 images and 1 password-protected PDF.\n'
                    'Redacting… 20 of 48 files done.\n'
                    'Scanned pages take a little longer to process.')
    assert state == 'busy'
    for jargon in ('worker', 'OCR', 'text PDF', 'mixed', 'Pages:'):
        assert jargon not in text


def test_status_while_redacting_names_the_files_in_progress(page):
    data = status_data(running=True, phase='redacting', done=2, total=10, current=['alpha.pdf', 'beta.pdf'])
    text, state = render(page, data)
    assert 'Redacting… 2 of 10 files done.\nNow: alpha.pdf, beta.pdf' in text
    assert state == 'busy'


def test_status_while_redacting_without_current_files(page):
    text, _ = render(page, status_data(running=True, phase='redacting', done=1, total=10))
    assert 'Now:' not in text


@pytest.mark.parametrize('results, total, cancelled, expected', [
    ([ok('a'), ok('b')], 2, False, 'Done. 2 files redacted.'),
    ([ok('a')], 1, False, 'Done. 1 file redacted.'),
    ([ok('a'), skip('b'), skip('c'), bad('d')], 4, False, "Done. 1 file redacted, 2 had no matches and 1 couldn't be redacted (see below)."),
    ([skip('a'), skip('b')], 2, False, 'Done. None of the files contain anything to redact, so no copies were made.'),
    ([bad('a')], 1, False, "Done. 0 files redacted and 1 couldn't be redacted (see below)."),
    ([], 0, False, 'No PDFs or images were found in the input directory.'),
    ([ok('a')], 10, True, 'Cancelled. 1 file was redacted before stopping.'),
    ([], 10, True, 'Cancelled. 0 files were redacted before stopping.'),
    ([ok('a.pdf'), no_ocr('scan.pdf'), no_ocr('photo.png')], 3, False,
     'Done. 1 file redacted.\n1 scanned PDF and 1 image ' + TESSERACT_HINT),
    ([no_ocr('a.png'), no_ocr('b.jpg')], 2, False, 'Done. 0 files redacted.\n2 images ' + TESSERACT_HINT),
    ([skip('a.pdf'), no_ocr('s1.pdf'), no_ocr('s2.pdf'), bad('locked.pdf')], 4, False,
     "Done. 0 files redacted, 1 had no matches and 1 couldn't be redacted (see below).\n2 scanned PDFs " + TESSERACT_HINT),
])
def test_finished_messages(page, results, total, cancelled, expected):
    data = status_data(results=results, total=total, done=len(results), cancelled=cancelled)
    assert render(page, data) == (expected, 'done')


def test_results_show_automatic_pii_counts(page):
    result = {'input': '/in/id.pdf', 'path': '/out/id_redacted.pdf',
              'result': {'matches': 2, 'pii': {'email': 2}}, 'error': None}
    page.evaluate("r => renderResults(r)", [result])
    assert page.locator('.result-item .meta').inner_text() == 'Saved as id_redacted.pdf · 2 email addresses'


def test_finished_message_reports_automatically_found_pii(page):
    result = {'input': '/in/id.pdf', 'path': '/out/id_redacted.pdf',
              'result': {'matches': 3, 'pii': {'email': 2, 'ssn': 1}}, 'error': None}
    text, state = render(page, status_data(results=[result], total=1, done=1))
    assert text == 'Done. 1 file redacted.\nAutomatically found 2 email addresses and 1 Social Security number.'
    assert state == 'done'


def test_cancel_during_run_says_cancelling_then_cancelled(page, tmp_path, sample_pdf):
    page.route('**/status', lambda route: route.fulfill(json=status_data(running=True, phase='surveying', total=5)))
    fill_dirs(page, tmp_path)
    page.fill('#terms', 'Alice Smith')
    page.click('text=Start Redaction')
    page.wait_for_function("document.getElementById('status').textContent.startsWith('Looking through')")
    page.click('.actions >> text=Cancel')
    page.wait_for_function("document.getElementById('status').textContent === 'Cancelling…'")


def test_server_not_responding_message(page, tmp_path):
    page.route('**/redact', lambda route: route.abort())
    page.fill('#terms', 'x')
    assert start_and_wait(page) == "DocuCloak isn't responding. Make sure it's running, then try again."


# ─── Tesseract badge and help ──────────────────────────────────────────────────

def open_with_tools(browser, server, available):
    ctx = browser.new_context()
    pg = ctx.new_page()
    state = {'available': available}
    pg.route('**/tools', lambda route: route.fulfill(json={'tesseract': state['available']}))
    pg.goto(server.base + '/')
    pg.wait_for_function("!document.getElementById('tesseractBadge').textContent.startsWith('Checking')")
    return ctx, pg, state


def help_open(pg):
    return pg.evaluate("document.getElementById('tesseractHelp').classList.contains('open')")


def test_badge_when_tesseract_installed(browser, server):
    ctx, pg, _ = open_with_tools(browser, server, True)
    assert pg.inner_text('#tesseractBadge') == 'Tesseract ready'
    assert 'ok' in pg.get_attribute('#tesseractBadge', 'class')
    assert not help_open(pg)
    ctx.close()


def test_help_opens_by_itself_when_tesseract_missing(browser, server):
    ctx, pg, _ = open_with_tools(browser, server, False)
    assert pg.inner_text('#tesseractBadge') == 'Tesseract not found · click for help'
    assert help_open(pg)
    assert 'brew install tesseract' in pg.inner_text('#tesseractHelp')
    pg.click('#tesseractHelp >> text=Dismiss')
    assert not help_open(pg)
    pg.click('#tesseractBadge')            # the badge reopens it
    assert help_open(pg)
    pg.keyboard.press('Escape')
    assert not help_open(pg)
    pg.click('#tesseractBadge')
    pg.mouse.click(5, 5)                    # clicking outside the dialog closes it
    assert not help_open(pg)
    ctx.close()


def test_check_again_after_installing(browser, server):
    ctx, pg, state = open_with_tools(browser, server, False)
    state['available'] = True               # user installs Tesseract
    pg.click('#tesseractHelp >> text=Check again')
    pg.wait_for_function("document.getElementById('tesseractBadge').textContent === 'Tesseract ready'")
    assert not help_open(pg)
    ctx.close()


def test_status_explains_missing_tesseract_during_run(browser, server):
    ctx, pg, _ = open_with_tools(browser, server, False)
    survey = {'counts': {'text PDF': 2, 'scanned PDF': 1, 'image': 3}, 'pages': 5, 'scan_pages': 2}
    text, _ = render(pg, status_data(running=True, phase='redacting', done=1, total=6, survey=survey))
    assert "Tesseract isn't installed, so scanned PDFs and images can't be redacted." in text
    assert 'take a little longer' not in text
    ctx.close()


# ─── Show in Finder ────────────────────────────────────────────────────────────

def capture_reveals(page):
    reveals = []

    def handle(route):
        reveals.append(json.loads(route.request.post_data)['path'])
        route.fulfill(json={'status': 'shown'})
    page.route('**/reveal', handle)
    return reveals


def test_clicking_a_result_shows_it_in_finder(page, tmp_path, sample_pdf):
    from conftest import make_pdf
    make_pdf(tmp_path / 'in' / 'clean.pdf', [['nothing here']])
    reveals = capture_reveals(page)
    fill_dirs(page, tmp_path)
    page.fill('#terms', 'Alice Smith')
    start_and_wait(page)
    page.wait_for_function("document.querySelectorAll('.result-item').length === 2")
    page.click('.result-item.ok .reveal')     # redacted: shows the copy
    page.click('.result-item.skip .reveal')   # no matches: shows the original
    page.wait_for_function("() => true")
    page.wait_for_timeout(300)
    assert reveals == [str(tmp_path / 'out' / 'sample_redacted.pdf'), str((tmp_path / 'in' / 'clean.pdf').resolve())]


def test_show_output_folder_button(page, tmp_path, sample_pdf):
    reveals = capture_reveals(page)
    fill_dirs(page, tmp_path)
    assert page.is_hidden('#showOutput')
    page.fill('#terms', 'Alice Smith')
    start_and_wait(page)
    assert page.is_visible('#showOutput')
    assert page.inner_text('#showOutput') == 'Show output folder'
    assert 'Finder' not in page.content(), 'labels must work on Windows and Linux too'
    page.click('#showOutput')
    page.wait_for_timeout(300)
    assert reveals == [str(tmp_path / 'out')]


def test_show_output_folder_disabled_when_nothing_was_saved(page, tmp_path, sample_pdf):
    fill_dirs(page, tmp_path)
    disable_pii(page)
    page.fill('#terms', 'not in the document')
    start_and_wait(page)
    assert page.is_visible('#showOutput') and page.is_disabled('#showOutput')
    assert page.get_attribute('#showOutput', 'title') == 'Nothing was saved to the output folder'
    page.fill('#terms', 'Alice Smith')        # a later run that saves something re-enables it
    start_and_wait(page)
    assert page.is_enabled('#showOutput') and not page.get_attribute('#showOutput', 'title')


@pytest.mark.parametrize('results', [[skip('a.pdf'), skip('b.pdf')], [no_ocr('scan.pdf'), no_ocr('photo.png')],
                                     [skip('a.pdf'), no_ocr('photo.png')], []])
def test_show_output_folder_disabled_for_every_nothing_saved_outcome(page, results):
    render(page, status_data(results=results, total=len(results), done=len(results)))
    assert page.is_disabled('#showOutput')


def test_show_output_folder_hidden_while_running(page, tmp_path, sample_pdf):
    render(page, status_data(results=[ok('a.pdf')], total=1, done=1))
    assert page.is_enabled('#showOutput')
    page.route('**/status', lambda route: route.fulfill(json=status_data(running=True, phase='surveying', total=5)))
    fill_dirs(page, tmp_path)
    page.fill('#terms', 'Alice Smith')
    page.click('text=Start Redaction')
    page.wait_for_function("document.getElementById('status').textContent.startsWith('Looking through')")
    assert page.is_hidden('#showOutput')


def test_show_in_finder_error_is_shown(page, tmp_path, sample_pdf):
    page.route('**/reveal', lambda route: route.fulfill(status=404, json={'error': 'That file or folder no longer exists.'}))
    fill_dirs(page, tmp_path)
    page.fill('#terms', 'Alice Smith')
    start_and_wait(page)
    page.click('.result-item .reveal')
    page.wait_for_function("document.getElementById('status').textContent === 'That file or folder no longer exists.'")


def test_ui_without_tesseract_end_to_end(browser, server, tmp_path, monkeypatch):
    """A folder of only scanned PDFs and images, no Tesseract: explained, nothing saved, button disabled."""
    from lib import tesseract
    from PIL import Image
    from test_redact_pdf import scanned_pdf
    monkeypatch.setattr(tesseract, '_find_tesseract', lambda: None)
    scanned_pdf(tmp_path / 'in' / 'scan.pdf', ['Alice Smith'])
    Image.new('RGB', (40, 40), 'white').save(tmp_path / 'in' / 'photo.png')
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto(server.base + '/')
    page.wait_for_function("document.getElementById('tesseractHelp').classList.contains('open')")
    page.click('#tesseractHelp >> text=Dismiss')
    page.fill('#inputDir', str(tmp_path / 'in'))
    page.fill('#outputDir', str(tmp_path / 'out'))
    page.fill('#terms', 'Alice Smith')
    page.click('text=Start Redaction')
    page.wait_for_function("document.getElementById('status').dataset.state === 'done'", timeout=30000)
    assert page.inner_text('#status') == (
        "Done in under 1 sec. 0 files redacted.\n1 scanned PDF and 1 image couldn't be redacted because Tesseract isn't installed. "
        "Install Tesseract to redact scanned PDFs and images (click the badge in the top-right corner for help).")
    assert page.is_disabled('#showOutput')
    ctx.close()


# ─── Outdated server ───────────────────────────────────────────────────────────

OUTDATED = ("DocuCloak was updated but is still running the old version. Restart it with ./server.sh restart "
            "(or close and reopen docucloak.command), then reload this page.")


@pytest.mark.parametrize('answer', [
    {'json': {'outdated': True}},             # current server noticed app.py changed
    {'status': 404, 'body': ''},              # older server without /version
])
def test_page_warns_when_server_is_outdated(browser, server, answer):
    ctx = browser.new_context()
    pg = ctx.new_page()
    pg.route('**/version', lambda route: route.fulfill(**answer))
    pg.goto(server.base + '/')
    pg.wait_for_function("document.getElementById('status').dataset.state === 'error'")
    assert pg.inner_text('#status') == OUTDATED
    ctx.close()


def test_page_is_quiet_when_server_is_current(page):
    page.wait_for_timeout(500)
    assert page.inner_text('#status') == 'Ready.'


def test_browse_against_outdated_server_says_so(page, tmp_path):
    page.route('**/list_dir*', lambda route: route.fulfill(status=404, body=''))
    page.click('#inputDir ~ button')
    page.wait_for_selector('#browserError:not([hidden])')
    assert page.inner_text('#browserError') == OUTDATED


def test_start_against_outdated_server_says_so(page, tmp_path, sample_pdf):
    page.route('**/redact', lambda route: route.fulfill(status=404, body=''))
    fill_dirs(page, tmp_path)
    page.fill('#terms', 'x')
    assert start_and_wait(page) == OUTDATED


def test_folder_mode_footer_is_blank_while_loading(page):
    page.route('**/list_dir*', lambda route: None)   # never answers
    page.click('#inputDir ~ button')
    page.wait_for_selector('#browser.open')
    assert page.inner_text('#browserSelection') == ''


# ─── Elapsed and remaining time ────────────────────────────────────────────────

@pytest.mark.parametrize('seconds, text', [(0, '0 sec'), (7.9, '7 sec'), (59, '59 sec'), (60, '1 min'),
                                           (125, '2 min 5 sec'), (3600, '1 hr'), (3720, '1 hr 2 min')])
def test_format_duration(page, seconds, text):
    assert page.evaluate("s => formatDuration(s)", seconds) == text


@pytest.mark.parametrize('seconds, text', [(None, 'Estimating time left…'), (3, 'Almost done'), (7, 'About 5 sec left'),
                                           (33, 'About 35 sec left'), (61, 'About 1 min left'),
                                           (144, 'About 2 min 20 sec left'), (1290, 'About 22 min left')])
def test_describe_remaining(page, seconds, text):
    assert page.evaluate("s => describeRemaining(s)", seconds) == text


@pytest.mark.parametrize('files, text', [
    ([], ''),
    (['a.pdf'], 'Now: a.pdf'),
    (['a.pdf', 'b.pdf'], 'Now: a.pdf, b.pdf'),
    (['a.pdf', 'b.pdf', 'c.pdf'], 'Now: a.pdf, b.pdf, c.pdf'),
    (['a.pdf', 'b.pdf', 'c.pdf', 'd.pdf'], 'Now: a.pdf, b.pdf, c.pdf and 1 more file'),
    (['a', 'b', 'c', 'd', 'e'], 'Now: a, b, c and 2 more files'),
])
def test_describe_current(page, files, text):
    assert page.evaluate("files => describeCurrent(files)", files) == text


def test_running_status_shows_elapsed_and_remaining(page):
    data = status_data(running=True, phase='redacting', done=3, total=10, elapsed=12.0, remaining=33.0,
                       survey={'counts': {'text PDF': 10}, 'pages': 10, 'scan_pages': 0})
    text, _ = render(page, data)
    assert text.split('\n') == ['Found 10 PDFs.', 'Redacting… 3 of 10 files done.', 'Elapsed 12 sec · About 35 sec left']
    width = page.evaluate("parseFloat(document.getElementById('progressBar').style.width)")
    assert width == pytest.approx(100 * 12 / 45, abs=0.5), 'progress bar is time-based once there is an estimate'


def test_running_status_before_an_estimate(page):
    data = status_data(running=True, phase='redacting', done=0, total=10, elapsed=0.6, remaining=None,
                       pages_done=2, pages_total=10)
    text, _ = render(page, data)
    assert 'Elapsed 0 sec · Estimating time left…' in text
    assert page.evaluate("document.getElementById('progressBar').style.width") == '20%', 'pages until an estimate exists'


def test_times_count_along_between_updates(page):
    data = status_data(running=True, phase='redacting', done=3, total=10, elapsed=10.0, remaining=33.0)
    page.evaluate("d => { receiveTiming(d); renderStatus(d); }", data)
    assert 'Elapsed 10 sec · About 35 sec left' in page.inner_text('#status')
    page.wait_for_timeout(2300)
    page.evaluate("() => renderStatus(timing.data)")  # what the once-a-second refresh does
    assert 'Elapsed 12 sec · About 30 sec left' in page.inner_text('#status')


def test_estimate_is_smoothed(page):
    first = status_data(running=True, phase='redacting', done=3, total=10, elapsed=10.0, remaining=40.0)
    page.evaluate("d => receiveTiming(d)", first)
    jumpy = dict(first, remaining=20.0)
    page.evaluate("d => { receiveTiming(d); renderStatus(d); }", jumpy)
    assert 'About 30 sec left' in page.inner_text('#status'), 'a sudden drop to 20 is blended with the 40 counting down'


@pytest.mark.parametrize('elapsed, cancelled, expected', [
    (0.4, False, 'Done in under 1 sec. 1 file redacted.'),
    (125.0, False, 'Done in 2 min 5 sec. 1 file redacted.'),
    (5.2, True, 'Cancelled after 5 sec. 1 file was redacted before stopping.'),
    (None, False, 'Done. 1 file redacted.'),
])
def test_finished_message_says_how_long_it_took(page, elapsed, cancelled, expected):
    data = status_data(results=[ok('a.pdf')], total=3 if cancelled else 1, done=1, elapsed=elapsed, cancelled=cancelled)
    assert render(page, data)[0] == expected
