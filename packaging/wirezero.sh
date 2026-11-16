#!/bin/sh
# Launcher entry point for WireZero. Isolated mode (-I) ignores PYTHONPATH,
# user site-packages and the current directory; -B writes no bytecode.
exec /usr/bin/python3 -I -B /usr/lib/wirezero/run.py "$@"
