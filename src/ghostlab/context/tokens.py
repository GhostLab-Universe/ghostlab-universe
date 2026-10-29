"""Deterministic, provider-independent token estimate.

ceil(UTF-8 bytes / 4) per text plus a fixed per-item overhead. This is a
conservative budgeting heuristic, not any provider's tokenizer; M01 reports the
provider's actual usage separately.
"""

ITEM_OVERHEAD_TOKENS = 8
BUNDLE_OVERHEAD_TOKENS = 32  # scope, schema and skill references


def estimate_tokens(text: str) -> int:
    return -(-len(text.encode("utf-8")) // 4)


def item_tokens(content: str) -> int:
    return estimate_tokens(content) + ITEM_OVERHEAD_TOKENS
