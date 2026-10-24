"""Application paths and settings loaded from app.conf."""

import os

from . import logging_setup, paths

APP_DIR = paths.APP_DIR
logger = logging_setup.logger


def _read_app_config():
    cfg = {}
    conf_path = os.path.join(APP_DIR, 'app.conf')
    try:
        with open(conf_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, val = line.split('=', 1)
                cfg[key.strip()] = val.strip().strip("\"'")
    except OSError as e:
        logger.debug('No app config loaded from %s: %s', conf_path, e)
        return cfg
    logger.debug('Loaded app config from %s: %r', conf_path, cfg)
    return cfg


_APP_CONFIG = _read_app_config()


def _code_fingerprint():
    """Hash of the backend files on disk. Compared with the one taken at startup, it
    tells the page when the running server is older than the files (updated but not
    restarted)."""
    import hashlib
    base = os.path.dirname(os.path.abspath(__file__))
    try:
        names = sorted(name for name in os.listdir(base) if name.endswith('.py'))
    except OSError:
        return None
    digest = hashlib.sha256()
    for name in names:
        try:
            with open(os.path.join(base, name), 'rb') as f:
                digest.update(f.read())
        except OSError:
            continue
    return digest.hexdigest()


_STARTED_FINGERPRINT = _code_fingerprint()
APP_NAME = _APP_CONFIG.get('APP_NAME') or os.path.basename(APP_DIR)
APP_TITLE = _APP_CONFIG.get('APP_TITLE') or APP_NAME


def _config_int(key, default):
    try:
        return max(1, int(_APP_CONFIG.get(key) or default))
    except ValueError:
        return default
