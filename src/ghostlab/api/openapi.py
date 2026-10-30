"""Generate the committed OpenAPI document.

    python -m ghostlab.api.openapi          # write src/ghostlab/api/openapi.v1.json
    python -m ghostlab.api.openapi --check  # exit 1 on drift
"""

import argparse
import json
import sys
from pathlib import Path

from .app import create_app

OPENAPI_PATH = Path(__file__).with_name("openapi.v1.json")


def document() -> str:
    return json.dumps(create_app().openapi(), indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def drifted() -> bool:
    if not OPENAPI_PATH.exists():
        return True
    return OPENAPI_PATH.read_bytes().replace(b"\r\n", b"\n") != document().encode()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    if parser.parse_args(argv).check:
        if drifted():
            print(f"{OPENAPI_PATH} is out of date; run python -m ghostlab.api.openapi")
            return 1
        return 0
    OPENAPI_PATH.write_bytes(document().encode())
    return 0


if __name__ == "__main__":
    sys.exit(main())
