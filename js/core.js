// Shared helpers: status box, server requests, UI event log, saved fields, Show in Finder.

// Show a file (selected) or folder in the system file manager
async function revealPath(path) {
  if (!path) return;
  try {
    const res = await fetch('/reveal', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({path})});
    const data = await res.json();
    if (data.error) setStatus(data.error, 'error');
  } catch (e) {
    setStatus(NOT_RESPONDING, 'error');
  }
}

document.addEventListener('click', e => {
  const target = e.target.closest('[data-reveal]');
  if (target) revealPath(target.dataset.reveal);
});

// Send UI events to the server log. Chained so they are logged in the order
// they happened, and so Start Redaction is logged after any pending field edit.
let pendingLog = Promise.resolve();

function logEvent(payload) {
  pendingLog = pendingLog.then(() => fetch('/log', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  })).catch(() => {});
  return pendingLog;
}

// Paths only: the redaction terms are sensitive and never stored or sent to the log.
const SAVED_FIELD_IDS = ['inputDir', 'outputDir', 'termsFile'];

function fieldValues() {
  const values = {};
  SAVED_FIELD_IDS.forEach(id => { values[id] = document.getElementById(id).value; });
  return values;
}

function countTerms(text) {
  return new Set(text.split(/[\r\n,]+/).map(t => t.trim()).filter(Boolean)).size;
}

function saveFields() {
  localStorage.setItem('dc_inputDir', document.getElementById('inputDir').value);
  localStorage.setItem('dc_outputDir', document.getElementById('outputDir').value);
  localStorage.setItem('dc_termsFile', document.getElementById('termsFile').value);
}

function restoreFields() {
  const i = document.getElementById('inputDir');
  const o = document.getElementById('outputDir');
  const f = document.getElementById('termsFile');
  if (localStorage.getItem('dc_inputDir') !== null) i.value = localStorage.getItem('dc_inputDir');
  if (localStorage.getItem('dc_outputDir') !== null) o.value = localStorage.getItem('dc_outputDir');
  localStorage.removeItem('dc_terms');  // purge terms saved by older versions
  if (localStorage.getItem('dc_termsFile') !== null) f.value = localStorage.getItem('dc_termsFile');
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}

const NOT_RESPONDING = "DocuCloak isn't responding. Make sure it's running, then try again.";

const OUTDATED = "DocuCloak was updated but is still running the old version. Restart it with ./server.sh restart " +
                 "(or close and reopen docucloak.command), then reload this page.";

// The page is always current (served fresh), but the server only picks up app.py
// changes when restarted. An older server doesn't know newer requests: it answers
// them with a non-JSON 404, which means "outdated", not "not responding".
async function readJson(res) {
  try {
    return await res.json();
  } catch (e) {
    throw new Error(OUTDATED);
  }
}

async function checkServerVersion() {
  try {
    const res = await fetch('/version');
    const data = res.ok ? await readJson(res) : {outdated: true};
    if (data.outdated) setStatus(OUTDATED, 'error');
  } catch (e) {
    setStatus(e.message === OUTDATED ? OUTDATED : NOT_RESPONDING, 'error');
  }
}

// state: busy | done | error (lets tests and styles tell where a run is, independent of wording)
function setStatus(text, state = 'busy') {
  const el = document.getElementById('status');
  el.textContent = text;
  el.dataset.state = state;
}

// fraction of work done (0 to 1), or null to hide the bar
function showProgress(fraction) {
  const bar = document.getElementById('progressBar');
  bar.parentElement.hidden = fraction === null;
  bar.style.width = (fraction === null ? 0 : 100 * fraction) + '%';
}

function plural(n, word) {
  return `${n} ${word}${n === 1 ? '' : 's'}`;
}

// ['a', 'b', 'c'] -> 'a, b and c'
function listJoin(items) {
  return items.length < 2 ? items.join('') : items.slice(0, -1).join(', ') + ' and ' + items[items.length - 1];
}
