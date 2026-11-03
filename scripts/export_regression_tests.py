"""Export a runnable pytest module from a saved regression case.

Reads a ``ghostlab-diagnostic-regression/2`` case (plus its sibling capsule)
or a ``ghostlab-diagnostic-regression-ext/1`` dict (plus ``--evidence``
recorded evidence) and writes a standalone pytest module that replays the
case and locks the recorded expectation. Exit 0 with a summary, 2 on any
refused input. No model calls, no network.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ghostlab.replay.export_tests import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
