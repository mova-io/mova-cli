"""Workday HCM API connector (ADR 052 Phase 1 -- Action Fabric).

Three skill variants against the Workday REST API:

* ``workday-worker-get``       -- GET  /ccx/api/v1/{tenant}/workers/{worker_id}
* ``workday-worker-create``    -- POST /ccx/api/v1/{tenant}/workers
* ``workday-timeoff-balance``  -- GET  /ccx/api/v1/{tenant}/workers/{worker_id}/timeOffBalance

All use ``kind: http`` through the existing ``HttpSkillBackend``.
Auth is ``bearer-from-env:WORKDAY_ACCESS_TOKEN``. The instance host
is passed as an input and templated into the URL at call time.
"""
