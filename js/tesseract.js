// Tesseract badge and help dialog.

let tesseractAvailable = true;

function openTesseractHelp() {
  document.getElementById('tesseractHelp').classList.add('open');
}

function closeTesseractHelp() {
  document.getElementById('tesseractHelp').classList.remove('open');
}

// Badge in the corner; the help opens by itself when Tesseract is missing
async function checkTesseract({openHelp = true} = {}) {
  const badge = document.getElementById('tesseractBadge');
  try {
    const data = await (await fetch('/tools')).json();
    tesseractAvailable = data.tesseract;
    if (data.tesseract) {
      badge.className = 'tool-badge ok';
      badge.textContent = 'Tesseract ready';
      badge.onclick = null;
      closeTesseractHelp();
    } else {
      badge.className = 'tool-badge missing';
      badge.textContent = 'Tesseract not found · click for help';
      badge.onclick = openTesseractHelp;
      if (openHelp) openTesseractHelp();
    }
  } catch (e) {
    badge.className = 'tool-badge';
    badge.textContent = "Couldn't check for Tesseract";
  }
}

document.addEventListener('keydown', e => { if (e.key === 'Escape') closeTesseractHelp(); });

document.addEventListener('click', e => { if (e.target.id === 'tesseractHelp') closeTesseractHelp(); });
