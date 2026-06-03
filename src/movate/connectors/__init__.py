"""Built-in enterprise connectors for the Action Fabric (ADR 052 Phase 1).

Each connector is a bundle of HTTP skills behind the existing
``HttpSkillBackend`` -- no new dependencies, no new backend kind.
The ``BUILTIN_CONNECTORS`` dict maps connector names to their skill
bundle metadata so the Tool Registry can discover them at load time.

Connector layout::

    connectors/
        servicenow/          # ServiceNow Table API (incident CRUD)
            skill.yaml       # multi-action skill descriptor
        msgraph/             # Microsoft Graph API (user + license mgmt)
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
    "servicenow": {
        "module": "movate.connectors.servicenow",
        "skills": [
            "servicenow-incident-create",
            "servicenow-incident-get",
            "servicenow-incident-update",
        ],
        "env_vars": ["SERVICENOW_API_KEY", "SERVICENOW_INSTANCE_URL"],
        "description": ("ServiceNow Table API -- incident create / get / update."),
    },
    "msgraph": {
        "module": "movate.connectors.msgraph",
        "skills": [
            "msgraph-user-create",
            "msgraph-user-resetpassword",
            "msgraph-license-assign",
        ],
        "env_vars": ["MSGRAPH_ACCESS_TOKEN", "MSGRAPH_TENANT_ID"],
        "description": (
            "Microsoft Graph API -- user provisioning, password reset, license assignment."
        ),
    },
}
