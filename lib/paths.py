"""Filesystem locations shared by the backend modules."""

import os

# The package lives in <project>/docucloak, so its parent is the project root.
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
