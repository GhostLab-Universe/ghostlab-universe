"""Project-owned plain-text evidence enters context as untrusted observations.

No URL fetching, executable formats, HTML rendering or document-upload route. The
artifact store performs ownership and digest checks before text reaches the model.
"""

from ghostlab.context.compiler import Observation
from ghostlab.contracts import Ref
from ghostlab.policy import PolicyDenied, authorize_capability
from .redaction import Redactor

MAX_DOCUMENT_BYTES = 32_000


def document_observation(scope, artifacts, artifact_id: str, *, observed_at: str,
                         redactor: Redactor | None = None) -> Observation:
    decision = authorize_capability(scope, "read_evidence", project_id=scope.project_id)
    if not decision.allowed:
        raise PolicyDenied(decision)
    view = artifacts.for_project(scope)
    ref = view.ref(artifact_id)
    if ref.media_type != "text/plain":
        raise ValueError("only UTF-8 plain-text evidence is supported")
    text = view.read(ref, max_bytes=MAX_DOCUMENT_BYTES).decode("utf-8", errors="strict")
    if not text.strip() or "\x00" in text:
        raise ValueError("invalid plain-text evidence")
    text = (redactor or Redactor()).text(text)
    source = Ref(kind="artifact", id=ref.artifact_id, sha256=ref.sha256)
    return Observation(item_id="document-" + ref.sha256[:24], content=text,
                       source_ref=source, artifact=source, observed_at=observed_at)
