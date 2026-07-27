"""The write-capable KWP2000 client.

This is the counterpart to :class:`carpi.core.protocol.kwp2000.KwpClient`, which refuses
to emit anything that could change a vehicle. Here the write services exist -- and they
are the *only* reason this file is in a separate package.

Two services beyond the read-only set:

``0x27 SecurityAccess``
    VAG's login. On the KWP2000 era it is a five-digit code the module compares directly,
    which is why coding is achievable here at all. On a modern car the equivalent is a
    cryptographic exchange with a token from the manufacturer's server, and no amount of
    cleverness substitutes for it.

``0x3B WriteDataByLocalIdentifier``
    Writes a coding value or an adaptation channel.

Everything here assumes the caller has already been through
:mod:`carpi.coding.plan`, which is what actually enforces the safety rules. Using this
class directly bypasses the backup, the diff and the confirmation, so don't.
"""

from __future__ import annotations

import logging

from carpi.core.protocol.kwp2000 import (
    SERVICE_READ_LOCAL_ID,
    KwpClient,
    KwpError,
    KwpNegativeResponse,
)
from carpi.core.transport.base import Channel

__all__ = ["CodingSession", "LoginFailed"]

log = logging.getLogger(__name__)

SERVICE_SECURITY_ACCESS = 0x27
SERVICE_WRITE_LOCAL_ID = 0x3B

# VAG uses sub-function 0x03 for a login that carries the code directly, rather than the
# seed-then-key pair the standard describes.
LOGIN_SUBFUNCTION = 0x03

# Coding and adaptation local identifiers that hold a fixed meaning across VAG modules.
LID_CODING = 0x00
LID_ADAPTATION = 0x0A


class LoginFailed(KwpError):
    """The module rejected the login code."""


class CodingSession:
    """A logged-in, write-capable conversation with one module.

    Reads go through the ordinary read-only client, so there is exactly one
    implementation of reading and it is the audited one.
    """

    def __init__(self, channel: Channel, *, timeout: float = 2.0) -> None:
        self._channel = channel
        self._timeout = timeout
        self._reader = KwpClient(channel, timeout=timeout)
        self._logged_in = False

    @property
    def address(self) -> object:
        return self._channel.address

    @property
    def reader(self) -> KwpClient:
        """The read-only client, for reading current values before a write."""
        return self._reader

    @property
    def logged_in(self) -> bool:
        return self._logged_in

    def _exchange(self, request: bytes) -> bytes:
        reply = self._channel.request(request, timeout=self._timeout)
        if not reply:
            raise KwpError(f"empty reply to {request.hex(' ')}")
        if reply[0] == 0x7F:
            if len(reply) < 3:
                raise KwpError(f"truncated negative response: {reply.hex(' ')}")
            raise KwpNegativeResponse(service=reply[1], nrc=reply[2])
        expected = request[0] + 0x40
        if reply[0] != expected:
            raise KwpError(
                f"expected 0x{expected:02X} in reply to 0x{request[0]:02X}, got 0x{reply[0]:02X}"
            )
        return reply

    def login(self, code: int) -> None:
        """Send a five-digit login code. Raises :class:`LoginFailed` if refused.

        Modules count failed attempts and stop accepting them for a while, so this does
        not retry. Guessing is not a strategy: the code for a given module and function is
        published in the community documentation for the platform.
        """
        if not 0 <= code <= 0xFFFF:
            raise LoginFailed(f"login code {code} is out of range; VAG codes fit in two bytes")
        request = bytes([SERVICE_SECURITY_ACCESS, LOGIN_SUBFUNCTION, code >> 8, code & 0xFF])
        try:
            self._exchange(request)
        except KwpNegativeResponse as exc:
            raise LoginFailed(
                f"{self.address} rejected login {code:05d}: {exc}. Modules limit repeated "
                f"attempts, so check the code against the documentation for this platform "
                f"rather than trying another."
            ) from exc
        self._logged_in = True
        log.info("logged in to %s", self.address)

    def read_raw(self, identifier: int) -> bytes:
        """Read a local identifier's current bytes, via the read-only client."""
        return self._reader.read_local_identifier(identifier)

    def write_raw(self, identifier: int, payload: bytes) -> bytes:
        """Write bytes to a local identifier.

        The one call in car-pi that changes a car. Requires a successful login first --
        not because the module would necessarily refuse otherwise, but because reaching
        here without one means the caller skipped the plan step that enforces every other
        safety rule.
        """
        if not self._logged_in:
            raise KwpError(
                "refusing to write without a login. Go through carpi.coding.plan, which "
                "handles the backup, the diff and the confirmation."
            )
        if not payload:
            raise KwpError("refusing to write an empty value")

        request = bytes([SERVICE_WRITE_LOCAL_ID, identifier]) + bytes(payload)
        log.warning(
            "WRITING to %s identifier 0x%02X: %s",
            self.address,
            identifier,
            payload.hex(" "),
        )
        return self._exchange(request)

    def verify(self, identifier: int, expected: bytes) -> bool:
        """Read the value back and confirm it took.

        A positive response only means the module accepted the request. Reading back is
        what establishes the value actually changed, and it is cheap insurance against
        believing a write succeeded when it silently did not.
        """
        try:
            actual = self.read_raw(identifier)
        except (KwpError, KwpNegativeResponse) as exc:
            log.warning("could not read back identifier 0x%02X: %s", identifier, exc)
            return False
        # Modules pad differently on read and write, so compare the meaningful prefix.
        width = min(len(expected), len(actual))
        return actual[:width] == expected[:width]


# Re-exported so callers do not have to reach into the read-only module for this.
READ_LOCAL_ID = SERVICE_READ_LOCAL_ID
