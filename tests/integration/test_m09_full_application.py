"""Full-application acceptance. BLOCKED until M07 and M08 exist.

These are placeholders, not results. Each test names what it must prove once the API and
interface are integrated. They stay skipped, so nothing here counts as passing evidence:
pytest reports them as skipped, and the M09 report lists them as a remaining dependency.
"""

import pytest

M07 = "Blocked on M07: no API exists on this base"
M08 = "Blocked on M08: no interface exists on this base"


@pytest.mark.skip(reason=M07)
def test_api_session_derives_identity_and_cannot_read_another_project():
    """Cross-project campaign, approval, memory and artifact reads through real routes."""


@pytest.mark.skip(reason=M07)
def test_refund_campaign_end_to_end_through_the_api():
    """Start, stream status, approve, resume, download the capsule, replay."""


@pytest.mark.skip(reason=M07)
def test_journey_campaign_end_to_end_through_the_api():
    """The same path for group-journey/1, including the guarded repair."""


@pytest.mark.skip(reason=M07)
def test_api_errors_and_logs_hold_no_secret():
    """Scan API responses and logs for the fixture credential."""


@pytest.mark.skip(reason=M08)
def test_interface_shows_reality_diff_and_approval_from_real_records():
    """The UI renders stored evidence and sends a real approval decision."""


@pytest.mark.skip(reason=M08)
def test_interface_never_receives_a_provider_key():
    """Browser payloads and bundles hold no credential."""
