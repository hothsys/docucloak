"""Locate Tesseract, including the folders Homebrew uses when Finder launches the app."""

import os
import shutil
import subprocess

from .logging_setup import logger

# Apps launched from Finder don't get the shell's PATH, so also look where Homebrew installs
TESSERACT_DIRS = ('/opt/homebrew/bin', '/usr/local/bin')
_NOT_CHECKED = object()
_tesseract_seen = _NOT_CHECKED  # last detection result, so only changes are logged

def _find_tesseract():
    """Path of the tesseract program, or None. Cheap enough to call per request."""
    path = shutil.which('tesseract')
    if path is None:
        for folder in TESSERACT_DIRS:
            candidate = os.path.join(folder, 'tesseract')
            if os.access(candidate, os.X_OK):
                return candidate
    return path

def _tesseract_status():
    """Detect Tesseract and log when it appears, disappears or changes."""
    global _tesseract_seen
    path = _find_tesseract()
    if path != _tesseract_seen:
        _tesseract_seen = path
        if path:
            try:
                version = subprocess.run([path, '--version'], capture_output=True, text=True, timeout=10)
                version = (version.stdout or version.stderr).splitlines()[0]
            except (OSError, subprocess.SubprocessError, IndexError):
                version = 'unknown version'
            logger.info('Tesseract found: %s (%s)', path, version)
        else:
            logger.warning('Tesseract not found on PATH or in %s; scanned PDFs and images cannot be redacted '
                           '(install with: brew install tesseract)', ', '.join(TESSERACT_DIRS))
    return {'tesseract': path is not None}
