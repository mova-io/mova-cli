"""Temporal dev-server fixture — ``temporal server start-dev`` integration.

Starts the Temporal CLI single-binary dev server as a subprocess, waits
for it to be ready (polls ``localhost:7233``), yields a connected
``temporalio.client.Client``, and tears down the server on fixture exit.

Skip-if-absent guard: if ``temporal`` is not on ``$PATH`` the fixture
(and every test that depends on it) is skipped with a clear message
rather than failing CI. The dev server is a local-development convenience,
not a CI hard requirement.

All tests in this directory are gated behind ``@pytest.mark.temporal``
(skip by default; run with ``pytest -m temporal``).

ADR 054 Phase 1 item 1.13 — local ``temporal server start-dev`` test
integration.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
from collections.abc import AsyncGenerator
from typing import Any

import pytest


def _is_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """Return True if a TCP connection to ``host:port`` succeeds."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, TimeoutError):
        return False


def _wait_for_server(
    host: str = "localhost",
    port: int = 7233,
    *,
    max_wait: float = 30.0,
    poll_interval: float = 0.5,
) -> None:
    """Block until the Temporal dev server accepts TCP connections.

    Raises ``TimeoutError`` if the server is not ready within
    ``max_wait`` seconds.
    """
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        if _is_port_open(host, port):
            return
        time.sleep(poll_interval)
    raise TimeoutError(
        f"Temporal dev server did not become ready at {host}:{port} within {max_wait}s"
    )


@pytest.fixture(scope="session")
def temporal_server() -> Any:
    """Start ``temporal server start-dev`` and yield connection info.

    Skip-if-absent: if the ``temporal`` CLI binary is not on ``$PATH``
    the fixture skips with a message pointing at the download page.

    The server runs on the default ``localhost:7233`` with namespace
    ``default``. The process is terminated on fixture teardown.

    Yields a dict: ``{"host": str, "port": int, "namespace": str}``.
    """
    temporal_bin = shutil.which("temporal")
    if temporal_bin is None:
        pytest.skip(
            "temporal CLI not found on $PATH — install from "
            "https://temporal.io/download to run @pytest.mark.temporal tests"
        )

    # Start the dev server. --headless suppresses the Temporal Web UI
    # auto-open; --log-level warn keeps output quiet.
    proc = subprocess.Popen(
        [
            temporal_bin,
            "server",
            "start-dev",
            "--headless",
            "--log-level",
            "warn",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        _wait_for_server("localhost", 7233, max_wait=30.0)
    except TimeoutError:
        proc.terminate()
        proc.wait(timeout=5)
        pytest.fail("temporal server start-dev did not become ready within 30s")

    yield {"host": "localhost:7233", "port": 7233, "namespace": "default"}

    # Teardown: terminate the dev server.
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


@pytest.fixture
async def temporal_client(temporal_server: dict[str, Any]) -> AsyncGenerator[Any, None]:
    """Yield a connected ``temporalio.client.Client`` against the dev server.

    Depends on :func:`temporal_server` (session-scoped) so the server
    is started once per test session.
    """
    from temporalio.client import Client  # noqa: PLC0415

    client = await Client.connect(
        temporal_server["host"],
        namespace=temporal_server["namespace"],
    )
    yield client
