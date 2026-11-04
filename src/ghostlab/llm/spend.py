"""Persistent spend ledger with hard per-key caps (var/spend.json, git-ignored).

Every live model call is recorded: timestamp, key label (never the key),
provider, model, request id, input/output/reasoning/cached tokens, latency and
a USD estimate. Before every outbound HTTP attempt (retries included) the
worst-case cost of that attempt is checked against the key's cap; a call that
could cross it is refused before anything is sent.
"""

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

# USD per 1M tokens, Standard mode. Source: https://dev.meta.ai/docs/pricing-rate-limits
# fetched 2026-11-04. Reasoning tokens are a breakdown of output_tokens
# (output_tokens_details.reasoning_tokens), so output_tokens is what is billed.
PRICES_FETCHED = "2026-11-04"
PRICES = {
    "muse-spark-1.3": {"input": 1.25, "cached": 0.15, "output": 4.25},
    "muse-wonder-1.0": {"input": 4.00, "cached": 0.20, "output": 20.00},
}
# Confirmed by the owner 2026-11-04 (AGENTS.md). Glimmer is local and free.
CAPS = {"dev": 120.0, "public": 50.0, "glimmer": None}

DEFAULT_PATH = Path(__file__).resolve().parents[3] / "var" / "spend.json"


class SpendRefused(Exception):
    """A call that could exceed its key's cap. Raised before anything is sent."""


def usd(model: str, input_tokens=0, output_tokens=0, cached_tokens=0) -> float:
    price = PRICES.get(model)
    if price is None:
        return 0.0  # ponytail: only priced (Meta) models cost; local models are free.
    cached = min(cached_tokens or 0, input_tokens or 0)
    return ((input_tokens - cached) * price["input"] + cached * price["cached"]
            + (output_tokens or 0) * price["output"]) / 1_000_000


def worst_case(model: str, body_bytes: int, max_output_tokens: int) -> float:
    """Upper bound for one attempt: ~3 bytes/token is generous for JSON prompts."""
    return usd(model, input_tokens=body_bytes // 3 + 1, output_tokens=max_output_tokens)


class SpendLedger:
    def __init__(self, path: Path | str = DEFAULT_PATH, caps: dict | None = None):
        self.path = Path(path)
        self.caps = dict(CAPS if caps is None else caps)
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"prices_fetched": PRICES_FETCHED, "calls": []}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def spent(self, label: str) -> float:
        with self._lock:
            return sum(c["usd"] for c in self._load()["calls"] if c["key_label"] == label)

    def check(self, label: str, est_usd: float) -> None:
        if label not in self.caps:
            raise SpendRefused(f"unknown key label {label!r}")
        cap = self.caps[label]
        if cap is None:
            return
        spent = self.spent(label)
        if spent + est_usd > cap:
            raise SpendRefused(f"{label}: spent {spent:.4f} + worst case {est_usd:.4f}"
                               f" USD would exceed the {cap} USD cap")

    def record(self, *, label, provider, model, request_id, input_tokens, output_tokens,
               reasoning_tokens=None, cached_tokens=None, latency_ms=None, purpose=None) -> dict:
        row = {"at": datetime.now(UTC).isoformat(), "key_label": label, "provider": provider,
               "model": model, "request_id": request_id, "input_tokens": input_tokens,
               "output_tokens": output_tokens, "reasoning_tokens": reasoning_tokens,
               "cached_tokens": cached_tokens, "latency_ms": latency_ms, "purpose": purpose,
               "usd": usd(model, input_tokens or 0, output_tokens or 0, cached_tokens or 0)}
        with self._lock:
            data = self._load()
            data["calls"].append(row)
            data["totals"] = _totals(data["calls"])
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        return row

    def totals(self) -> dict:
        with self._lock:
            return _totals(self._load()["calls"])


def _totals(calls) -> dict:
    out = {}
    for c in calls:
        t = out.setdefault(c["key_label"], {"calls": 0, "usd": 0.0, "input_tokens": 0,
                                            "output_tokens": 0})
        t["calls"] += 1
        t["usd"] += c["usd"]
        t["input_tokens"] += c["input_tokens"] or 0
        t["output_tokens"] += c["output_tokens"] or 0
    for label, t in out.items():
        t["cap_usd"] = CAPS.get(label)
    return out
