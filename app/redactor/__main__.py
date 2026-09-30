# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Hafiz
"""Command line: `python -m redactor [--port N] [--no-browser]`."""
import argparse
import sys

from . import __version__
from .server import serve


def main(argv=None):
    ap = argparse.ArgumentParser(prog="redactor", description="Offline face redaction for video.")
    ap.add_argument("--port", type=int, default=0, help="port to listen on (default: any free port)")
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser")
    ap.add_argument("--version", action="version", version=f"Redactor {__version__}")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # never crash printing a file name the console can't show
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    serve(args.port, not args.no_browser)


if __name__ == "__main__":
    main()
