#!/usr/bin/env python3
"""Entry point for the newsvoice guided setup.

Run it straight from a fresh clone, before anything is installed::

    python3 scripts/setup.py              # TUI when there is a terminal
    python3 scripts/setup.py --plain      # line mode
    python3 scripts/setup.py --check      # only inspect and validate
    python3 scripts/setup.py --help

Only the Python standard library is required, so the helper works before the
project virtualenv exists.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _main() -> int:
    """Put ``scripts/`` on the import path and run the wizard.

    Returns:
        Process exit status.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from setup_wizard.__main__ import main

    return main()


if __name__ == "__main__":
    raise SystemExit(_main())
