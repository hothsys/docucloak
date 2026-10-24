"""Install the optional runtime packages on first run."""

import subprocess
import sys

from .logging_setup import logger

def _ensure_dependencies():
    required = {
        'pymupdf': 'PyMuPDF>=1.23.0',
        'PIL': 'Pillow>=10.0.0',
        'pytesseract': 'pytesseract>=0.3.10',
    }
    missing = []
    for module, package in required.items():
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    if missing:
        logger.info('Installing missing dependencies: %s', ', '.join(missing))
        subprocess.check_call(
            [sys.executable, '-m', 'pip', 'install', '--quiet'] + missing,
        )
        logger.info('Dependencies installed.')
    else:
        logger.debug('All dependencies present: %s', ', '.join(required))
