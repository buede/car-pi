"""Diagnostic trouble code encoding, per SAE J2012 / ISO 15031-6.

A DTC is two bytes on the wire and five characters to a human:

    bits 15..14   letter    00=P powertrain, 01=C chassis, 10=B body, 11=U network
    bits 13..12   digit 1   0..3
    bits 11..8    digit 2   0..F
    bits  7..4    digit 3   0..F
    bits  3..0    digit 4   0..F

So ``0x0143`` is ``P0143``. The all-zero value is padding, not a fault code.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "DtcCountMismatch",
    "DtcMeaning",
    "decode_dtc",
    "describe_dtc",
    "encode_dtc",
    "parse_dtc_response",
]

_LETTERS = "PCBU"

_SYSTEMS = {
    "P": "powertrain -- engine, transmission and emissions",
    "C": "chassis -- braking, steering and suspension",
    "B": "body -- interior and comfort electronics",
    "U": "network -- modules not talking to each other properly",
}

# Second character. J2012 splits the code space between codes every manufacturer must
# use identically and codes each manufacturer defines for itself. Which one a code is
# decides whether a generic description can exist for it at all, so it is the single most
# useful thing to be able to tell somebody about an unfamiliar code.
_STANDARDISED = {0: True, 1: False, 2: True, 3: False}

# Third character, for powertrain codes only. From the J2012 subsystem allocation. Broad
# on purpose: this says which part of the car to look at, and nothing more precise than
# the standard actually promises.
_P_SUBSYSTEMS = {
    "0": "fuel and air metering, plus auxiliary emission controls",
    "1": "fuel and air metering",
    "2": "fuel and air metering -- injector circuit",
    "3": "ignition system or misfire",
    "4": "auxiliary emission controls",
    "5": "vehicle speed control, idle control and auxiliary inputs",
    "6": "computer output circuits",
    "7": "transmission",
    "8": "transmission",
    "9": "transmission or control module",
    "A": "hybrid propulsion",
    "B": "hybrid propulsion",
    "C": "hybrid propulsion",
}


class DtcCountMismatch(ValueError):
    """The ECU's DTC count byte disagreed with the codes it then listed.

    Carries the codes so a caller can log the anomaly and still use them.
    """

    def __init__(self, declared: int, codes: list[str]) -> None:
        self.declared = declared
        self.codes = codes
        super().__init__(f"ECU declared {declared} DTCs but listed {len(codes)}: {codes}")


# Response service IDs for the three DTC-reading modes. Kept here so callers name
# the mode rather than a magic number.
MODE_STORED = 0x43
MODE_PENDING = 0x47
MODE_PERMANENT = 0x4A


def decode_dtc(high: int, low: int) -> str | None:
    """Decode one two-byte DTC. Returns ``None`` for the ``0x0000`` padding value."""
    if high == 0 and low == 0:
        return None
    letter = _LETTERS[(high >> 6) & 0x03]
    return f"{letter}{(high >> 4) & 0x03}{high & 0x0F:X}{(low >> 4) & 0x0F:X}{low & 0x0F:X}"


def encode_dtc(code: str) -> tuple[int, int]:
    """Encode ``"P0143"`` back to its two wire bytes. Inverse of :func:`decode_dtc`."""
    text = code.strip().upper()
    if len(text) != 5:
        raise ValueError(f"DTC must be 5 characters, got {code!r}")
    letter, digits = text[0], text[1:]
    if letter not in _LETTERS:
        raise ValueError(f"DTC must start with one of {_LETTERS}, got {code!r}")
    try:
        d1, d2, d3, d4 = (int(c, 16) for c in digits)
    except ValueError:
        raise ValueError(f"DTC digits must be hexadecimal, got {code!r}") from None
    if d1 > 3:
        raise ValueError(f"DTC first digit must be 0-3, got {code!r}")
    high = (_LETTERS.index(letter) << 6) | (d1 << 4) | d2
    return high, (d3 << 4) | d4


@dataclass(frozen=True)
class DtcMeaning:
    """What the structure of a code says, without looking anything up.

    This is not a fault description. It is the part of a code's meaning that SAE J2012
    fixes in the standard, which is available for every code including ones nobody has a
    description for. For somebody holding a code they have never seen, the useful facts
    are which part of the car it concerns and whether a generic description can exist for
    it at all -- and both are readable straight off the characters.

    No guessing happens here. A code whose exact meaning is manufacturer-defined says so,
    rather than being given a plausible generic description that would belong to a
    different fault on a different make.
    """

    code: str
    system: str
    standardised: bool
    subsystem: str | None = None

    @property
    def summary(self) -> str:
        parts = [self.system]
        if self.subsystem:
            parts.append(self.subsystem)
        if not self.standardised:
            parts.append(
                "manufacturer-specific, so its exact meaning varies by make and a "
                "generic code list will not have it"
            )
        return "; ".join(parts)


def describe_dtc(code: str) -> DtcMeaning | None:
    """Interpret a code's structure. ``None`` if it is not a well-formed DTC.

    Deliberately shallow. Anything beyond this needs a per-code description, and a wrong
    one of those does not fail loudly -- it sends somebody to replace the wrong part.
    """
    text = code.strip().upper()
    # Codes may arrive with a UDS failure-type byte appended, as in "P0420-08".
    text = text.split("-", 1)[0]
    if len(text) != 5 or text[0] not in _LETTERS:
        return None
    try:
        first = int(text[1], 16)
        int(text[2:], 16)
    except ValueError:
        return None
    if first not in _STANDARDISED:
        return None

    standardised = _STANDARDISED[first]
    # The subsystem allocation binds manufacturers only for the codes they are required to
    # use identically. Reading it off a manufacturer-specific code would be inventing a
    # meaning the standard does not give it, so those report the system and nothing more.
    subsystem = _P_SUBSYSTEMS.get(text[2]) if text[0] == "P" and standardised else None
    return DtcMeaning(
        code=text,
        system=_SYSTEMS[text[0]],
        standardised=standardised,
        subsystem=subsystem,
    )


def parse_dtc_response(payload: bytes) -> list[str]:
    """Parse a Mode 03, 07 or 0A response into a list of DTC strings.

    *payload* must start with the response service ID (``0x43``, ``0x47`` or
    ``0x4A``) and be exactly as long as the ISO-TP layer reported -- do not pass
    frame padding, which would corrupt the length parity check below.

    ISO 15765-4 has the ECU send a DTC-count byte before the codes, but not every
    ECU in the wild does. The two layouts are distinguishable by parity, because
    codes are always two bytes: ``count + 2n`` is odd, bare ``2n`` is even.
    """
    if not payload:
        raise ValueError("empty DTC response")
    if payload[0] not in (MODE_STORED, MODE_PENDING, MODE_PERMANENT):
        raise ValueError(f"not a DTC response: service ID 0x{payload[0]:02X}")

    body = payload[1:]
    declared_count: int | None = None
    if len(body) % 2 == 1:
        declared_count = body[0]
        body = body[1:]

    codes: list[str] = []
    for index in range(0, len(body) - 1, 2):
        code = decode_dtc(body[index], body[index + 1])
        if code is not None:
            codes.append(code)

    # A mismatch is worth surfacing rather than hiding: it usually means the ECU
    # padded the response, but it can also mean something is filtering the bus.
    if declared_count is not None and declared_count != len(codes):
        raise DtcCountMismatch(declared=declared_count, codes=codes)

    return codes
