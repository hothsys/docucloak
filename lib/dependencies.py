"""Install the exact runtime packages from requirements.txt on first run."""

import os
import re
import subprocess
import sys

from . import config
from .logging_setup import logger

# Map PyPI package name -> importable module name
_MODULE_MAP = {
    'PyMuPDF': 'pymupdf',
    'Pillow': 'PIL',
    'pytesseract': 'pytesseract',
}

def _parse_requirements():
    """Parse requirements.txt and return list of (module_name, pinned_spec)."""
    req_path = os.path.join(config.APP_DIR, 'requirements.txt')
    specs = []
    try:
        with open(req_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                # Expect format: Package==version
                m = re.match(r'^([A-Za-z0-9_.-]+)==(.+)$', line)
                if m:
                    pkg, version = m.groups()
                    module = _MODULE_MAP.get(pkg, pkg.lower())
                    specs.append((module, f'{pkg}=={version}'))
    except OSError:
        logger.debug('requirements.txt not found at %s', req_path)
    return specs

def _ensure_dependencies():
    specs = _parse_requirements()
    if not specs:
        logger.debug('No requirements to check')
        return
    missing = []
    for module, spec in specs:
        try:
            __import__(module)
        except ImportError:
            missing.append(spec)
    if missing:
        logger.info('Installing missing dependencies: %s', ', '.join(missing))
        subprocess.check_call(
            [sys.executable, '-m', 'pip', 'install', '--quiet'] + missing,
        )
        logger.info('Dependencies installed.')
    else:
        logger.debug('All dependencies present: %s', ', '.join(s for _, s in specs))

_ensure_dependencies()