# syntax=docker/dockerfile:1.7
#
# Development, test and runtime images for car-pi.
#
# Pinned to Python 3.11 on Debian 12, which is exactly what Raspberry Pi OS Bookworm
# ships. That is the point of building this rather than using whatever Python the
# developer's machine happens to have: the container *is* the deployment target, so a
# version-specific problem shows up here instead of on the Pi.
#
# It also makes SocketCAN testable from macOS, where it otherwise cannot be tested at
# all -- the container has a real Linux kernel underneath it.

ARG PYTHON_VERSION=3.11

FROM python:${PYTHON_VERSION}-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /src


# --- development and test ----------------------------------------------------
FROM base AS dev

# iproute2 and kmod are for bringing up a vcan interface; shellcheck lints the
# deployment scripts, which are shell and therefore easy to get quietly wrong.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         iproute2 \
         kmod \
         shellcheck \
    && rm -rf /var/lib/apt/lists/*

# Copied rather than mounted so the image is usable on its own; compose then mounts
# the working tree over /src at the same path, which keeps the editable install valid
# and makes edits take effect without a rebuild.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -e '.[dev]'

CMD ["pytest"]


# --- runtime -----------------------------------------------------------------
# A slim image for running the unit. Note that on a Pi the primary deployment is the
# systemd unit in deploy/, not this: the hotspot and SocketCAN both want the host's
# network namespace, so containerising there buys little. This exists for running the
# server somewhere else, and to prove the package installs cleanly from scratch.
FROM base AS runtime

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install . \
    && useradd --create-home --uid 10001 carpi

USER carpi
EXPOSE 8080

# Defaults to a simulated vehicle. Reaching a real bus needs the host network
# namespace and a configured SocketCAN interface, which is a deliberate opt-in.
CMD ["carpi", "serve", "--transport", "sim", "--host", "0.0.0.0", "--port", "8080"]
