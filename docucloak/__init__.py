"""DocuCloak backend package."""

from . import logging_setup  # noqa: F401  (configures logging when the package is imported)
from .dependencies import _ensure_dependencies

_ensure_dependencies()
