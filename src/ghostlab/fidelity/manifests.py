"""Fidelity manifests: explicit, checkable claims about simulation fidelity.

Fidelity means (a) which faults the real service could exhibit and the
checked-in world models, and (b) whether the world's state transitions match
the real service's transitions for the declared tools.

A manifest never claims synthetic == production. A ``synthetic-only`` fault
exists only in the local world. ``boundary-real`` is reserved for proxy-backed
environments where the real upstream service executes the call, and the
classification rule enforces that: ``boundary-real`` requires a non-empty
``upstream`` service name plus a ``basis`` naming the forwarding mechanism.
Validation is pure: it never imports adapter code and never runs anything.
"""

import hashlib
import json
import os
import re
from pathlib import Path

FIDELITY_MANIFEST_VERSION = 1
MAX_FIDELITY_MANIFEST_BYTES = 262_144

_FIDELITY_KEYS = {"manifest_version", "env_id", "env_version", "adapter_ref",
                  "faults", "state_basis", "assessed_by", "assessed_at"}
_FAULT_KEYS = {"class", "basis", "upstream"}
_FAULT_CLASSES = ("boundary-real", "synthetic-only")
_STATE_BASES = ("authoritative-service", "synthetic-model")
# A boundary-real basis must name the forwarding mechanism: it must contain
# one of these (case-insensitive) so the claim stays mechanically checkable.
_FORWARDING_KEYWORDS = ("forward", "prox", "delegat", "relay", "gateway",
                        "upstream executes", "executes the call",
                        "pass-through", "passthrough", "pass through", "shim")

_ENV_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_ENV_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+*-]{0,63}$")
_HOOK_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ASSESSED_AT_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_URL_PATTERN = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://")
_WINDOWS_ABS_PATTERN = re.compile(r"^[A-Za-z]:[\\/]|^\\\\")


def _forbidden_value(text: str) -> str | None:
    if _URL_PATTERN.search(text):
        return "URL"
    if (text.startswith("/") or os.path.isabs(text)
            or _WINDOWS_ABS_PATTERN.match(text)):
        return "absolute path"
    if "\x00" in text:
        return "NUL byte"
    return None


