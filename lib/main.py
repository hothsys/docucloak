"""Start the loopback-only DocuCloak web server."""

import os
from http.server import ThreadingHTTPServer

from . import config, logging_setup, tesseract, web
from .logging_setup import logger


def main():
    port = int(os.environ.get('PORT', 5903))
    # Allow overriding port via app.conf
    cfg_port = config._APP_CONFIG.get('PORT')
    if cfg_port:
        try:
            port = int(cfg_port)
        except ValueError:
            pass
    # Loopback only: the app reads and writes local files, so other devices on
    # the network must not be able to reach it
    server = ThreadingHTTPServer(('127.0.0.1', port), web.Handler)
    logger.info('%s running at http://localhost:%d (log file: %s)',
                config.APP_NAME, port, logging_setup.LOG_FILE)
    tesseract._tesseract_status()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info('Shutting down...')
        server.shutdown()


if __name__ == '__main__':
    main()
