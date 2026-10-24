"""Server logging: a rotating file, plus console output for interactive runs."""

import logging
import logging.handlers
import os
import sys

from .paths import APP_DIR

LOG_FILE = os.path.join(APP_DIR, 'server.log')
LOG_MAX_BYTES = 5 * 1024 * 1024  # rotate at 5 MB: server.log, server.log.1 ... server.log.3
LOG_BACKUPS = 3

class _RotatingLog(logging.handlers.RotatingFileHandler):
    """Rotating log that also moves stdout/stderr along when it rotates.

    server.sh sends the process's stdout/stderr to server.log, so without this,
    tracebacks and PDF library warnings would keep going to the renamed file.
    """

    def doRollover(self):
        super().doRollover()
        if self.stream is not None and not sys.stdout.isatty():
            for fd in (1, 2):
                os.dup2(self.stream.fileno(), fd)

_log_handlers = [_RotatingLog(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS)]
# server.sh redirects stdout into LOG_FILE, so only echo to stdout when it is an
# interactive terminal; otherwise every line would be written to the log twice.
if sys.stdout.isatty():
    _log_handlers.append(logging.StreamHandler(sys.stdout))
logging.basicConfig(
    level=logging.DEBUG,
    # threadName shows which request/worker thread wrote each line
    format='%(asctime)s %(levelname)s [%(threadName)s] %(message)s',
    handlers=_log_handlers,
)
logging.getLogger('PIL').setLevel(logging.INFO)  # PIL logs every PNG chunk at DEBUG
logger = logging.getLogger('docucloak')
