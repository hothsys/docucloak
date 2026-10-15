// Start/cancel redaction, progress polling, status summary and results list.

let pollTimer = null;

let lastOutputDir = '';

async function startRedact() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  // No separate /cancel call: the server cancels any running job when a new one starts.
  await pendingLog;
  saveFields();
  const inputDir = document.getElementById('inputDir').value.trim();
  const outputDir = document.getElementById('outputDir').value.trim();
  const terms = document.getElementById('terms').value;
  // Only the path is sent; the server reads the file's current contents itself.
  const termsFilePath = document.getElementById('termsFile').value.trim();
  setStatus('Starting…');
  showProgress(null);
  document.getElementById('showOutput').hidden = true;
  lastOutputDir = outputDir;
  document.getElementById('results').innerHTML = '';

  try {
    const res = await fetch('/redact', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({input_dir: inputDir, output_dir: outputDir, terms: terms, terms_file_path: termsFilePath})
    });
    const data = await readJson(res);
    if (data.error) {
      setStatus(data.error, 'error');
      return;
    }
    setStatus(`Looking through ${plural(data.files, 'file')}…`);
    pollStatus();
  } catch (e) {
    setStatus(e.message === OUTDATED ? OUTDATED : NOT_RESPONDING, 'error');
  }
}

async function cancelRedact() {
  try {
    await fetch('/cancel', {method: 'POST'});
  } catch (e) {
    setStatus(NOT_RESPONDING, 'error');
    return;
  }
  if (pollTimer) setStatus('Cancelling…');
}

// "30 PDFs, 7 scanned PDFs, 10 images and 1 password-protected PDF"
function describeFound(survey) {
  const c = survey.counts;
  const groups = [
    [c['text PDF'] || 0, 'PDF'],
    [(c['scanned PDF'] || 0) + (c['mixed PDF'] || 0), 'scanned PDF'],
    [c['image'] || 0, 'image'],
    [c['protected PDF'] || 0, 'password-protected PDF'],
    [c['unreadable PDF'] || 0, 'unreadable file'],
  ];
  return listJoin(groups.filter(([n]) => n).map(([n, word]) => plural(n, word)));
}

// ─── Time elapsed and remaining ─────────────────────────────────────────────
// The server measures elapsed time and estimates the time left (a simulation of
// the remaining work, calibrated on finished tasks). Between status updates the
// page counts both along, and it smooths the estimate so it doesn't jitter.

const timing = {data: null, receivedAt: 0, remaining: null, ticker: null};

function receiveTiming(data) {
  const now = performance.now() / 1000;
  if (data.remaining === null || data.remaining === undefined) {
    timing.remaining = null;
  } else if (timing.remaining === null) {
    timing.remaining = data.remaining;
  } else {
    const countedDown = timing.remaining - (now - timing.receivedAt);
    timing.remaining = 0.6 * countedDown + 0.4 * data.remaining;
  }
  timing.data = data;
  timing.receivedAt = now;
}

// Elapsed and remaining seconds right now (counted along since the last update)
function currentTiming(data) {
  if (data !== timing.data) return {elapsed: data.elapsed, remaining: data.remaining};
  const since = performance.now() / 1000 - timing.receivedAt;
  return {
    elapsed: data.elapsed === null || data.elapsed === undefined ? null : data.elapsed + (data.running ? since : 0),
    remaining: timing.remaining === null ? null : Math.max(0, timing.remaining - since),
  };
}

// 7 -> "7 sec", 125 -> "2 min 5 sec", 3720 -> "1 hr 2 min"
function formatDuration(seconds) {
  const s = Math.floor(seconds);
  if (s < 60) return `${s} sec`;
  if (s < 3600) return `${Math.floor(s / 60)} min` + (s % 60 ? ` ${s % 60} sec` : '');
  return `${Math.floor(s / 3600)} hr` + (Math.floor(s / 60) % 60 ? ` ${Math.floor(s / 60) % 60} min` : '');
}

// Rounded so the estimate doesn't pretend to be more precise than it is
function describeRemaining(seconds) {
  if (seconds === null || seconds === undefined) return 'Estimating time left…';
  if (seconds < 5) return 'Almost done';
  if (seconds < 60) return `About ${Math.max(5, Math.round(seconds / 5) * 5)} sec left`;
  if (seconds < 600) return `About ${formatDuration(Math.round(seconds / 10) * 10)} left`;
  return `About ${formatDuration(Math.round(seconds / 60) * 60)} left`;
}