def _scan(node, where: str) -> None:
    if isinstance(node, str):
        hit = _forbidden_value(node)
        if hit:
            raise ValueError(f"fidelity manifest {where} must not contain a {hit}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _scan(item, f"{where}[{index}]")
    elif isinstance(node, dict):
        for key, item in node.items():
            _scan(item, f"{where}.{key}")


def canonical_digest(manifest: dict) -> str:
    text = json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(text.encode()).hexdigest()


def validate_fidelity_manifest(obj) -> dict:
    """Validate a parsed fidelity manifest. Pure: no imports, no execution."""
    if type(obj) is not dict or set(obj) != _FIDELITY_KEYS:
        raise ValueError("fidelity manifest must carry exactly the v1 fields")
    if (type(obj["manifest_version"]) is not int
            or obj["manifest_version"] != FIDELITY_MANIFEST_VERSION):
        raise ValueError("unsupported fidelity manifest_version")
    if type(obj["env_id"]) is not str or not _ENV_ID_PATTERN.match(obj["env_id"]):
        raise ValueError("invalid fidelity env_id")
    if (type(obj["env_version"]) is not str
            or not _ENV_VERSION_PATTERN.match(obj["env_version"])):
        raise ValueError("invalid fidelity env_version")
    if (type(obj["adapter_ref"]) is not str or not obj["adapter_ref"]
            or len(obj["adapter_ref"]) > 300):
        raise ValueError("invalid fidelity adapter_ref")
    faults = obj["faults"]
    if type(faults) is not dict or len(faults) > 16:
        raise ValueError("fidelity faults must map at most 16 hooks")
    for hook, entry in faults.items():
        if type(hook) is not str or not _HOOK_PATTERN.match(hook):
            raise ValueError(f"invalid fidelity fault hook '{hook}'")
        if type(entry) is not dict or set(entry) != _FAULT_KEYS:
            raise ValueError(
                f"fidelity fault '{hook}' needs exactly class, basis, upstream")
        cls, basis, upstream = entry["class"], entry["basis"], entry["upstream"]
        if cls not in _FAULT_CLASSES:
            raise ValueError(f"fidelity fault '{hook}' has unknown class '{cls}'")
        if type(basis) is not str or not basis or len(basis) > 300:
            raise ValueError(
                f"fidelity fault '{hook}' basis must be 1-300 characters")
        if upstream is not None and (type(upstream) is not str or not upstream
                                     or len(upstream) > 500):
            raise ValueError(
                f"fidelity fault '{hook}' upstream must be null or a service name")
        if cls == "boundary-real":
            if upstream is None or not upstream:
                raise ValueError(
                    f"fidelity fault '{hook}': boundary-real requires a "
                    "non-empty upstream service name")
            if not any(word in basis.lower() for word in _FORWARDING_KEYWORDS):
                raise ValueError(
                    f"fidelity fault '{hook}': boundary-real basis must name "
                    "the forwarding mechanism")
    if obj["state_basis"] not in _STATE_BASES:
        raise ValueError("invalid fidelity state_basis")
    if (type(obj["assessed_by"]) is not str or not obj["assessed_by"]
            or len(obj["assessed_by"]) > 200):
        raise ValueError("invalid fidelity assessed_by")
    if (type(obj["assessed_at"]) is not str
            or not _ASSESSED_AT_PATTERN.match(obj["assessed_at"])):
        raise ValueError("invalid fidelity assessed_at (want YYYY-MM-DDTHH:MM:SSZ)")
    _scan(obj, "manifest")
    return json.loads(json.dumps(obj))


def validate_fidelity_manifest_bytes(data: bytes) -> dict:
    """Validate raw fidelity-manifest bytes under the size bound. Pure."""
    if type(data) is not bytes:
        raise ValueError("fidelity manifest input must be bytes")
    if len(data) > MAX_FIDELITY_MANIFEST_BYTES:
        raise ValueError("fidelity manifest exceeds the size bound")
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError("fidelity manifest is not valid JSON") from error
    return validate_fidelity_manifest(obj)


class FidelityStore:
    """JSON-file-backed store: one digest-sealed file per env_id.

    Each file holds ``{"digest": ..., "manifest": ...}`` where the digest is
    the canonical digest of the manifest. ``load`` recomputes it and raises on
    any mismatch, so post-save edits are detected as tampering.
    """

    def __init__(self, directory: str | Path) -> None:
        self._directory = Path(directory)
        self._directory.mkdir(parents=True, exist_ok=True)

    def _path(self, env_id: str) -> Path:
        if type(env_id) is not str or not _ENV_ID_PATTERN.match(env_id):
            raise ValueError(f"invalid env_id '{env_id}'")
        return self._directory / f"{env_id}.json"

    def save(self, manifest: dict) -> dict:
        validated = validate_fidelity_manifest(manifest)
        digest = canonical_digest(validated)
        payload = {"digest": digest, "manifest": validated}
        text = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False)
        self._path(validated["env_id"]).write_text(text, encoding="utf-8")
        return {"env_id": validated["env_id"], "digest": digest}

    def load(self, env_id: str) -> dict:
        path = self._path(env_id)
        if not path.exists():
            raise ValueError(f"unknown fidelity manifest for env '{env_id}'")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as error:
            raise ValueError(
                f"fidelity manifest for env '{env_id}' is not valid JSON"
            ) from error
        if (type(payload) is not dict or set(payload) != {"digest", "manifest"}
                or type(payload["digest"]) is not str):
            raise ValueError(
                f"fidelity manifest file for env '{env_id}' has a bad envelope")
        manifest = validate_fidelity_manifest(payload["manifest"])
        if canonical_digest(manifest) != payload["digest"]:
            raise ValueError(
                f"fidelity manifest for env '{env_id}' fails digest check: "
                "the file was modified after save (tamper detected)")
        if manifest["env_id"] != env_id:
            raise ValueError(
                f"fidelity manifest env_id '{manifest['env_id']}' does not "
                f"match file '{env_id}'")
        return manifest

    def list(self, env_id: str | None = None) -> list:
        """List stored manifests (all, or one env_id) as env_id/digest rows."""
        if env_id is not None:
            manifest = self.load(env_id)
            return [{"env_id": env_id, "digest": canonical_digest(manifest)}]
        rows = []
        for path in sorted(self._directory.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (type(payload) is dict and type(payload.get("digest")) is str
                    and type(payload.get("manifest")) is dict
                    and type(payload["manifest"].get("env_id")) is str):
                rows.append({"env_id": payload["manifest"]["env_id"],
                             "digest": payload["digest"]})
        rows.sort(key=lambda row: row["env_id"])
        return rows


REFUND_FIDELITY_MANIFEST = {
    "manifest_version": 1,
    "env_id": "refund-env",
    "env_version": "refund-env/1",
    "adapter_ref": "ghostlab.envs.adapters:RefundEnvAdapter",
    "faults": {
        "timeout_after_commit": {
            "class": "synthetic-only",
            "basis": "World commits the refund row then raises PaymentTimeout; "
                     "models at-least-once retry. No upstream executes.",
            "upstream": None,
        },
    },
    "state_basis": "synthetic-model",
    "assessed_by": "ghostlab-track-e3",
    "assessed_at": "2026-11-02T00:00:00Z",
}

BOOKING_FIDELITY_MANIFEST = {
    "manifest_version": 1,
    "env_id": "booking-env",
    "env_version": "booking-env/1",
    "adapter_ref": "examples.equipment_booking.env_adapter:BookingEnvAdapter",
    "faults": {
        "timeout_after_commit": {
            "class": "synthetic-only",
            "basis": "World commits the reservation then raises BookingTimeout; "
                     "models at-least-once retry. No upstream executes.",
            "upstream": None,
        },
    },
    "state_basis": "synthetic-model",
    "assessed_by": "ghostlab-track-e3",
    "assessed_at": "2026-11-02T00:00:00Z",
}

# Documented template for a proxy-backed environment. This is an example of a
# well-formed boundary-real claim, not an assessment of any real service: to
# use it, replace env_id/env_version/adapter_ref/upstream/assessed_by with the
# operator's own values and record fresh assessed_at evidence.
PROXY_FIDELITY_TEMPLATE = {
    "manifest_version": 1,
    "env_id": "proxy-env-template",
    "env_version": "proxy-env/0-template",
    "adapter_ref": "operator.module:ProxyAdapter",
    "faults": {
        "timeout_after_commit": {
            "class": "boundary-real",
            "basis": "Adapter forwards each call to the upstream service over "
                     "the operator proxy; upstream executes the call.",
            "upstream": "operator-run upstream reservation service",
        },
    },
    "state_basis": "authoritative-service",
    "assessed_by": "template-operator",
    "assessed_at": "2026-11-02T00:00:00Z",
}

ASSESSED_MANIFESTS = (REFUND_FIDELITY_MANIFEST, BOOKING_FIDELITY_MANIFEST)

# Assessed boundary-real claim for the loopback proxy example. The fault
# MECHANISM is genuinely boundary-executed (real HTTP forward, then a dropped
# response / an unforwarded 403), while the upstream itself is the checked-in
# synthetic booking service — the upstream strings say so explicitly.
PROXY_BOOKING_FIDELITY_MANIFEST = {
    "manifest_version": 1,
    "env_id": "booking-proxy-env",
    "env_version": "booking-proxy-env/1",
    "adapter_ref": "examples.booking_service.env_adapter:ProxyEnvAdapter",
    "faults": {
        "timeout_after_commit": {
            "class": "boundary-real",
            "basis": "Proxy forwards the booking POST upstream (commit "
                     "executes there), then answers 504 without the "
                     "upstream 200. Fires once, then disarms.",
            "upstream": "checked-in synthetic booking service on loopback "
                        "HTTP (examples.booking_service.service)",
        },
        "permission_revoked": {
            "class": "boundary-real",
            "basis": "Proxy answers 403 without forwarding mutating POSTs "
                     "while revoked; reads keep forwarding.",
            "upstream": "checked-in synthetic booking service on loopback "
                        "HTTP (examples.booking_service.service)",
        },
    },
    "state_basis": "authoritative-service",
    "assessed_by": "ghostlab-integration-owner",
    "assessed_at": "2026-11-02T00:00:00Z",
}

for _shipped in (*ASSESSED_MANIFESTS, PROXY_FIDELITY_TEMPLATE,
                 PROXY_BOOKING_FIDELITY_MANIFEST):
    validate_fidelity_manifest(_shipped)
del _shipped
