"""SAP S/4HANA OData API connector (ADR 052 Phase 1 -- Action Fabric).

Two skill variants against the SAP OData APIs:

* ``sap-employee-get``          -- GET  .../A_BusinessPartner('{id}')
* ``sap-purchaseorder-create``  -- POST .../A_PurchaseOrder

All use ``kind: http`` through the existing ``HttpSkillBackend``.
Auth is ``bearer-from-env:SAP_API_KEY``. The instance host
is passed as an input and templated into the URL at call time.
"""
