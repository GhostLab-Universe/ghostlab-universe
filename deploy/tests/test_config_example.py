"""Schema + safety checks for deploy/config.mumbai.json.example.

Run from the repo root with the repo interpreter:

    PYTHONPATH=src python -m pytest deploy/tests/ -q

The checks pin the example to the repo's own seams (never a copy of them):
loopback via local_dev.is_loopback, the live model via live.QUALIFIED_LIVE_MODEL,
and the storage layout via StoragePaths.
"""

import json
import re
from pathlib import Path

from ghostlab.api.live import QUALIFIED_LIVE_MODEL
from ghostlab.security.local_dev import is_loopback
from ghostlab.security.redaction import SECRET_ENV_NAMES
from ghostlab.storage.database import StoragePaths

EXAMPLE = Path(__file__).resolve().parents[1] / "config.mumbai.json.example"

_SECRET_VALUE = re.compile(
    r"(sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"xox[bap]-[A-Za-z0-9-]+)"
)


def _load():
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def _walk_strings(node, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk_strings(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _walk_strings(value, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, node


def test_example_is_valid_json_with_schema_marker():
    cfg = _load()
    assert cfg["schema"] == "ghostlab-mumbai-deploy/1"


def test_region_and_size_match_the_package():
    cfg = _load()
    assert cfg["region"] == "ap-south-1"
    assert cfg["instance_type"] == "t3.medium"
    assert cfg["root_volume_gb"] == 30
    assert cfg["backup"]["schedule"] == "hourly"
    assert cfg["backup"]["retention_days"] == 30


def test_api_binds_loopback_only():
    cfg = _load()
    assert is_loopback(cfg["api"]["bind_host"]), "bind_host must stay loopback"
    assert cfg["api"]["port"] == 8787


def test_live_model_is_the_qualified_one():
    cfg = _load()
    assert cfg["live"]["model"] == QUALIFIED_LIVE_MODEL
    assert cfg["live"]["enabled"] is False
    assert cfg["live"]["confirm_paid_calls"] is False


def test_auth_waits_for_pr22():
    cfg = _load()
    assert cfg["auth"]["mode"] == "PR-22-pending"
    assert "local_dev" not in cfg["auth"]["mode"]


def test_storage_root_uses_storage_paths_layout():
    cfg = _load()
    paths = StoragePaths(Path(cfg["storage_root"]))
    assert paths.app_db.name == "ghostlab.sqlite"
    assert paths.graph_db.name == "graph-checkpoints.sqlite"
    assert paths.artifacts.name == "artifacts"


def test_no_secrets_in_example():
    cfg = _load()
    for path, value in _walk_strings(cfg):
        leaf = path.rsplit(".", 1)[-1].upper()
        if leaf in SECRET_ENV_NAMES or leaf in {"SECRET", "TOKEN", "PASSWORD"}:
            assert value == "", f"{path} must stay empty, got a value"
        assert not _SECRET_VALUE.search(value), f"{path} looks like a credential"
