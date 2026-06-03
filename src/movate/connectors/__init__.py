"""Built-in enterprise connectors for the Action Fabric (ADR 052 Phase 1).

Each connector is a bundle of HTTP skills behind the existing
``HttpSkillBackend`` -- no new dependencies, no new backend kind.
The ``BUILTIN_CONNECTORS`` dict maps connector names to their skill
bundle metadata so the Tool Registry can discover them at load time.

Connector layout::

    connectors/
        workday/             # Workday HCM API (worker + time-off)
            skill.yaml       # multi-action skill descriptor
        salesforce/          # Salesforce REST API (account, case, contact)
            skill.yaml       # multi-action skill descriptor
        sap/                 # SAP S/4HANA OData API (partner + PO)
            skill.yaml       # multi-action skill descriptor

Each connector's ``skill.yaml`` defines multiple skill variants
(actions) under the same namespace. The HTTP skill backend handles
URL templating, auth, and JSON I/O unchanged.
"""

from __future__ import annotations

from typing import Any

# Connector metadata for Tool Registry discovery (ADR 052 D3).
# Each entry: name -> dict with 'module' (importable path to the
# connector package), 'skills' (list of skill action names), and
# 'env_vars' (required environment variables).
BUILTIN_CONNECTORS: dict[str, dict[str, Any]] = {
    "workday": {
        "module": "movate.connectors.workday",
        "skills": [
            "workday-worker-get",
            "workday-worker-create",
            "workday-timeoff-balance",
        ],
        "env_vars": ["WORKDAY_ACCESS_TOKEN", "WORKDAY_BASE_URL"],
        "description": ("Workday HCM API -- worker lookup, onboarding, time-off balance."),
    },
    "salesforce": {
        "module": "movate.connectors.salesforce",
        "skills": [
            "salesforce-account-get",
            "salesforce-case-create",
            "salesforce-contact-search",
        ],
        "env_vars": ["SALESFORCE_ACCESS_TOKEN", "SALESFORCE_INSTANCE_URL"],
        "description": ("Salesforce REST API -- account lookup, case creation, contact search."),
    },
    "sap": {
        "module": "movate.connectors.sap",
        "skills": [
            "sap-employee-get",
            "sap-purchaseorder-create",
        ],
        "env_vars": ["SAP_API_KEY", "SAP_BASE_URL"],
        "description": (
            "SAP S/4HANA OData API -- business partner lookup, purchase order creation."
        ),
    },
}
