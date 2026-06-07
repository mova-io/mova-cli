"""ServiceNow Table API connector (ADR 052 Phase 1 -- Action Fabric).

Three skill variants against the ServiceNow Table API:

* ``servicenow-incident-create``  -- POST /api/now/table/incident
* ``servicenow-incident-get``     -- GET  .../incident/{sys_id}
* ``servicenow-incident-update``  -- PATCH .../incident/{sys_id}

All use ``kind: http`` through the existing ``HttpSkillBackend``.
Auth is ``bearer-from-env:SERVICENOW_API_KEY``. The instance host
is passed as an input field and templated into the URL.
"""
