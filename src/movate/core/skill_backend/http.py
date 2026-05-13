"""HTTP skill backend — POSTs to an arbitrary URL, parses JSON response.

Second backend per ADR 002. Lets skills call any REST API the operator
controls: internal CRM lookups, weather, customer-facing services,
hosted ML endpoints. Unlocks "tool-using agents that talk to real
infrastructure" — the biggest functional gap after PR 1.

Shape, recapped from :class:`SkillImplementation`:

* ``entry`` — the URL. May contain ``{{ input.* }}`` Jinja
  placeholders that get rendered against the skill call's input dict.
* ``method`` — HTTP method (default ``POST``).
* ``auth`` — ``bearer-from-env:VAR_NAME``. The named env var's value
  goes out as ``Authorization: Bearer <value>``. ``None`` for no auth.
  More shapes (basic, header-from-env) land in later PRs.
* ``headers`` — static headers added to every invocation.
* ``timeout_seconds`` — per-skill override; ``None`` falls through to
  the caller's ``call_ms_budget``.

Request body: the full input dict (after URL templating consumes
whatever it consumes). POST/PUT/PATCH send JSON; GET/DELETE send the
input as query parameters.

Failure → :class:`SkillError` mapping:

* Env var named in ``auth`` doesn't exist → ``backend_error``
* URL templating fails (e.g. ``{{ input.x }}`` but no ``x`` in input)
  → ``backend_error`` (the agent's tool-use loop will see it and
  recover)
* httpx connection/timeout errors → ``backend_error`` / ``timeout``
* Non-2xx status → ``backend_error`` with status + body excerpt
* Response body isn't valid JSON → ``backend_error``
* Response body isn't a dict → ``validation_failed`` (the skill's
  output schema requires an object)

Caches one ``httpx.AsyncClient`` per backend instance — the connection
pool amortizes TCP+TLS across N tool calls in a long-running session.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import httpx
from jinja2 import Environment, StrictUndefined, select_autoescape

from movate.core.models import SkillImplementationKind
from movate.core.skill_backend.base import (
    SkillError,
    SkillErrorType,
    SkillExecutionContext,
    register_backend,
)

if TYPE_CHECKING:
    from movate.core.skill_loader import SkillBundle


# Methods that carry a JSON body. Everything else sends ``input`` as
# query params (well-suited for pure lookup skills like a weather GET).
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})

# HTTP statuses ≥ this surface as backend_error. Named so the lint
# linter's "magic number" check stays quiet and the threshold is in
# one obvious place.
_HTTP_ERROR_THRESHOLD = 400


class HttpSkillBackend:
    """Dispatches ``kind: http`` skills via httpx.

    One instance handles every HTTP skill in the project. We keep a
    single ``httpx.AsyncClient`` open for the backend's lifetime so
    consecutive tool calls reuse the connection pool — important for
    a multi-turn tool-use loop calling the same internal API.
    """

    kind = SkillImplementationKind.HTTP

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        """``transport`` is for tests — pass an ``httpx.MockTransport``
        to intercept every outbound HTTP call without touching the
        network. Production callers leave it ``None`` (real transport)."""
        self._client: httpx.AsyncClient | None = None
        self._transport = transport

    async def execute(
        self,
        skill: SkillBundle,
        input: dict[str, Any],
        ctx: SkillExecutionContext,
    ) -> dict[str, Any]:
        impl = skill.spec.implementation

        # 1) URL templating. Jinja with StrictUndefined so a missing
        # input field surfaces as a clean ``backend_error`` instead of
        # silently substituting an empty string into the URL.
        try:
            url = _render_url(impl.entry, input)
        except SkillError:
            raise
        except Exception as exc:
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"http skill {skill.spec.name!r}: failed to render URL "
                    f"template {impl.entry!r}: {exc}"
                ),
            ) from exc

        # 2) Auth header. ``bearer-from-env:VAR`` looks up the env var
        # and sends it as a Bearer token. Missing env var is fatal —
        # the skill's whole job is to talk to an authenticated endpoint.
        headers = dict(impl.headers)
        if impl.auth is not None:
            auth_header = _build_auth_header(impl.auth, skill.spec.name)
            headers["Authorization"] = auth_header

        # 3) Build + send the request. JSON body for write methods,
        # query params for everything else. The skill's output schema
        # validates the response one layer up in dispatch_skill.
        client = self._ensure_client()
        method = impl.method
        # Effective timeout: skill's HTTP-specific override > inherited
        # context budget. ctx.call_ms_budget is already the merged
        # agent + skill timeout from the executor.
        timeout = (
            impl.timeout_seconds
            if impl.timeout_seconds is not None
            else max(1, ctx.call_ms_budget // 1000)
        )

        try:
            if method in _BODY_METHODS:
                response = await client.request(
                    method,
                    url,
                    headers=headers,
                    json=input,
                    timeout=timeout,
                )
            else:
                response = await client.request(
                    method,
                    url,
                    headers=headers,
                    params=input,
                    timeout=timeout,
                )
        except httpx.TimeoutException as exc:
            # Surface as TIMEOUT so the LLM sees consistent vocabulary;
            # the outer dispatch wrap will also catch wait_for timeouts
            # but a raw httpx.TimeoutException can happen sub-budget
            # (e.g. DNS hung).
            raise SkillError(
                type=SkillErrorType.TIMEOUT,
                message=(
                    f"http skill {skill.spec.name!r}: timeout after {timeout}s "
                    f"calling {method} {url}: {exc}"
                ),
            ) from exc
        except httpx.HTTPError as exc:
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"http skill {skill.spec.name!r}: transport error on "
                    f"{method} {url}: {type(exc).__name__}: {exc}"
                ),
            ) from exc

        # 4) Status check. Anything non-2xx is the operator's API
        # telling us "no, that didn't work" — surface as backend_error
        # so the model can recover (e.g. try a different tool, give up,
        # explain the failure to the user).
        if response.status_code >= _HTTP_ERROR_THRESHOLD:
            body_excerpt = response.text[:500]
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"http skill {skill.spec.name!r}: {method} {url} returned "
                    f"{response.status_code}: {body_excerpt}"
                ),
            )

        # 5) Parse response. JSON-only — non-JSON endpoints aren't
        # usable as skills today (the model contract requires the
        # output to match a JSON Schema). validation_failed for non-dict
        # so the operator sees a clean error rather than a deep
        # jsonschema traceback.
        try:
            payload = response.json()
        except ValueError as exc:
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(f"http skill {skill.spec.name!r}: response body wasn't valid JSON: {exc}"),
            ) from exc

        if not isinstance(payload, dict):
            raise SkillError(
                type=SkillErrorType.VALIDATION_FAILED,
                message=(
                    f"http skill {skill.spec.name!r}: response was a "
                    f"{type(payload).__name__}, expected a JSON object"
                ),
            )

        return payload

    async def aclose(self) -> None:
        """Close the connection pool. Called when the executor shuts
        down; safe to call multiple times."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _ensure_client(self) -> httpx.AsyncClient:
        """Lazy-init the httpx client on first use. Per-instance, so
        tests that swap in a ``MockTransport`` get a fresh client."""
        if self._client is None:
            kwargs: dict[str, Any] = {}
            if self._transport is not None:
                kwargs["transport"] = self._transport
            self._client = httpx.AsyncClient(**kwargs)
        return self._client


