"""Guided TUI setup wizard for newsvoice.

Configures the three ``.env`` files (``deploy/``, ``apps/news/``,
``apps/gateway/``) and the Python/Docker dependencies, with a step by step
assistant that explains every option, generates secrets and validates the
result.

The package is standard library only on purpose: it has to run *before* any
project virtualenv exists, straight from the system interpreter.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "1.0.0"
