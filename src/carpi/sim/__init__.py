"""A virtual ECU, so development and CI need neither a car nor CAN hardware.

The simulator answers real OBD-II requests over a real ISO-TP stack on an in-process
virtual CAN bus. Everything above the transport layer is therefore exercised exactly
as it would be in a driveway.

Its encoders in :mod:`carpi.sim.encode` are written from the standard's own
definitions rather than by inverting the decode formulas in the definition database.
That is deliberate: an encoder built as the algebraic inverse of a decoder agrees with
it even when both are wrong, and the round-trip test would pass while the tool
reported nonsense to a buyer.
"""

from carpi.sim.ecu import SimulatedVehicle, VirtualEcu
from carpi.sim.scenarios import SCENARIOS, EcuSpec, Scenario, get_scenario

__all__ = [
    "SCENARIOS",
    "EcuSpec",
    "Scenario",
    "SimulatedVehicle",
    "VirtualEcu",
    "get_scenario",
]
