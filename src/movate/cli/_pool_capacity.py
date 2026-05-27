"""Connection-ceiling capacity check for ``mdk doctor`` (ADR 034 D1, no infra).

Under KEDA autoscale, each pod opens its own per-pod asyncpg pool of up to
``pool_max`` connections. With ``N`` pods that is ``N x pool_max`` connections
against a single Azure Postgres whose ``max_connections`` is finite — exceed it
and new connections fail with ``too many clients already``. The failure is
**invisible until load**: it only bites once enough pods scale up under traffic.

This module does the *static* capacity math the ADR 034 D1 doctor check needs —
no infra, no PgBouncer, no provisioning. It computes whether the worst-case fleet
connection demand fits under the server ceiling with headroom to spare, and
returns a structured verdict the doctor table + ``--explain`` block render.

The sizing formula (documented for operators, also in the verdict's remediation):

    pods x pool_max  <=  max_connections - headroom

i.e. the worst-case simultaneous connection demand of every pod's full pool,
plus a reserved ``headroom`` for superuser/admin/migration connections, must
stay at or under the server's ``max_connections``. When it doesn't, the fix is
one of: lower ``pool_max`` per pod, cap KEDA ``maxReplicas``, or front Postgres
with PgBouncer / Azure built-in pooling (the Deva-sign-off-gated ADR 034 D1
infra piece — this check only *flags* the need, it does not build it).

Everything degrades gracefully: when a value can't be observed it falls back to a
documented env override and then an assumed default, and the verdict records
which inputs were assumed so the doctor row can say "informational, assumed
inputs" rather than crying wolf. It NEVER raises — a capacity check must not be
able to crash ``mdk doctor``.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass, field
from typing import Literal

# Documented env overrides. Operators set these to match their actual deploy when
# the values can't be observed from the running process (the doctor CLI usually
# runs OUTSIDE the container, so it can't read the live pool / KEDA config).
ENV_POOL_MAX = "MOVATE_DB_POOL_MAX_SIZE"
ENV_MAX_REPLICAS = "MOVATE_KEDA_MAX_REPLICAS"
ENV_MAX_CONNECTIONS = "MOVATE_DB_MAX_CONNECTIONS"
ENV_HEADROOM = "MOVATE_DB_CONNECTION_HEADROOM"

# Assumed defaults when neither an observed value nor an env override is present.
# Chosen to match the shipped infra so the check is meaningful out of the box:
#   * pool_max 10  — PostgresProvider's create_pool(max_size=10) default.
#   * replicas 2   — infra/azure/.../containerapp-{api,worker}.bicep maxReplicas
#                    default is 2 EACH; the fleet ceiling is api + worker, so 4.
#   * max_connections 100 — a conservative Azure Postgres Flexible Server floor
#                    (small SKUs cap here; larger SKUs allow more). Assumed only
#                    when we can't query the live server.
#   * headroom 20  — reserve for superuser, migrations (the advisory-lock pod),
#                    monitoring, and Azure's own management connections.
DEFAULT_POOL_MAX = 10
DEFAULT_MAX_REPLICAS_PER_APP = 2
DEFAULT_FLEET_APPS = 2  # api + worker each run their own pool-bearing pods.
DEFAULT_MAX_CONNECTIONS = 100
DEFAULT_HEADROOM = 20

#: Human-readable form of the sizing formula — surfaced verbatim in the doctor
#: row, the ``--explain`` block, and the ADR/runbook so all three agree.
SIZING_FORMULA = "pods x pool_max <= max_connections - headroom"


@dataclass(frozen=True)
class _Input:
    """One resolved input + how it was resolved (observed / env / assumed)."""

    value: int
    source: Literal["observed", "env", "assumed"]


@dataclass(frozen=True)
class CapacityVerdict:
    """Result of the connection-ceiling capacity check.

    ``status`` drives the doctor row colour:

    * ``ok``      — demand fits under the ceiling with headroom (green).
    * ``warn``    — demand exceeds ``max_connections - headroom`` (yellow): the
      exhaustion risk ADR 034 D1 calls out. ``remediation`` says what to do.
    * ``info``    — inputs were assumed (we couldn't observe the live values), so
      the result is advisory, not authoritative (dim). Still shows the math.
    """

    status: Literal["ok", "warn", "info"]
    pods: int
    pool_max: int
    max_connections: int
    headroom: int
    demand: int  # pods x pool_max
    ceiling: int  # max_connections - headroom
    summary: str
    remediation: str = ""
    assumed: list[str] = field(default_factory=list)


def _read_int_env(name: str) -> int | None:
    """Parse a positive int from ``name``; None when unset/blank/invalid."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        n = int(raw)
    except ValueError:
        return None
    return n if n > 0 else None


def _resolve(observed: int | None, env_name: str, default: int) -> _Input:
    """Resolve an input: observed value > env override > assumed default."""
    if observed is not None:
        return _Input(observed, "observed")
    env_val = _read_int_env(env_name)
    if env_val is not None:
        return _Input(env_val, "env")
    return _Input(default, "assumed")


def compute_capacity_verdict(
    *,
    observed_pool_max: int | None = None,
    observed_max_connections: int | None = None,
    observed_max_replicas: int | None = None,
) -> CapacityVerdict:
    """Compute the connection-ceiling verdict from observed + env + assumed inputs.

    Pure + total (never raises). The caller passes any values it could observe
    (e.g. ``pool_max`` from the live ``PostgresProvider``, ``max_connections``
    from a ``SHOW max_connections`` query); anything ``None`` falls back to the
    documented env override and then the assumed default. ``observed_max_replicas``
    is the per-deploy fleet pod ceiling; when assumed we use api + worker each at
    their bicep ``maxReplicas`` default.
    """
    pool_max = _resolve(observed_pool_max, ENV_POOL_MAX, DEFAULT_POOL_MAX)
    max_conns = _resolve(observed_max_connections, ENV_MAX_CONNECTIONS, DEFAULT_MAX_CONNECTIONS)
    replicas = _resolve(
        observed_max_replicas,
        ENV_MAX_REPLICAS,
        DEFAULT_MAX_REPLICAS_PER_APP * DEFAULT_FLEET_APPS,
    )
    # Headroom has no observable form — env override or assumed default only.
    headroom_env = _read_int_env(ENV_HEADROOM)
    headroom = headroom_env if headroom_env is not None else DEFAULT_HEADROOM

    demand = replicas.value * pool_max.value
    ceiling = max_conns.value - headroom

    # Inputs that were ASSUMED (no observed value, no env override). Headroom is
    # tracked separately: it's a policy reserve constant with no observable form,
    # so a defaulted headroom alone must NOT downgrade a fully-observed verdict to
    # "info" — only the three capacity-determining inputs do that.
    assumed: list[str] = []
    if pool_max.source == "assumed":
        assumed.append(f"pool_max={pool_max.value}")
    if replicas.source == "assumed":
        assumed.append(f"pods={replicas.value}")
    if max_conns.source == "assumed":
        assumed.append(f"max_connections={max_conns.value}")
    # Whether a *capacity-determining* input was assumed (drives the info status).
    # Headroom is reported for transparency but doesn't on its own downgrade a
    # fully-observed verdict.
    capacity_assumed = bool(assumed)
    displayed_assumed = list(assumed)
    if headroom_env is None:
        displayed_assumed.append(f"headroom={headroom}")

    math_str = (
        f"{replicas.value} pods x {pool_max.value} pool_max = {demand} conns "
        f"vs ceiling {max_conns.value} - {headroom} = {ceiling}"
    )

    if demand > ceiling:
        # Over the ceiling — the exhaustion risk. WARN regardless of whether
        # inputs were assumed: a likely-real risk should be loud, with the fix.
        remediation = (
            f"worst-case fleet demand exceeds the connection ceiling "
            f"({SIZING_FORMULA}). Fix: lower {ENV_POOL_MAX} (per-pod pool_max), "
            f"cap KEDA maxReplicas ({ENV_MAX_REPLICAS}), or front Postgres with "
            f"PgBouncer / Azure built-in pooling (ADR 034 D1, infra)."
        )
        return CapacityVerdict(
            status="warn",
            pods=replicas.value,
            pool_max=pool_max.value,
            max_connections=max_conns.value,
            headroom=headroom,
            demand=demand,
            ceiling=ceiling,
            summary=f"connection exhaustion risk: {math_str}",
            remediation=remediation,
            assumed=displayed_assumed,
        )

    # Within the ceiling. If we had to assume a capacity-determining input, mark
    # it info (advisory) rather than a confident green — confirm the real values.
    status: Literal["ok", "info"] = "info" if capacity_assumed else "ok"
    summary = f"within ceiling: {math_str}"
    if displayed_assumed:
        summary += " [assumed inputs]"
    return CapacityVerdict(
        status=status,
        pods=replicas.value,
        pool_max=pool_max.value,
        max_connections=max_conns.value,
        headroom=headroom,
        demand=demand,
        ceiling=ceiling,
        summary=summary,
        assumed=displayed_assumed,
    )


async def probe_postgres_inputs() -> tuple[int | None, int | None]:
    """Best-effort: observe ``(pool_max, max_connections)`` from a live Postgres.

    Returns a tuple of either value or ``None`` when it can't be observed. Only
    attempts anything when ``MOVATE_DB_URL`` points at Postgres; for SQLite (or
    no DB) it returns ``(None, None)`` immediately so the verdict falls back to
    env/assumed inputs. NEVER raises — a DB that's down, unreachable, or
    misconfigured degrades to ``(None, None)`` and the check stays informational.

    ``max_connections`` comes from ``SHOW max_connections`` (cheap, no table
    access). ``pool_max`` is the provider's configured ceiling (the value it
    would pass to ``create_pool``), read without opening the pool.
    """
    db_url = os.environ.get("MOVATE_DB_URL", "").strip()
    if not db_url.startswith(("postgresql://", "postgres://")):
        return (None, None)

    try:
        import asyncpg  # noqa: PLC0415
    except ImportError:
        return (None, None)

    # pool_max: the value PostgresProvider would configure. It's a constructor
    # default today (not env-driven), so read it off the class without building
    # a provider — keeps this probe a single short-lived connection.
    pool_max: int | None
    try:
        from movate.storage.postgres import PostgresProvider  # noqa: PLC0415

        pool_max = PostgresProvider(dsn=db_url)._max_size
    except Exception:
        pool_max = None

    max_connections: int | None = None
    conn = None
    try:
        # A single short-lived connection (not the pool) + PGPASSWORD handling
        # mirrors PostgresProvider.init (ACA wires the password separately).
        kwargs: dict[str, object] = {}
        env_password = os.environ.get("PGPASSWORD")
        if env_password:
            kwargs["password"] = env_password
        conn = await asyncpg.connect(db_url, timeout=5, **kwargs)
        raw = await conn.fetchval("SHOW max_connections")
        max_connections = int(raw) if raw is not None else None
    except Exception:
        max_connections = None
    finally:
        if conn is not None:
            with contextlib.suppress(Exception):
                await conn.close()

    return (pool_max, max_connections)
