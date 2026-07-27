"""A simulated VAG car of the KWP2000 era, roughly a Passat B6.

Enough modules to exercise the whole path: a cluster whose odometer disagrees with the
engine's, a comfort module that can be coded, and an airbag controller that exists so
tests can prove the coding path refuses to touch it.

The measuring-block contents and local identifiers are invented. They are shaped like a
real car's -- the right services, the right refusals, the right module addresses -- but no
value here is claimed to be what a real Passat returns. The point is to exercise the
machinery, not to stand in for a definition.
"""

from __future__ import annotations

from carpi.sim.tp20 import SimulatedTp20Module

__all__ = ["passat_b6_modules"]

# Invented, but plausible in shape: VAG part numbers look like this.
_CLUSTER_ID = b"\x03\x1a\x80" + b"3C0920870A  KOMBIINSTRUMENT VDD "[:29]
_ENGINE_ID = b"\x03\x1a\x80" + b"03G906021KL  R4 2.0L TDI    G000"[:29]
_COMFORT_ID = b"\x03\x1a\x80" + b"3C0959433A  KOMFORTGERAET  H07 "[:29]


def passat_b6_modules() -> list[SimulatedTp20Module]:
    """The simulated module set. Fresh objects each call, so tests do not share state."""
    return [
        SimulatedTp20Module(
            logical_address=0x01,
            label="Engine",
            module_tx_id=0x740,
            identification=_ENGINE_ID,
            blocks={
                1: ((0x01, 0x40, 0x50), (0x05, 0x0A, 0xC8), (0x07, 0x64, 0x00), (0x25, 0x00, 0x2A)),
                2: ((0x21, 0x64, 0x32), (0x12, 0x20, 0x30), (0x23, 0x0A, 0x14), (0x25, 0x00, 0x01)),
            },
            # The engine module still holds the true distance.
            local_ids={0x22: (285_400).to_bytes(3, "big")},
            dtcs=((16486, 0x2F),),
        ),
        SimulatedTp20Module(
            logical_address=0x17,
            label="Instruments",
            module_tx_id=0x741,
            identification=_CLUSTER_ID,
            blocks={
                1: ((0x07, 0x64, 0x00), (0x05, 0x0A, 0xC8), (0x25, 0x00, 0x37), (0x36, 0x01, 0x2C)),
            },
            # Rewritten to a lower figure. On a real car this is the tampering signature:
            # the cluster is the number a buyer looks at, so it is the one that gets changed.
            local_ids={0x22: (145_000).to_bytes(3, "big")},
            dtcs=(),
        ),
        SimulatedTp20Module(
            logical_address=0x46,
            label="Central convenience",
            module_tx_id=0x742,
            identification=_COMFORT_ID,
            blocks={1: ((0x25, 0x00, 0x01), (0x25, 0x00, 0x00), (0x25, 0x00, 0x03))},
            # The coding value, and a login that permits changing it. This is the module a
            # comfort or lighting change would target on a real car.
            local_ids={0x00: bytes.fromhex("0A1B2C")},
            coding=bytes.fromhex("0A1B2C"),
            login_code=13861,
            dtcs=(),
        ),
        SimulatedTp20Module(
            logical_address=0x15,
            label="Airbags",
            module_tx_id=0x743,
            identification=b"\x03\x1a\x80" + b"3C0959655C  AIRBAG VW8R     013"[:29],
            local_ids={0x00: bytes.fromhex("000102")},
            coding=bytes.fromhex("000102"),
            # A login exists, so a test proving the refusal cannot be passing merely
            # because the module would have refused anyway.
            login_code=20103,
            dtcs=(),
        ),
    ]
