"""Comparison metadata: diffs between resolved configs.

Compares the meaningful config surface (pins, effective values and the policy
version) and skips record identity (resolved id, timestamps, digests), so two
independent resolutions of the same requested config compare equal.
"""

from copy import deepcopy

MAX_DIFFS = 200
_SKIP_KEYS = {"resolved_config_id", "resolved_at", "digest"}


def _summarize(value):
    if isinstance(value, dict):
        return {k: _summarize(v) for k, v in value.items() if k not in _SKIP_KEYS}
    if isinstance(value, list):
        return [_summarize(v) for v in value]
    return value


def _walk(path: str, left, right, diffs: list) -> None:
    if len(diffs) >= MAX_DIFFS:
        return
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(set(left) | set(right)):
            if key in _SKIP_KEYS:
                continue
            child = f"{path}.{key}" if path else str(key)
            if key not in left:
                diffs.append({"path": child, "left": None,
                              "right": deepcopy(right[key])})
            elif key not in right:
                diffs.append({"path": child, "left": deepcopy(left[key]),
                              "right": None})
            else:
                _walk(child, left[key], right[key], diffs)
    elif isinstance(left, list) and isinstance(right, list):
        if left != right:
            for index in range(max(len(left), len(right))):
                if len(diffs) >= MAX_DIFFS:
                    return
                child = f"{path}[{index}]"
                if index >= len(left):
                    diffs.append({"path": child, "left": None,
                                  "right": deepcopy(right[index])})
                elif index >= len(right):
                    diffs.append({"path": child, "left": deepcopy(left[index]),
                                  "right": None})
                else:
                    _walk(child, left[index], right[index], diffs)
    elif left != right or type(left) is not type(right):
        diffs.append({"path": path, "left": deepcopy(left),
                      "right": deepcopy(right)})


def diff_resolved(left: dict, right: dict) -> dict:
    """Diff two resolved bodies; returns stable comparison metadata."""
    if type(left) is not dict or type(right) is not dict:
        raise ValueError("diff_resolved needs two resolved bodies")
    diffs: list = []
    for key in ("pins", "effective"):
        _walk(key, _summarize(left.get(key)), _summarize(right.get(key)),
              diffs)
    _walk("provenance.policy_version",
          (left.get("provenance") or {}).get("policy_version"),
          (right.get("provenance") or {}).get("policy_version"), diffs)
    truncated = len(diffs) >= MAX_DIFFS
    return {"left_id": left.get("resolved_config_id"),
            "right_id": right.get("resolved_config_id"),
            "left_digest": left.get("digest"),
            "right_digest": right.get("digest"),
            "equal": not diffs,
            "diff_count": len(diffs),
            "truncated": truncated,
            "diffs": diffs}
