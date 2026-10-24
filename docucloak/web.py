"""HTTP routes for the page, the file browser and the redaction API."""

import json
import os
import re
import subprocess
import sys
import threading
from http.server import SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config, jobs, pii, redaction, scheduler, tesseract
from . import terms as term_utils
from .logging_setup import logger

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
            self._json_response(tesseract._tesseract_status())
            return
        if path == '/version':
            current = config._code_fingerprint()
            outdated = current is not None and current != config._STARTED_FINGERPRINT
            if outdated:
                logger.warning('app files changed since this server started; restart it to use the new version')
            self._json_response({'outdated': outdated})
            return
        if path == '/list_dir':
            query = parse_qs(parsed.query)
            arg = lambda key: (query.get(key) or [''])[0]
            self._json_response(*_list_dir(arg('path'), arg('start'), arg('mode') == 'file', arg('field')))
            return
        if path == '/status':
            with jobs._job_lock:
                self._json_response({
                    'running': jobs.job_state['running'],
                    'phase': jobs.job_state['phase'],
                    'done': jobs.job_state['done'],
                    'total': jobs.job_state['total'],
                    'pages_done': jobs.job_state['pages_done'],
                    'pages_total': jobs.job_state['pages_total'],
                    'survey': jobs.job_state['survey'],
                    'cancelled': jobs.job_state['cancelled'],
                    **_timing(jobs.job_state),
                    'results': jobs.job_state['results'],
                    'errors': jobs.job_state['errors'],
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
                logo_version = str(int(os.path.getmtime(os.path.join(config.APP_DIR, 'logo.svg'))))
            except OSError:
                logo_version = '0'
            with open(os.path.join(config.APP_DIR, 'index.html'), 'r') as f:
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
            with open(os.path.join(config.APP_DIR, 'logo.svg'), 'rb') as f:
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
            raw_pii_kinds = payload.get('pii_kinds') or ()
            if isinstance(raw_pii_kinds, str):
                raw_pii_kinds = (raw_pii_kinds,)
            pii_kinds = tuple(dict.fromkeys(k for k in raw_pii_kinds if k in pii.PII_TYPES))
            logger.info('Start Redaction clicked: input directory=%r, output directory=%r, terms file=%r',
                        input_dir, output_dir, terms_file_path)
            logger.info('Automatic PII detection: %s',
                        ', '.join(pii.LABELS[k] for k in pii_kinds) or 'off')
            text_terms = term_utils._parse_terms(terms_raw)
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
                file_terms = term_utils._parse_terms(file_text)
                logger.info('Read terms file %s from disk: %d term(s)', terms_file_path, len(file_terms))
                terms_file_content = file_text + '\n' + terms_file_content if terms_file_content else file_text
            combined_raw = terms_raw
            if terms_file_content and terms_file_content.strip():
                combined_raw = terms_raw + '\n' + terms_file_content if terms_raw else terms_file_content
            terms = term_utils._parse_terms(combined_raw)
            if not terms:
                default_terms_path = os.path.join(config.APP_DIR, 'redact_terms.txt')
                if os.path.isfile(default_terms_path):
                    try:
                        with open(default_terms_path, 'r', encoding='utf-8', errors='ignore') as f:
                            combined_raw = f.read()
                        terms = term_utils._parse_terms(combined_raw)
                        logger.info('No terms supplied; using fallback file %s', default_terms_path)
                    except Exception as e:
                        logger.warning('Could not read fallback terms file %s: %s', default_terms_path, e)
            if not terms and not pii_kinds:
                logger.warning('Redaction not started: no terms and no PII types provided')
                self._json_response({'error': 'Enter at least one term to redact, choose a terms file, '
                                              'or choose something to find automatically.'}, 400)
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
            cancel = jobs._start_job()

            # Collect files, skipping the output directory if it lives inside the input directory
            files = []
            for root, dirs, filenames in os.walk(input_dir):
                root_path = Path(root).resolve()
                if root_path == out_resolved or out_resolved in root_path.parents:
                    dirs[:] = []
                    continue
                files.extend(root_path / fn for fn in sorted(filenames) if Path(fn).suffix.lower() in redaction.SUPPORTED_EXTS)
            with jobs._job_lock:
                jobs.job_state['total'] = len(files)
            logger.info('Found %d supported file(s) in %s', len(files), input_dir)
            output_root = Path(output_dir).expanduser()

            def output_for(fp):
                # Mirror the input's subfolders so same-named files never collide
                rel = fp.relative_to(in_resolved)
                return output_root / rel.parent / (fp.stem + '_redacted' + fp.suffix)

            threading.Thread(target=scheduler._run_job, args=(cancel.job_id, cancel, files, output_for, terms, pii_kinds),
                             name='redaction-job', daemon=True).start()
            self._json_response({'status': 'started', 'files': len(files)})
            return

        if path == '/cancel':
            logger.info('Cancel clicked')
            jobs._cancel_job()
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
        file_path = os.path.join(config.APP_DIR, folder, name)
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
            with os.scandir(os.path.join(config.APP_DIR, folder)) as entries:
                newest = max([newest] + [int(e.stat().st_mtime) for e in entries if e.is_file()])
        except OSError:
            pass
    return str(newest)

def _timing(state):
    """Elapsed seconds, estimated seconds left and files in progress for /status
    (call with jobs._job_lock held)."""
    cancel = state['cancel']
    progress = getattr(cancel, 'progress', None)
    if state['running'] and progress is not None:
        remaining = progress.remaining() if state['phase'] == 'redacting' else None
        return {'elapsed': round(progress.elapsed(), 1),
                'remaining': None if remaining is None else round(remaining, 1),
                'current': progress.current_files()}
    return {'elapsed': state['elapsed'], 'remaining': None, 'current': []}

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
        return f'{len(term_utils._parse_terms(str(value or "")))} term(s)'
    return repr(value)

_UI_FIELD_NAMES = {
    'inputDir': 'input directory',
    'outputDir': 'output directory',
    'termsFile': 'redact terms file',
    'terms': 'redaction terms text field',
}

