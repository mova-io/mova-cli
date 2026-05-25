"""Azure-side preflight checks for ``movate doctor --target <name>``.

Lives in its own module so :mod:`movate.cli.doctor` stays a focused
"local environment" report. The Azure section appears only when the
operator opts in via ``--target``; everyone else sees the same doctor
output they've always seen.

The checks all shell out to ``az``. Each is one subprocess + a status
classification — same pattern as ``movate deploy``, so we never grow a
hard dep on the Azure SDKs. Failures are categorized:

* ``ok``       — green ✓
* ``missing``  — yellow ! with a hint
* ``error``    — red ✗ with the underlying message

Order matters: we check ``az login`` first because every later check
implicitly needs it; we check subscription before RG before ACR
before Container Apps so the operator sees the earliest broken link
without false-positive cascading errors.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from movate.core.user_config import TargetConfig

Status = Literal["ok", "missing", "error"]


@dataclass
class Check:
    """One row of the Azure preflight table."""

    name: str
    status: Status
    detail: str = ""


def run_azure_preflight(target_name: str, target: TargetConfig) -> list[Check]:
    """Run every Azure-side check for ``target``. Returns rows for the
    doctor table; never raises.

    The checks are ordered from "always-runnable" to "depends on the
    previous check passing". Each unconditional row catches an
    independent failure mode; gated rows skip rather than producing
    a misleading red ✗ when the upstream check already explained why.
    """
    checks: list[Check] = []

    # ------------------------------------------------------------------
    # az CLI present + logged in
    # ------------------------------------------------------------------
    if shutil.which("az") is None:
        checks.append(
            Check(
                "az CLI",
                "missing",
                "install: https://learn.microsoft.com/cli/azure/install-azure-cli",
            )
        )
        return checks  # nothing else works without az
    checks.append(Check("az CLI", "ok", ""))

    account = _az_json(["az", "account", "show"])
    if account is None:
        checks.append(Check("az login", "missing", "run `az login`"))
        return checks
    logged_in_sub = account.get("id", "?")
    checks.append(Check("az login", "ok", f"tenant={account.get('tenantId', '?')[:8]}…"))

    # ------------------------------------------------------------------
    # Target's Azure config is fully populated
    # ------------------------------------------------------------------
    missing_fields = [
        name
        for name, value in (
            ("azure_subscription", target.azure_subscription),
            ("azure_resource_group", target.azure_resource_group),
            ("azure_acr_name", target.azure_acr_name),
            ("azure_env", target.azure_env),
        )
        if not value
    ]
    if missing_fields:
        checks.append(
            Check(
                "target azure config",
                "missing",
                f"missing on target {target_name!r}: {', '.join(missing_fields)}; "
                "see `movate config add-target --help`",
            )
        )
        return checks
    checks.append(Check("target azure config", "ok", f"env={target.azure_env}"))
    # Pydantic narrowing isn't enough across the `if` boundary.
    assert target.azure_subscription
    assert target.azure_resource_group
    assert target.azure_acr_name
    assert target.azure_env

    # ------------------------------------------------------------------
    # Subscription matches what the deploy will use
    # ------------------------------------------------------------------
    if logged_in_sub != target.azure_subscription:
        checks.append(
            Check(
                "subscription match",
                "missing",
                f"logged in to {logged_in_sub[:8]}…; "
                f"target wants {target.azure_subscription[:8]}…; "
                f"run `az account set --subscription {target.azure_subscription}`",
            )
        )
        return checks
    checks.append(Check("subscription match", "ok", target.azure_subscription[:8] + "…"))

    # ------------------------------------------------------------------
    # Resource group exists
    # ------------------------------------------------------------------
    rg = _az_json(
        [
            "az",
            "group",
            "show",
            "--subscription",
            target.azure_subscription,
            "--name",
            target.azure_resource_group,
        ]
    )
    if rg is None:
        checks.append(
            Check(
                "resource group",
                "missing",
                f"{target.azure_resource_group!r} not found; "
                "run `scripts/azure-bootstrap.sh <env>` to create it",
            )
        )
        return checks
    checks.append(
        Check("resource group", "ok", f"{target.azure_resource_group} ({rg.get('location', '?')})")
    )

    # ------------------------------------------------------------------
    # ACR exists
    # ------------------------------------------------------------------
    acr = _az_json(
        [
            "az",
            "acr",
            "show",
            "--subscription",
            target.azure_subscription,
            "--resource-group",
            target.azure_resource_group,
            "--name",
            target.azure_acr_name,
        ]
    )
    if acr is None:
        checks.append(
            Check(
                "ACR",
                "missing",
                f"{target.azure_acr_name}.azurecr.io not found in "
                f"{target.azure_resource_group}; run the Bicep deploy first",
            )
        )
    else:
        checks.append(
            Check(
                "ACR",
                "ok",
                f"{target.azure_acr_name}.azurecr.io ({acr.get('sku', {}).get('name', '?')})",
            )
        )

    # ------------------------------------------------------------------
    # Container Apps exist + are running (api + worker)
    # ------------------------------------------------------------------
    # A deployed api with a dead worker = jobs enqueue but never drain
    # (item 65 live-validation finding). So we report the worker's
    # running state, not just its existence — a Failed/Suspended worker
    # is an `error`, not a green ✓.
    for app_suffix in ("api", "worker"):
        checks.append(
            _check_containerapp(
                target.azure_subscription,
                target.azure_resource_group,
                f"movate-{target.azure_env}-{app_suffix}",
                label=f"containerapp {app_suffix}",
            )
        )

    # ------------------------------------------------------------------
    # Scheduler Job exists + reports its last run (item 65)
    # ------------------------------------------------------------------
    # The scheduler is a Container Apps *Job* (ADR 017 D2), not an app —
    # it's optional (gated by `enableScheduler` in Bicep), so absence is
    # informational (`missing`), never an `error`. When present we surface
    # the last execution's status so the operator can spot a tick that
    # has been failing silently.
    checks.append(
        _check_scheduler_job(
            target.azure_subscription,
            target.azure_resource_group,
            f"movate-{target.azure_env}-scheduler",
        )
    )

    # ------------------------------------------------------------------
    # OTel Collector health when App Insights export is enabled (item 65)
    # ------------------------------------------------------------------
    # The item-41 collector (`*-otelcol`) is the in-cluster bridge to App
    # Insights (ADR 020). It's only deployed when App Insights export is
    # on, so its absence is a no-op (`ok`, n/a). When present, a crash-loop
    # (e.g. the `command`-vs-`args` bug we hit) means telemetry silently
    # never reaches App Insights — that's an `error`.
    checks.append(
        _check_otel_collector(
            target.azure_subscription,
            target.azure_resource_group,
            f"movate-{target.azure_env}-otelcol",
        )
    )

    # ------------------------------------------------------------------
    # Required Key Vault secrets present (item 65)
    # ------------------------------------------------------------------
    # A deploy that comes up but 500s on first request usually means a
    # required secret never got set in KV. Catch it here rather than at
    # runtime. The KV name carries a global-uniqueness suffix (Bicep
    # `nameSuffix`) the target config doesn't store, so we discover it in
    # the RG by its `movate-{env}-kv` prefix rather than hardcoding.
    checks.append(
        _check_keyvault_secrets(
            target.azure_subscription,
            target.azure_resource_group,
            target.azure_env,
        )
    )

    # ------------------------------------------------------------------
    # /healthz responds
    # ------------------------------------------------------------------
    healthz = _check_healthz(target.url)
    checks.append(healthz)

    # ------------------------------------------------------------------
    # /ready reports a durable storage backend
    # ------------------------------------------------------------------
    # If /healthz is unreachable, /ready will be too — skip rather than
    # cascade a confusing second red row.
    if healthz.status == "ok":
        checks.append(_check_storage_durability(target.url))
        # Authenticated round-trip — the operator's saved bearer is the
        # one that gets used at deploy time, so a green storage row
        # + a red auth row means "infra is fine but YOUR copy of the
        # key is stale." Closes the gap between Layer 1's unauthed
        # health checks and the deploy-time 401 that would otherwise
        # be the first signal something's wrong.
        checks.append(_check_auth_roundtrip(target))

    return checks


def _az_json(cmd: list[str]) -> dict[str, Any] | None:
    """Run an ``az`` command with ``-o json``. Returns parsed dict or None
    if the command failed (most common: resource not found, returns
    non-zero exit and "not found" stderr).
    """
    import json  # noqa: PLC0415

    try:
        result = subprocess.run(
            [*cmd, "-o", "json"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    # Sometimes az returns an empty array for `account list` etc.; here
    # we only call commands that return objects, so reject non-dicts.
    return parsed if isinstance(parsed, dict) else None


def _az_json_list(cmd: list[str]) -> list[Any] | None:
    """Run an ``az`` command with ``-o json`` that returns an array.

    Sibling to :func:`_az_json` for the list-shaped commands (``keyvault
    list``, ``keyvault secret list``). Returns the parsed list, or None if
    the command failed / returned a non-list (so callers can distinguish
    "errored" from "empty list").
    """
    import json  # noqa: PLC0415

    try:
        result = subprocess.run(
            [*cmd, "-o", "json"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else None


def _check_containerapp(
    subscription: str, resource_group: str, app_name: str, *, label: str
) -> Check:
    """Report a Container App's existence + running state.

    The existing api/worker rows only proved the app *existed*; this also
    classifies its ``runningStatus``. A Failed/Suspended worker (the dead
    worker = jobs never drain failure mode) is an ``error``, not a green
    ✓. Degrades gracefully: a missing app is ``missing`` (run the deploy),
    an ``az`` error / unparseable response surfaces a finding rather than
    raising.
    """
    app = _az_json(
        [
            "az",
            "containerapp",
            "show",
            "--subscription",
            subscription,
            "--resource-group",
            resource_group,
            "--name",
            app_name,
        ]
    )
    if app is None:
        return Check(label, "missing", f"{app_name!r} not found; run the Bicep deploy")

    props = app.get("properties", {}) if isinstance(app, dict) else {}
    if not isinstance(props, dict):
        props = {}
    # Pull the running image tag for at-a-glance "what's deployed?"
    try:
        image = props.get("template", {}).get("containers", [{}])[0].get("image", "?")
    except (AttributeError, IndexError, TypeError):
        image = "?"
    running = props.get("runningStatus") or props.get("provisioningState")
    # Older API versions may omit runningStatus; treat unknown as ok so we
    # don't false-positive a healthy app on an older az/runtime.
    if running and str(running).lower() not in ("running", "succeeded"):
        return Check(
            label,
            "error",
            f"{app_name} runningStatus={running} ({image}); inspect with "
            f"`az containerapp logs show -n {app_name} -g {resource_group} --type system`",
        )
    detail = image if not running else f"{image} ({running})"
    return Check(label, "ok", detail)


def _check_scheduler_job(subscription: str, resource_group: str, job_name: str) -> Check:
    """Report the scheduler Container Apps **Job**'s existence + last run.

    The scheduler (ADR 017 D2) is optional — gated by ``enableScheduler``
    in Bicep — so its absence is ``missing`` (informational), never an
    ``error``. When present we surface the most recent execution's status
    so a tick that has been failing silently is visible. Degrades
    gracefully: an ``az``-unavailable / errored call yields a finding, and
    a missing execution history still reports the job as ``ok`` (deployed
    but not yet run).
    """
    job = _az_json(
        [
            "az",
            "containerapp",
            "job",
            "show",
            "--subscription",
            subscription,
            "--resource-group",
            resource_group,
            "--name",
            job_name,
        ]
    )
    if job is None:
        return Check(
            "scheduler job",
            "missing",
            f"{job_name!r} not found (scheduler not deployed — enableScheduler=false?)",
        )

    executions = _az_json_list(
        [
            "az",
            "containerapp",
            "job",
            "execution",
            "list",
            "--subscription",
            subscription,
            "--resource-group",
            resource_group,
            "--name",
            job_name,
        ]
    )
    if not executions:
        # Deployed but no run recorded yet (or list errored) — still a
        # healthy "it exists" signal; don't manufacture a red row.
        return Check("scheduler job", "ok", f"{job_name} (no runs yet)")

    # Most recent execution first (az returns newest-first; be defensive).
    latest = executions[0] if isinstance(executions[0], dict) else {}
    props = latest.get("properties", {})
    if not isinstance(props, dict):
        props = {}
    status = props.get("status") or latest.get("status") or "?"
    if str(status).lower() in ("failed", "degraded"):
        return Check(
            "scheduler job",
            "error",
            f"{job_name} last run {status}; inspect with "
            f"`az containerapp job execution list -n {job_name} -g {resource_group}`",
        )
    return Check("scheduler job", "ok", f"{job_name} (last run {status})")


def _check_otel_collector(subscription: str, resource_group: str, app_name: str) -> Check:
    """Report the OTel Collector Container App's health *when present*.

    The collector (item 41 / ADR 020) is the in-cluster bridge to App
    Insights and is only deployed when App Insights export is enabled, so
    its absence is a no-op: ``ok`` with an "n/a" detail, never a red row.
    When present, a crash-loop (the ``command``-vs-``args`` bug we hit in
    live validation) means telemetry silently never reaches App Insights —
    a Failed/Suspended ``runningStatus`` is an ``error``. Degrades
    gracefully on any ``az`` error.
    """
    app = _az_json(
        [
            "az",
            "containerapp",
            "show",
            "--subscription",
            subscription,
            "--resource-group",
            resource_group,
            "--name",
            app_name,
        ]
    )
    if app is None:
        # No collector = App Insights export not enabled. Nothing wrong.
        return Check("otel collector", "ok", "n/a (App Insights export not enabled)")

    props = app.get("properties", {}) if isinstance(app, dict) else {}
    if not isinstance(props, dict):
        props = {}
    running = props.get("runningStatus") or props.get("provisioningState")
    if running and str(running).lower() not in ("running", "succeeded"):
        return Check(
            "otel collector",
            "error",
            f"{app_name} runningStatus={running} — telemetry not reaching App "
            f"Insights; inspect with `az containerapp logs show -n {app_name} "
            f"-g {resource_group} --type system`",
        )
    return Check("otel collector", "ok", f"{app_name} ({running or 'present'})")


# The runtime cannot boot / serve without these in Key Vault. langfuse-*
# and appinsights-* secrets are intentionally NOT here — they're optional
# (App Insights / Langfuse export are opt-in), so their absence must never
# fail the preflight.
_REQUIRED_KV_SECRETS = (
    "pg-admin-password",
    "openai-api-key",
    "anthropic-api-key",
    "bootstrap-api-key",
)


def _check_keyvault_secrets(subscription: str, resource_group: str, env: str) -> Check:
    """Verify the runtime's required Key Vault secrets exist.

    The KV name carries a global-uniqueness suffix (Bicep ``nameSuffix``)
    the target config doesn't store, so we discover the vault in the RG by
    its ``movate-{env}-kv`` prefix rather than hardcoding a suffix. Yields
    ``ok`` (all present), ``missing`` (lists the absent secrets + how to
    set them), or ``error`` (KV / list could not be resolved). Degrades
    gracefully: an ``az``-unavailable / errored call yields a finding, and
    optional langfuse/appinsights secrets are never counted as missing.
    """
    prefix = f"movate-{env}-kv"
    vaults = _az_json_list(
        [
            "az",
            "keyvault",
            "list",
            "--subscription",
            subscription,
            "--resource-group",
            resource_group,
            "--query",
            f"[?starts_with(name, '{prefix}')]",
        ]
    )
    if vaults is None:
        return Check(
            "key vault secrets",
            "error",
            f"could not list Key Vaults in {resource_group!r} "
            "(missing 'Key Vault Reader' / Azure RBAC?)",
        )
    if not vaults:
        return Check(
            "key vault secrets",
            "missing",
            f"no Key Vault matching {prefix!r} in {resource_group}; run the Bicep deploy first",
        )
    vault_name = vaults[0].get("name") if isinstance(vaults[0], dict) else None
    if not vault_name:
        return Check(
            "key vault secrets",
            "error",
            f"Key Vault in {resource_group} has no resolvable name",
        )

    secrets = _az_json_list(
        [
            "az",
            "keyvault",
            "secret",
            "list",
            "--subscription",
            subscription,
            "--vault-name",
            vault_name,
        ]
    )
    if secrets is None:
        return Check(
            "key vault secrets",
            "error",
            f"could not list secrets in {vault_name!r} "
            "(missing 'Key Vault Secrets User' on your principal?)",
        )

    present = {
        s.get("name", "").rsplit("/", 1)[-1]
        for s in secrets
        if isinstance(s, dict) and s.get("name")
    }
    absent = [name for name in _REQUIRED_KV_SECRETS if name not in present]
    if absent:
        return Check(
            "key vault secrets",
            "missing",
            f"{vault_name} missing: {', '.join(absent)}; set with "
            f"`az keyvault secret set --vault-name {vault_name} --name <name> --value ...` "
            f"(bootstrap-api-key via `mdk auth bootstrap-seed {env} --keyvault {vault_name}`)",
        )
    return Check(
        "key vault secrets",
        "ok",
        f"{vault_name} ({len(_REQUIRED_KV_SECRETS)} required present)",
    )


def _check_healthz(url: str) -> Check:
    """``GET /healthz`` against the deployed runtime. Reports the version
    when reachable so an operator can see at a glance what's serving."""
    try:
        with httpx.Client(timeout=5.0) as client:
            r = client.get(f"{url.rstrip('/')}/healthz")
    except httpx.HTTPError as exc:
        return Check("/healthz", "error", f"unreachable: {exc.__class__.__name__}")
    if r.status_code != httpx.codes.OK:
        return Check("/healthz", "error", f"HTTP {r.status_code}")
    try:
        body = r.json()
    except ValueError:
        return Check("/healthz", "error", "non-JSON response")
    version = body.get("version", "?")
    return Check("/healthz", "ok", f"serving v{version}")


