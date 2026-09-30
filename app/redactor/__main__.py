# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Hafiz
import argparse

from .server import serve

ap = argparse.ArgumentParser(prog="redactor", description="Offline face redaction for video.")
ap.add_argument("--port", type=int, default=0, help="port to listen on (default: any free port)")
ap.add_argument("--no-browser", action="store_true", help="don't open the browser")
args = ap.parse_args()
serve(args.port, not args.no_browser)
