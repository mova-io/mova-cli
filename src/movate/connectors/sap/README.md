# SAP Connector

Enterprise connector for the SAP S/4HANA OData API (business partner + purchase order).
Part of the Action Fabric (ADR 052 Phase 1).

## Skills

| Skill name                    | Method | Endpoint                                                                              | Side-effects   |
|-------------------------------|--------|---------------------------------------------------------------------------------------|----------------|
| `sap.employee.get`            | GET    | `/sap/opu/odata/sap/API_BUSINESS_PARTNER/A_BusinessPartner('{id}')`                  | read-only      |
| `sap.purchaseorder.create`    | POST   | `/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV/A_PurchaseOrder`                   | mutates-state  |

## Setup

### 1. SAP API Business Hub Access

Set up API access for your SAP S/4HANA system:

1. Log in to the [SAP API Business Hub](https://api.sap.com/) or your
   on-premise S/4HANA system
2. Navigate to the APIs you need:
   - **Business Partner (A2X)** -- `API_BUSINESS_PARTNER`
   - **Purchase Order** -- `API_PURCHASEORDER_PROCESS_SRV`

### 2. Communication Arrangement (S/4HANA Cloud)

For SAP S/4HANA Cloud, set up a communication arrangement:

1. Create a **Communication System** in the SAP Fiori launchpad
2. Create a **Communication User** with the required authorization
3. Create a **Communication Arrangement** using the relevant
   communication scenario:
   - `SAP_COM_0008` for Business Partner
   - `SAP_COM_0069` for Purchase Order
4. Note the **Service URL** and **API Key** from the arrangement

### 3. On-Premise S/4HANA

For on-premise systems:

1. Create a technical user in SU01 with the required authorizations
2. Enable OData services in transaction `/IWFND/MAINT_SERVICE`:
   - `API_BUSINESS_PARTNER`
   - `API_PURCHASEORDER_PROCESS_SRV`
3. Generate an API key or use Basic Auth with a gateway service

### 4. Configure mdk

```bash
# Set the S/4HANA base URL
export SAP_BASE_URL=https://my-s4hana.sap-api.com

# Store the API key via mdk auth
mdk auth login sap
```

Or set both in your project `.env`:

```
SAP_BASE_URL=https://my-s4hana.sap-api.com
SAP_API_KEY=your-api-key-here
```

### 5. Reference in agent.yaml

```yaml
tools:
  - sap.employee.get@1.0.0
  - sap.purchaseorder.create@1.0.0
```

## Required permissions

The communication user / technical user needs:

- **Read**: Business Partner API (`API_BUSINESS_PARTNER`) for `employee.get`
- **Write**: Purchase Order API (`API_PURCHASEORDER_PROCESS_SRV`) for `purchaseorder.create`

## Environment variables

| Variable       | Required | Description                                     |
|----------------|----------|-------------------------------------------------|
| `SAP_API_KEY`  | Yes      | API key or bearer token for the SAP OData APIs. |
| `SAP_BASE_URL` | Yes      | Base URL of the SAP S/4HANA system.             |
