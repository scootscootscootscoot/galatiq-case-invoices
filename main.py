#!/usr/bin/env python3
"""Entry point for the Acme accounts-payable pipeline.

python main.py --invoice_path=data/invoices/invoice_1001.txt
"""

from __future__ import annotations

import sys

from acme_ap.cli import app

if __name__ == "__main__":
    # A bare `--invoice_path=...` with no subcommand means "run", which is the
    # invocation the case specifies. Keep it working without making the other
    # subcommands unreachable.
    if len(sys.argv) > 1 and sys.argv[1].startswith("-"):
        sys.argv.insert(1, "run")
    app()
