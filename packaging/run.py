"""Entry point used by the launcher. Run with `python3 -I -B` (isolated mode):
no PYTHONPATH, no user site-packages, no current directory on sys.path."""
import sys

sys.path.insert(0, "/usr/lib/wirezero")

from wirezero.main import main  # noqa: E402

sys.exit(main())
