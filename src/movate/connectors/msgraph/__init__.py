"""Microsoft Graph API connector (ADR 052 Phase 1 -- Action Fabric).

Three skill variants against the Microsoft Graph REST API:

* ``msgraph-user-create``         -- POST /v1.0/users
* ``msgraph-user-resetpassword``  -- POST .../resetPassword
* ``msgraph-license-assign``      -- POST .../assignLicense

All use ``kind: http`` through the existing ``HttpSkillBackend``.
Auth is ``bearer-from-env:MSGRAPH_ACCESS_TOKEN``. The tenant ID
is available via ``MSGRAPH_TENANT_ID`` for multi-tenant scenarios.
"""
