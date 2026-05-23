"""Client-side OIDC token acquisition (ADR 012 D4).

The control-plane counterpart to :mod:`movate.runtime.oidc`: when a target's
:class:`~movate.core.user_config.TargetConfig` opts into ``auth: oidc``, the
CLI obtains a short-lived OIDC JWT from an :class:`OidcTokenProvider` instead
of reading the static ``key_env`` bearer.

Provider seam (so the deferred ``azure-identity`` provider can slot in later
without touching call sites):

* :class:`OidcTokenProvider` — the Protocol all providers satisfy.
* :class:`AzureCliTokenProvider` — the **default, no-new-dependency** provider.
  Shells out to ``az account get-access-token``, which the operator already
  has installed (the same ``az`` ``refresh_runtime_key_inline`` uses). MIT/no
  extra dep.

``azure-identity`` (``DefaultAzureCredential`` for managed identity / env
creds) is explicitly **DEFERRED** pending Deva sign-off (ADR 012 D4) and is
intentionally NOT implemented here. A future ``DefaultAzureCredentialProvider``
would implement the same :class:`OidcTokenProvider` Protocol behind the
``mdk[azure-identity]`` extra and only be imported when selected.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from movate.core.user_config import TargetConfig


class OidcTokenError(Exception):
    """Raised when an OIDC token can't be obtained for a target.

    Carries an operator-actionable message — the CLI surfaces it directly
    (it never contains a token).
    """


@runtime_checkable
class OidcTokenProvider(Protocol):
    """Obtains an OIDC bearer token for an ``auth: oidc`` target.

    Implementations MUST raise :class:`OidcTokenError` with an actionable
    message on any failure and MUST NOT log the returned token.
    """

    def get_token(self, target_name: str, target: TargetConfig) -> str:
        """Return a bearer token string for ``target``."""
        ...


class AzureCliTokenProvider:
    """Default provider — shells out to the Azure CLI.

    Runs ``az account get-access-token --resource <resource> --query
    accessToken -o tsv`` (or ``--scope`` when the target sets ``oidc_scope``).
    Needs no new Python dependency: the operator already has ``az`` for
    deploys / key refresh.
    """

    # Bounded so a hung/prompting `az` (e.g. needs interactive `az login`)
    # fails fast with an actionable error rather than blocking the CLI.
    _TIMEOUT_SECONDS = 60

    def get_token(self, target_name: str, target: TargetConfig) -> str:
        if shutil.which("az") is None:
            raise OidcTokenError(
                f"target {target_name!r} uses auth='oidc' but the Azure CLI ('az') "
                "was not found on PATH. Install it (https://aka.ms/azure-cli) and run "
                "`az login`, or switch the target to auth='key'."
            )

        cmd = ["az", "account", "get-access-token"]
        if target.oidc_scope:
            cmd += ["--scope", target.oidc_scope]
        elif target.oidc_resource:
            cmd += ["--resource", target.oidc_resource]
        else:
            raise OidcTokenError(
                f"target {target_name!r} uses auth='oidc' but neither 'oidc_resource' "
                "nor 'oidc_scope' is set. Add one to ~/.movate/config.yaml (it must "
                "match the runtime's MOVATE_OIDC_AUDIENCE)."
            )
        cmd += ["--query", "accessToken", "-o", "tsv"]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._TIMEOUT_SECONDS,
                check=False,
            )
        except FileNotFoundError as exc:  # race: az vanished between which() + run()
            raise OidcTokenError(
                f"target {target_name!r} uses auth='oidc' but the Azure CLI ('az') "
                "could not be executed. Install it and run `az login`."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise OidcTokenError(
                f"`az account get-access-token` timed out for target {target_name!r}. "
                "Run `az login` to refresh your Azure CLI session, then retry."
            ) from exc

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise OidcTokenError(
                f"`az account get-access-token` failed for target {target_name!r} "
                f"(exit {proc.returncode}). Run `az login` and confirm the resource/"
                f"scope is correct.{(' Details: ' + detail) if detail else ''}"
            )

        token = (proc.stdout or "").strip()
        # `-o tsv` returns the bare token; defensively unwrap JSON if a future
        # az/output-format change hands us a quoted/object value instead.
        if token.startswith(("{", '"')):
            try:
                parsed = json.loads(token)
                token = parsed["accessToken"] if isinstance(parsed, dict) else str(parsed)
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
        if not token:
            raise OidcTokenError(
                f"`az account get-access-token` returned an empty token for "
                f"target {target_name!r}. Run `az login` and retry."
            )
        return token


def default_oidc_provider() -> OidcTokenProvider:
    """The provider used when a target sets ``auth: oidc`` with no override.

    Today this is always the Azure CLI provider (no new dependency). The
    indirection is the seam where a future ``azure-identity`` provider
    (DEFERRED, Deva sign-off pending) would be selected.
    """
    return AzureCliTokenProvider()


__all__ = [
    "AzureCliTokenProvider",
    "OidcTokenError",
    "OidcTokenProvider",
    "default_oidc_provider",
]
