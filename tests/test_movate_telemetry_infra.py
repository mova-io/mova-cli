"""Fleet-telemetry infra guard (ADR 039 Phase 1).

The Movate-side fleet observability infra under ``infra/movate-telemetry/`` is
**Movate-ops-only** (deployed manually in Movate's own subscription + a per-
customer Azure Lighthouse offer). Because it never rides the per-customer
``main.bicep`` deploy, it had **no automated guard** — it could silently rot, and
worse, a careless edit to the Lighthouse offer could broaden the cross-tenant
grant beyond the ADR 039 D2 least-privilege contract (read-only telemetry).

This test is that guard. It is **pure-Python + parse-only** (no ``az`` needed) so
it runs in CI everywhere, plus an opt-in ``az bicep build`` smoke when the CLI is
present. It enforces the load-bearing security invariant: the Lighthouse offer and
the Grafana MI grant **only Monitoring Reader** — never a write/Owner/Contributor
role — into the customer's tenant.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TELEMETRY_DIR = _REPO_ROOT / "infra" / "movate-telemetry"
_LIGHTHOUSE = _TELEMETRY_DIR / "lighthouse-offer.json"
_LIGHTHOUSE_PARAMS = _TELEMETRY_DIR / "lighthouse-offer.parameters.example.json"
_MANAGED_GRAFANA = _TELEMETRY_DIR / "managed-grafana.bicep"
_ASSIGN_MI = _TELEMETRY_DIR / "_assign-mi-monitoring-reader.bicep"

# Built-in Azure role-definition GUIDs.
_MONITORING_READER = "43d0d8ad-25c7-4714-9337-8ba259a9fe05"  # read of telemetry only
_GRAFANA_ADMIN = "22926164-76b3-42b3-bc55-97df8dab3e41"  # scoped to the Grafana instance

# Roles that would BREACH the ADR 039 D2 least-privilege contract if they ever
# appeared in the cross-tenant Lighthouse offer or the Grafana MI's subscription
# grant. The guard fails loudly if any of these GUIDs show up.
_FORBIDDEN_ROLES = {
    "8e3af657-a8ff-443c-a75c-2fe8c4bcb635": "Owner",
    "b24988ac-6180-42a0-ab88-20f7382dd24c": "Contributor",
    "749f88d5-cbae-40b8-bcfc-e573ddc772fa": "Monitoring Contributor",  # WRITE on Monitor
    "00482a5a-887f-4fb3-b363-3b7fe8e74483": "Key Vault Administrator",
    "4633458b-17de-408a-b874-0445c86b69e6": "Key Vault Secrets User",
}

# A role-definition GUID as it appears in a roleDefinitionId path.
_GUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_GUID_RE = re.compile(_GUID)


def _role_guids_in(text: str) -> set[str]:
    """All role-definition GUIDs referenced under a roleDefinitions path in text."""
    # Only GUIDs that sit in a roleDefinitions reference are roles; other GUIDs
    # (guid() name seeds, tenant placeholders) are not. Match the path form.
    return {
        m.group(1).lower()
        for m in re.finditer(r"roleDefinitions/(" + _GUID_RE.pattern + r")", text)
    }


# --------------------------------------------------------------------------- #
# Lighthouse offer — the cross-tenant grant customers deploy
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_lighthouse_offer_parses_as_arm_template() -> None:
    assert _LIGHTHOUSE.is_file(), f"missing {_LIGHTHOUSE}"
    doc = json.loads(_LIGHTHOUSE.read_text(encoding="utf-8"))
    assert "deploymentTemplate" in doc["$schema"]
    types = {r["type"] for r in doc["resources"]}
    assert "Microsoft.ManagedServices/registrationDefinitions" in types
    assert "Microsoft.ManagedServices/registrationAssignments" in types


@pytest.mark.unit
def test_lighthouse_offer_grants_only_monitoring_reader() -> None:
    """The load-bearing ADR 039 D2 invariant: ONE authorization, read-only role.

    A broadened grant (extra authorization, or a write/Owner role) would silently
    over-delegate into every onboarded customer's tenant. Fail loudly if so.
    """
    doc = json.loads(_LIGHTHOUSE.read_text(encoding="utf-8"))
    reg_def = next(
        r
        for r in doc["resources"]
        if r["type"] == "Microsoft.ManagedServices/registrationDefinitions"
    )
    auths = reg_def["properties"]["authorizations"]
    assert len(auths) == 1, f"expected exactly one authorization, got {len(auths)}"
    # The roleDefinitionId is an ARM variable reference; resolve via the GUID scan
    # over the whole file (the only role GUID present must be Monitoring Reader).
    role_guids = _role_guids_in(_LIGHTHOUSE.read_text(encoding="utf-8"))
    assert role_guids == {_MONITORING_READER}, (
        f"lighthouse offer references role GUIDs {sorted(role_guids)}; the ADR 039 "
        f"D2 contract allows ONLY Monitoring Reader ({_MONITORING_READER})."
    )


@pytest.mark.unit
def test_lighthouse_offer_has_no_forbidden_roles() -> None:
    text = _LIGHTHOUSE.read_text(encoding="utf-8")
    for guid, name in _FORBIDDEN_ROLES.items():
        assert guid not in text.lower(), (
            f"lighthouse offer references {name} ({guid}) — breaches the read-only "
            f"least-privilege contract (ADR 039 D2)."
        )


@pytest.mark.unit
def test_lighthouse_params_example_parses_and_has_required_keys() -> None:
    assert _LIGHTHOUSE_PARAMS.is_file(), f"missing {_LIGHTHOUSE_PARAMS}"
    doc = json.loads(_LIGHTHOUSE_PARAMS.read_text(encoding="utf-8"))
    params = doc["parameters"]
    # The two operator-pasted values the offer needs (from managed-grafana outputs).
    assert "movateTenantId" in params
    assert "movateApplicationId" in params


# --------------------------------------------------------------------------- #
# Managed Grafana bicep — Movate-side instance + MI grant
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_managed_grafana_bicep_invariants() -> None:
    text = _MANAGED_GRAFANA.read_text(encoding="utf-8")
    assert "Microsoft.Dashboard/grafana@" in text, "not a Managed Grafana resource"
    assert "SystemAssigned" in text, "MI must be system-assigned (Lighthouse principal)"
    assert _MONITORING_READER in text, "MI must be granted Monitoring Reader"
    # Outputs the customer onboarding runbook copies into the Lighthouse params.
    assert "managedIdentityTenantId" in text
    assert "managedIdentityApplicationId" in text


@pytest.mark.unit
def test_managed_grafana_mi_has_no_write_role() -> None:
    """The Grafana MI's SUBSCRIPTION-scoped grant must stay read-only.

    Grafana Admin (instance-scoped, NOT a subscription role) is allowed; the
    forbidden set is the write/Owner/secrets roles that would over-grant.
    """
    text = _MANAGED_GRAFANA.read_text(encoding="utf-8").lower()
    for guid, name in _FORBIDDEN_ROLES.items():
        assert guid not in text, f"managed-grafana.bicep references {name} ({guid})"


@pytest.mark.unit
def test_assign_mi_module_is_monitoring_reader_only() -> None:
    text = _ASSIGN_MI.read_text(encoding="utf-8")
    assert "Microsoft.Authorization/roleAssignments@" in text
    assert "targetScope = 'subscription'" in text
    for guid, name in _FORBIDDEN_ROLES.items():
        assert guid not in text.lower(), f"_assign module references {name} ({guid})"


# --------------------------------------------------------------------------- #
# Opt-in compile smoke — real bicep build when the az CLI is present
# --------------------------------------------------------------------------- #
@pytest.mark.unit
@pytest.mark.skipif(shutil.which("az") is None, reason="az CLI not installed")
def test_managed_grafana_bicep_compiles() -> None:
    """`az bicep build` the fleet Grafana template (also builds the nested module).

    Skipped where az isn't installed (most CI); a real compile check locally /
    in az-enabled CI so a bicep syntax error can't merge.
    """
    proc = subprocess.run(
        ["az", "bicep", "build", "--file", str(_MANAGED_GRAFANA), "--stdout"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"az bicep build failed:\n{proc.stderr}"
