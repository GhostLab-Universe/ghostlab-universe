"""M09 integration tests import ``evals`` from the repository root.

The whole evaluation suite runs once per pytest session, in pytest's temporary directory.
Inference is a labeled fixture (httpx.MockTransport) throughout.
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ["LANGSMITH_TRACING"] = "false"


@pytest.fixture(scope="session")
def evaluated(tmp_path_factory):
    from evals.run import run_all
    out = tmp_path_factory.mktemp("evals")
    return out, run_all(out)
