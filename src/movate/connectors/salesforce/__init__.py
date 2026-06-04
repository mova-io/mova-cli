"""Salesforce REST API connector (ADR 052 Phase 1 -- Action Fabric).

Three skill variants against the Salesforce REST API v59.0:

* ``salesforce-account-get``    -- GET  /services/data/v59.0/sobjects/Account/{id}
* ``salesforce-case-create``    -- POST /services/data/v59.0/sobjects/Case
* ``salesforce-contact-search`` -- GET  /services/data/v59.0/query/?q={soql}

All use ``kind: http`` through the existing ``HttpSkillBackend``.
Auth is ``bearer-from-env:SALESFORCE_ACCESS_TOKEN``. The instance host
is passed as an input and templated into the URL at call time.
"""
