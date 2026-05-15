"""Verify a provider API key with a cheap test call.

Catches typos + expired keys at ``mdk auth login`` time instead of at
first ``mdk run`` — operators don't want to discover their key is
wrong an hour into prompt iteration.

Each provider has a canonical "list models" or equivalent metadata
endpoint that costs ~$0 to call. We use those. Network errors degrade
gracefully — a verify-failed key still gets saved (operators may be
offline; the test call would have caught typos only).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VerifyResult:
    """Outcome of a provider-key verification probe."""

    ok: bool
    """Whether the key worked. ``False`` covers both auth failures
    (key is wrong) and network errors (could be auth, could be us)."""

    detail: str
    """Human-readable context: ``"OK — 47 models available"`` on
    success, ``"401 Unauthorized — key rejected"`` on auth failure,
    ``"network error: ..."`` on transport failure."""

    network_error: bool = False
    """True when the verify call failed for connectivity reasons
    (not for the key itself being wrong). Lets the caller distinguish
    "your key is bad" from "we couldn't reach the provider right now"
    when deciding whether to still save the key."""


def verify_provider_key(provider: str, key: str) -> VerifyResult:
    """Test a provider key with a cheap metadata call.

    ``provider`` is one of {``openai``, ``anthropic``, ``azure``,
    ``gemini``}. Unknown providers return ``VerifyResult(ok=True)``
    with a "verification skipped" detail — better to let the operator
    proceed than block on a provider we haven't wired.

    Network timeout is hard-capped at 5 seconds so a slow provider
    can't hang the ``mdk auth login`` flow.
    """
    provider = provider.lower().strip()
    if provider == "openai":
        return _verify_openai(key)
    if provider == "anthropic":
        return _verify_anthropic(key)
    if provider in ("azure", "azure-openai", "azure_openai"):
        return _verify_azure_openai(key)
    if provider == "gemini":
        return _verify_gemini(key)
    return VerifyResult(
        ok=True,
        detail=f"verification not wired for provider {provider!r}; key saved as-is",
    )


_HTTP_TIMEOUT = 5.0


def _verify_openai(key: str) -> VerifyResult:
    """Probe OpenAI by listing models — the cheapest authenticated call."""
    import httpx  # noqa: PLC0415

    try:
        resp = httpx.get(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=_HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        return VerifyResult(
            ok=False,
            detail=f"network error: {exc}",
            network_error=True,
        )
    if resp.status_code == 200:  # noqa: PLR2004
        data = resp.json()
        n_models = len(data.get("data", []))
        return VerifyResult(ok=True, detail=f"OK — {n_models} models available")
    if resp.status_code == 401:  # noqa: PLR2004
        return VerifyResult(ok=False, detail="401 Unauthorized — key rejected")
    return VerifyResult(
        ok=False,
        detail=f"HTTP {resp.status_code}: {resp.text[:80]}",
    )


def _verify_anthropic(key: str) -> VerifyResult:
    """Probe Anthropic by calling the messages endpoint with a tiny
    payload. Anthropic doesn't have a `/v1/models` so we use a
    1-token message instead — still cheap (<$0.0001).
    """
    import httpx  # noqa: PLC0415

    try:
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            },
            timeout=_HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        return VerifyResult(
            ok=False,
            detail=f"network error: {exc}",
            network_error=True,
        )
    if resp.status_code == 200:  # noqa: PLR2004
        return VerifyResult(ok=True, detail="OK — messages endpoint reachable")
    if resp.status_code == 401:  # noqa: PLR2004
        return VerifyResult(ok=False, detail="401 Unauthorized — key rejected")
    return VerifyResult(
        ok=False,
        detail=f"HTTP {resp.status_code}: {resp.text[:80]}",
    )


def _verify_azure_openai(key: str) -> VerifyResult:
    """Azure OpenAI requires endpoint URL + deployment name in addition
    to the key — we don't have either at ``mdk auth login`` time.

    Return a skip-verification result; operators will discover any
    issue at first ``mdk run`` against the Azure-backed agent.
    """
    return VerifyResult(
        ok=True,
        detail=(
            "verification skipped — Azure OpenAI needs endpoint + "
            "deployment name; saving key as-is"
        ),
    )


def _verify_gemini(key: str) -> VerifyResult:
    """Probe Gemini by listing models."""
    import httpx  # noqa: PLC0415

    try:
        resp = httpx.get(
            f"https://generativelanguage.googleapis.com/v1/models?key={key}",
            timeout=_HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        return VerifyResult(
            ok=False,
            detail=f"network error: {exc}",
            network_error=True,
        )
    if resp.status_code == 200:  # noqa: PLR2004
        data = resp.json()
        n_models = len(data.get("models", []))
        return VerifyResult(ok=True, detail=f"OK — {n_models} models available")
    if resp.status_code in (401, 403):
        return VerifyResult(ok=False, detail=f"{resp.status_code} — key rejected")
    return VerifyResult(
        ok=False,
        detail=f"HTTP {resp.status_code}: {resp.text[:80]}",
    )
