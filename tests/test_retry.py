"""Retry policy: taxonomy honored, backoff applied, retry_after preferred."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from movate.core.failures import (
    AuthError,
    MovateTimeoutError,
    RateLimitError,
    SchemaError,
)
from movate.core.retry import RetryExhaustedError, run_with_retries


@pytest.mark.unit
async def test_succeeds_first_try() -> None:
    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        return "ok"

    assert await run_with_retries(fn) == "ok"
    assert calls == 1


@pytest.mark.unit
async def test_retries_then_succeeds() -> None:
    attempts = 0

    async def fn() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RateLimitError("slow down")
        return "ok"

    with patch("movate.core.retry.asyncio.sleep") as fake_sleep:
        result = await run_with_retries(fn)
    assert result == "ok"
    assert attempts == 3
    # Two sleeps: between attempt 1→2 and 2→3.
    assert fake_sleep.call_count == 2


@pytest.mark.unit
async def test_rate_limit_uses_retry_after() -> None:
    async def fn() -> None:
        raise RateLimitError("slow", retry_after=7.0)

    with patch("movate.core.retry.asyncio.sleep") as fake_sleep, pytest.raises(RetryExhaustedError):
        await run_with_retries(fn)
    # Each retry sleeps for the server-supplied retry_after, not the table.
    assert all(call.args[0] == 7.0 for call in fake_sleep.call_args_list)


@pytest.mark.unit
async def test_no_retry_for_schema_error() -> None:
    attempts = 0

    async def fn() -> None:
        nonlocal attempts
        attempts += 1
        raise SchemaError("bad")

    with pytest.raises(RetryExhaustedError):
        await run_with_retries(fn)
    assert attempts == 1


@pytest.mark.unit
async def test_no_retry_for_auth_error() -> None:
    attempts = 0

    async def fn() -> None:
        nonlocal attempts
        attempts += 1
        raise AuthError("unauthorized")

    with pytest.raises(RetryExhaustedError):
        await run_with_retries(fn)
    assert attempts == 1


@pytest.mark.unit
async def test_timeout_retries_once() -> None:
    attempts = 0

    async def fn() -> None:
        nonlocal attempts
        attempts += 1
        raise MovateTimeoutError("timeout")

    with patch("movate.core.retry.asyncio.sleep"), pytest.raises(RetryExhaustedError) as excinfo:
        await run_with_retries(fn)
    assert attempts == 2  # default rule: 2 attempts
    assert excinfo.value.attempts == 2


@pytest.mark.unit
async def test_retry_exhausted_carries_last_error() -> None:
    last_msg = "rate limit hit again"

    async def fn() -> None:
        raise RateLimitError(last_msg)

    with patch("movate.core.retry.asyncio.sleep"), pytest.raises(RetryExhaustedError) as excinfo:
        await run_with_retries(fn)
    assert isinstance(excinfo.value.last_error, RateLimitError)
    assert last_msg in str(excinfo.value.last_error)