function finishedMessage(data) {
  const results = data.results || [];
  const redacted = results.filter(r => r.path).length;
  const skipped = results.filter(r => !r.error && r.result && r.result.skipped).length;
  const needOcr = results.filter(r => r.needs_tesseract);
  const otherFailed = results.filter(r => r.error && !r.needs_tesseract).length;
  // "Done in 12 sec." when the server reported how long the run took
  const took = data.elapsed === null || data.elapsed === undefined ? '' :
    (data.elapsed < 1 ? ' in under 1 sec' : ` in ${formatDuration(data.elapsed)}`);
  if (data.cancelled) {
    return `Cancelled${took ? ' after' + took.slice(3) : ''}. ${plural(redacted, 'file')} ${redacted === 1 ? 'was' : 'were'} redacted before stopping.`;
  }
  if (data.total === 0) return 'No PDFs or images were found in the input directory.';
  if (redacted === 0 && needOcr.length === 0 && otherFailed === 0) {
    return `Done${took}. None of the files contain your terms, so no copies were made.`;
  }
  const parts = [`${plural(redacted, 'file')} redacted`];
  if (skipped) parts.push(`${skipped} had no matches`);
  if (otherFailed) parts.push(`${otherFailed} couldn't be redacted (see below)`);
  const lines = [`Done${took}. ${listJoin(parts)}.`];
  if (needOcr.length) {
    // Spell out the cause: these files need Tesseract
    const pdfs = needOcr.filter(r => /\.pdf$/i.test(r.input)).length;
    const images = needOcr.length - pdfs;
    const what = listJoin([pdfs && plural(pdfs, 'scanned PDF'), images && plural(images, 'image')].filter(Boolean));
    lines.push(`${what} couldn't be redacted because Tesseract isn't installed. ` +
               'Install Tesseract to redact scanned PDFs and images (click the badge in the top-right corner for help).');
  }
  return lines.join('\n');
}

function renderStatus(data) {
  const t = currentTiming(data);
  if (data.running && data.phase === 'surveying') {
    setStatus(`Looking through ${plural(data.total, 'file')}…`);
    showProgress(0);
  } else if (data.running) {
    const lines = [];
    if (data.survey) lines.push(`Found ${describeFound(data.survey)}.`);
    lines.push(`Redacting… ${data.done} of ${plural(data.total, 'file')} done.`);
    if (t.elapsed !== null && t.elapsed !== undefined) {
      lines.push(`Elapsed ${formatDuration(t.elapsed)} · ${describeRemaining(t.remaining)}`);
    }
    const c = data.survey ? data.survey.counts : {};
    const needsTesseract = c['image'] || c['scanned PDF'] || c['mixed PDF'];
    if (needsTesseract && !tesseractAvailable) {
      lines.push("Tesseract isn't installed, so scanned PDFs and images can't be redacted.");
    } else if (data.survey && data.survey.scan_pages) {
      lines.push('Scanned pages take a little longer to process.');
    }
    setStatus(lines.join('\n'));
    // Time-based once there's an estimate (scanned pages take far longer than text
    // pages, so pages done would mislead); pages done until then
    if (t.remaining !== null && t.remaining !== undefined && t.elapsed) {
      showProgress(t.elapsed / (t.elapsed + t.remaining));
    } else {
      showProgress(data.pages_total ? data.pages_done / data.pages_total : 0);
    }
  } else {
    setStatus(finishedMessage(data), 'done');
    // Shown after every run; disabled when nothing was saved (no matches, or files that couldn't be read)
    const button = document.getElementById('showOutput');
    const saved = (data.results || []).some(r => r.path);
    button.hidden = false;
    button.disabled = !saved;
    button.title = saved ? '' : 'Nothing was saved to the output folder';
    showProgress(data.cancelled || data.total === 0 ? null : 1);
  }
}

function renderResults(results) {
  const baseName = p => p.split(/[\\/]/).pop();
  document.getElementById('results').innerHTML = results.map(r => {
    const skipped = !r.error && r.result && r.result.skipped;
    const cls = r.error ? 'err' : (skipped ? 'skip' : 'ok');
    const meta = r.error ? r.error : (skipped ? 'No matches, nothing to redact' : 'Saved as ' + baseName(r.path));
    const name = r.input ? baseName(r.input) : 'unknown';
    // Redacted files show their copy; skipped or failed files show the original
    const target = r.path || r.input || '';
    return `<div class="result-item ${cls}"><button type="button" class="reveal" data-reveal="${escapeHtml(target)}" title="Show in folder: ${escapeHtml(target)}">${escapeHtml(name)}</button><span class="meta">${escapeHtml(meta)}</span></div>`;
  }).join('');
}

function stopTicker() {
  if (timing.ticker) clearInterval(timing.ticker);
  timing.ticker = null;
}

function pollStatus() {
  if (pollTimer) clearInterval(pollTimer);
  stopTicker();
  Object.assign(timing, {data: null, remaining: null});
  // Re-render every second between updates so elapsed and remaining count smoothly
  timing.ticker = setInterval(() => {
    if (timing.data && timing.data.running) renderStatus(timing.data);
  }, 1000);
  pollTimer = setInterval(async () => {
    try {
      const res = await fetch('/status');
      const data = await res.json();
      receiveTiming(data);
      renderStatus(data);
      if (data.results && data.results.length) renderResults(data.results);
      if (!data.running) {
        clearInterval(pollTimer);
        stopTicker();
      }
    } catch (e) {
      // ignore poll errors
    }
  }, 500);
}