# ---------------------------------------------------------------------------
# Helpers (module-level for testability)
# ---------------------------------------------------------------------------


def _render_url(template: str, input: dict[str, Any]) -> str:
    """Render Jinja placeholders in the URL template against the input.

    StrictUndefined turns a missing key into a clear exception rather
    than silently substituting an empty string — operators reading
    the resulting URL would otherwise be debugging "why is the API
    seeing /lookup// when I expected /lookup/abc123".

    The template scope is just ``input`` (matching the agent's prompt
    template) — no filesystem, no env, no Python helpers. URLs are
    user-influenced data; we don't want them to be a side-effect
    vehicle.
    """
    if "{{" not in template and "{%" not in template:
        # Fast-path: literal URL, no rendering needed. Keeps the
        # common case zero-overhead.
        return template
    env = Environment(
        autoescape=select_autoescape(disabled_extensions=("md",)),
        undefined=StrictUndefined,
        keep_trailing_newline=False,
    )
    rendered = env.from_string(template).render(input=input)
    return rendered


def _build_auth_header(auth_spec: str, skill_name: str) -> str:
    """Resolve an ``auth:`` spec into the Authorization header value.

    Today the only supported shape is ``bearer-from-env:VAR_NAME``.
    The model_validator on :class:`SkillSpec` already rejected
    anything else at load time, so reaching here with an unknown
    prefix is a contract violation worth crashing on.
    """
    if not auth_spec.startswith("bearer-from-env:"):
        # Shouldn't happen — the spec validator caught it. Defensive
        # because someone could construct a SkillSpec by hand and
        # skip the validator (e.g. dynamic skill registration).
        raise SkillError(
            type=SkillErrorType.BACKEND_ERROR,
            message=f"http skill {skill_name!r}: unsupported auth scheme {auth_spec!r}",
        )
    var_name = auth_spec.removeprefix("bearer-from-env:").strip()
    if not var_name:
        raise SkillError(
            type=SkillErrorType.BACKEND_ERROR,
            message=(
                f"http skill {skill_name!r}: auth spec {auth_spec!r} is missing "
                "the env-var name (expected 'bearer-from-env:VAR')"
            ),
        )
    value = os.environ.get(var_name)
    if not value:
        raise SkillError(
            type=SkillErrorType.BACKEND_ERROR,
            message=(
                f"http skill {skill_name!r}: env var {var_name!r} (for auth) is "
                "unset or empty; set it or change the skill's auth spec"
            ),
        )
    return f"Bearer {value}"


# Auto-register on import. The executor imports this module at runtime
# (alongside the Python backend) so by the time any HTTP skill is
# dispatched the backend is in the registry. See _runtime.py.
register_backend(HttpSkillBackend())
