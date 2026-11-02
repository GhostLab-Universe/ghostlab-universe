"""External equipment-booking demo (Track B, outside core worlds)."""

from .agents import run_corrected, run_naive
from .checks import has_duplicate, verify_equipment
from .world import BookingTimeout, EquipmentFixture, EquipmentScenario, EquipmentWorld

__all__ = [
    "BookingTimeout",
    "EquipmentFixture",
    "EquipmentScenario",
    "EquipmentWorld",
    "has_duplicate",
    "run_corrected",
    "run_naive",
    "verify_equipment",
]
