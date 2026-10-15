// Page start-up: restore saved fields, check the server and Tesseract, log field edits.

window.addEventListener('load', () => {
  checkServerVersion();
  checkTesseract();
  restoreFields();
  logEvent({event: 'page_loaded', value: fieldValues()});
  SAVED_FIELD_IDS.forEach(id => {
    const el = document.getElementById(id);
    el.addEventListener('input', saveFields);
    // 'change' fires once the user finishes editing (blur/Enter), not per keystroke
    el.addEventListener('change', () => logEvent({event: 'field_changed', field: id, value: el.value}));
  });
  // For the terms text field, report only how many terms it holds
  const termsEl = document.getElementById('terms');
  termsEl.addEventListener('change', () => logEvent({event: 'field_changed', field: 'terms', count: countTerms(termsEl.value)}));
});
