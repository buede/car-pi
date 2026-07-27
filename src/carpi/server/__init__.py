"""The local web server and its offline UI.

The portable unit has no internet. It serves its own UI over its own hotspot, and a
phone browser is the whole interface. Everything here is built for that: no external
hosts, no CDN, and a UI that loads from the Pi or not at all.
"""

from carpi.server.app import create_app
from carpi.server.vehicle import BusBusy, DirectProvider, SimulatedProvider, VehicleGateway

__all__ = [
    "BusBusy",
    "DirectProvider",
    "SimulatedProvider",
    "VehicleGateway",
    "create_app",
]
