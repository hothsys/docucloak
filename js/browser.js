// In-page file browser for Browse and Choose File.

// ─── File browser ───────────────────────────────────────────────────────────
// Folders are listed by the server and picked inside the page. A native macOS
// dialog opened by the background server process appears behind the browser
// window, where the user can't even switch to it, so the page would hang.

const ICON_FOLDER = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M3 6.5A2.5 2.5 0 0 1 5.5 4h4.2l2 2.2h6.8A2.5 2.5 0 0 1 21 8.7v8.8a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 17.5z"/></svg>';

const ICON_FILE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/></svg>';

const BROWSE_TITLES = {inputDir: 'Choose the input directory', outputDir: 'Choose the output directory', termsFile: 'Choose a terms file'};

const browser = {fieldId: null, mode: 'dir', path: '', selected: null};

function pickDir(fieldId) { openBrowser(fieldId, 'dir'); }

function pickFile(fieldId) { openBrowser(fieldId, 'file'); }

async function openBrowser(fieldId, mode) {
  Object.assign(browser, {fieldId, mode, path: '', selected: null});
  document.getElementById('browserTitle').textContent = BROWSE_TITLES[fieldId] || 'Choose';
  document.getElementById('browserChoose').textContent = mode === 'file' ? 'Choose file' : 'Choose this folder';
  // Clear the previous listing so nothing stale shows while the folder loads
  document.getElementById('browserList').innerHTML = '';
  document.getElementById('browserError').hidden = true;
  updateBrowserFooter();
  document.getElementById('browser').classList.add('open');
  // Open where the field points; the server falls back to the home folder if that doesn't exist
  const start = document.getElementById(fieldId).value.trim();
  await loadFolder('', start);
}

async function loadFolder(path, start = '') {
  const params = new URLSearchParams({mode: browser.mode, field: browser.fieldId});
  if (path) params.set('path', path); else params.set('start', start);
  const errorEl = document.getElementById('browserError');
  try {
    const res = await fetch('/list_dir?' + params);
    const data = await readJson(res);
    if (data.error) {
      errorEl.textContent = data.error;
      errorEl.hidden = false;
      return;
    }
    errorEl.hidden = true;
    browser.path = data.path;
    browser.selected = null;
    renderBrowser(data);
  } catch (e) {
    errorEl.textContent = e.message === OUTDATED ? OUTDATED : NOT_RESPONDING;
    errorEl.hidden = false;
  }
}

function renderBrowser(data) {
  document.getElementById('browserPlaces').innerHTML = data.places.map(p =>
    `<button type="button" class="btn-secondary" data-path="${escapeHtml(p.path)}">${escapeHtml(p.name)}</button>`).join('');
  document.getElementById('browserCrumbs').innerHTML = data.crumbs.map((c, i) =>
    (i > 1 ? '<span aria-hidden="true">/</span>' : '') +
    `<button type="button" data-path="${escapeHtml(c.path)}">${escapeHtml(c.name)}</button>`).join('');
  const rows = [];
  if (data.parent) rows.push(`<button type="button" class="browser-item dir" data-path="${escapeHtml(data.parent)}">${ICON_FOLDER}<span>..</span></button>`);
  for (const name of data.dirs) {
    rows.push(`<button type="button" class="browser-item dir" data-path="${escapeHtml(joinPath(data.path, name))}">${ICON_FOLDER}<span>${escapeHtml(name)}</span></button>`);
  }
  for (const name of data.files) {
    rows.push(`<button type="button" class="browser-item file" data-file="${escapeHtml(joinPath(data.path, name))}">${ICON_FILE}<span>${escapeHtml(name)}</span></button>`);
  }
  const empty = !data.dirs.length && !data.files.length;
  document.getElementById('browserList').innerHTML = rows.join('') + (empty ?
    `<div class="browser-empty">${browser.mode === 'file' ? 'No files or folders here.' : 'No folders here.'}</div>` : '') +
    (data.truncated ? '<div class="browser-empty">Only the first 5,000 items are shown.</div>' : '');
  document.getElementById('browserList').scrollTop = 0;
  updateBrowserFooter();
}

function joinPath(folder, name) {
  return folder.endsWith('/') || folder.endsWith('\\') ? folder + name : folder + (folder.includes('\\') ? '\\' : '/') + name;
}

function updateBrowserFooter() {
  const target = browser.mode === 'file' ? browser.selected : browser.path;
  document.getElementById('browserSelection').textContent = target || (browser.mode === 'file' ? 'Select a file' : '');
  document.getElementById('browserChoose').disabled = !target;
}

function chooseInBrowser() {
  const value = browser.mode === 'file' ? browser.selected : browser.path;
  if (!value) return;
  const field = document.getElementById(browser.fieldId);
  field.value = value;
  saveFields();
  logEvent({event: 'browse_chosen', field: browser.fieldId, value});
  closeBrowser(true);
}

function closeBrowser(chosen) {
  const overlay = document.getElementById('browser');
  if (!overlay.classList.contains('open')) return;
  overlay.classList.remove('open');
  if (!chosen) logEvent({event: 'browse_cancelled', field: browser.fieldId});
}

document.addEventListener('click', e => {
  if (e.target.id === 'browser') return closeBrowser(false);
  const nav = e.target.closest('#browser [data-path]');
  if (nav) return loadFolder(nav.dataset.path);
  const file = e.target.closest('#browser [data-file]');
  if (file) {
    browser.selected = file.dataset.file;
    document.querySelectorAll('#browser .browser-item.selected').forEach(el => el.classList.remove('selected'));
    file.classList.add('selected');
    updateBrowserFooter();
  }
});

document.addEventListener('dblclick', e => {
  if (e.target.closest('#browser [data-file]')) chooseInBrowser();
});

document.addEventListener('keydown', e => {
  if (!document.getElementById('browser').classList.contains('open')) return;
  if (e.key === 'Escape') closeBrowser(false);
  // Enter chooses, unless a folder or file row has focus (then Enter opens or selects it)
  if (e.key === 'Enter' && !e.target.closest('#browser button') && !document.getElementById('browserChoose').disabled) {
    e.preventDefault();
    chooseInBrowser();
  }
});