def _check_storage_durability(url: str) -> Check:
    """``GET /ready`` against the deployed runtime, then surface whether
    the chosen storage backend survives container restarts.

    A red ✗ here is the single most important signal in the preflight:
    if the backend isn't durable, every revision recycle wipes the
    ApiKeyRecord table and the operator's saved bearer turns into a 401.
    Catching this once via the doctor is much better than re-running
    `mdk auth refresh-runtime-key` on every deploy.
    """
    try:
        with httpx.Client(timeout=5.0) as client:
            r = client.get(f"{url.rstrip('/')}/ready")
    except httpx.HTTPError as exc:
        return Check("storage durability", "error", f"unreachable: {exc.__class__.__name__}")
    # /ready returns 503 when storage ping fails; that's a separate
    # signal from durability, so we still parse the body for the
    # backend fields regardless of status code.
    try:
        body = r.json()
    except ValueError:
        return Check("storage durability", "error", "non-JSON /ready response")
    backend = body.get("storage_backend")
    durable = body.get("storage_durable")
    if backend is None or durable is None:
        # Older runtime that hasn't shipped the durability fields yet.
        return Check(
            "storage durability",
            "missing",
            "runtime predates storage durability reporting; upgrade the runtime image",
        )
    if durable:
        return Check("storage durability", "ok", f"{backend} (durable across recycles)")
    return Check(
        "storage durability",
        "error",
        f"{backend} — NOT durable; revision recycles wipe keys. "
        "Set MOVATE_DB_URL=postgresql://... on the Container App and restart.",
    )


