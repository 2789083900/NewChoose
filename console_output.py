#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Console output helpers shared by command-line entry points."""

import sys


def configure_utf8_output():
    """Prefer UTF-8 where the active text streams support reconfiguration."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            # Closed, detached, or host-managed streams may reject changes.
            continue
