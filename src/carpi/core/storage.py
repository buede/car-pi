"""Writing vehicle data to disk without handing it to everybody on the machine.

A scan is not a neutral document. It contains the VIN, which identifies one physical car
and, through it, a person; a coding restore point additionally contains the module's login
code, which is the secret that gates writing to that car. Written with the default umask
those land as ``-rw-r--r--``, readable by every account on the machine and by anything
running as another user.

This lives in ``core`` rather than beside any one caller because three layers need it --
the report writer, the sweep writer, and the coding archive -- and a chmod duplicated in
three places is a chmod forgotten in one. :mod:`carpi.coding` may import from ``core``;
the firewall only runs the other way.

What this does not attempt
--------------------------
Encryption. The threat it addresses is the ordinary one: a shared machine, a backup that
copies the home directory, a file left in a checkout. A key would have to be stored
somewhere on the same disk to be usable unattended, which moves the problem rather than
solving it, and would make an archive unreadable exactly when somebody needs it -- at the
car, putting a module back.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["PRIVATE_DIR_MODE", "PRIVATE_FILE_MODE", "write_private"]

# Owner only. A scan is readable by the person who took it and nobody else.
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIR_MODE = 0o700


def write_private(path: Path, text: str) -> Path:
    """Write *text* to *path*, readable only by the current user. Returns the path.

    The mode is applied as the file is created rather than afterwards, so there is no
    moment at which the content exists and is world-readable. It is applied again to the
    open descriptor because a file that already existed keeps whatever mode it had --
    which is the case that matters when an earlier version of car-pi wrote it.
    """
    directory = path.parent
    if str(directory) and not directory.exists():
        directory.mkdir(parents=True, mode=PRIVATE_DIR_MODE, exist_ok=True)

    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_FILE_MODE)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        # Not available on every platform car-pi can be developed on; the Pi and macOS both
        # have it, and where it is missing the creation mode above still applies.
        if hasattr(os, "fchmod"):
            os.fchmod(handle.fileno(), PRIVATE_FILE_MODE)
        handle.write(text)
    return path