def _check_auth_roundtrip(target: TargetConfig) -> Check:
    """Hit ``GET /api/v1/agents`` with the operator's saved bearer.

    This is the single most actionable doctor row: it answers "would
    my next `mdk deploy` get a 401?" without actually attempting the
    deploy. Three statuses:

    * ``ok`` — bearer authenticates; ``mdk deploy`` will succeed.
    * ``missing`` — env var unset; recovery is `mdk auth pull-runtime-key`
      (preferred) or `mdk auth refresh-runtime-key`.
    * ``error`` — bearer rejected with 401, or the request errored at
      the transport layer. Detail names the recovery command.
    """
    import os  # noqa: PLC0415

    api_key = os.environ.get(target.key_env, "").strip()
    if not api_key:
        return Check(
            "auth roundtrip",
            "missing",
            f"${target.key_env} is empty — run `mdk auth pull-runtime-key "
            f"<target>` or `mdk auth refresh-runtime-key <target>`",
        )

    try:
        with httpx.Client(timeout=5.0) as client:
            r = client.get(
                f"{target.url.rstrip('/')}/api/v1/agents",
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except httpx.HTTPError as exc:
        return Check("auth roundtrip", "error", f"unreachable: {exc.__class__.__name__}")
    if r.status_code == httpx.codes.UNAUTHORIZED:
        prefix = api_key[:16]
        return Check(
            "auth roundtrip",
            "error",
            f"401 (saved bearer starts '{prefix}…' — stale; run "
            f"`mdk auth pull-runtime-key <target>` to refresh from KV)",
        )
    if r.status_code != httpx.codes.OK:
        return Check("auth roundtrip", "error", f"HTTP {r.status_code}")
    return Check("auth roundtrip", "ok", "saved bearer accepted")


__all__ = ["Check", "run_azure_preflight"]
