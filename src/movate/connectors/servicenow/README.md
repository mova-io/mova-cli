# ServiceNow Connector

Enterprise connector for the ServiceNow Table API (incident CRUD).
Part of the Action Fabric (ADR 052 Phase 1).

## Skills

| Skill name                     | Method | Endpoint                                  | Side-effects   |
|--------------------------------|--------|-------------------------------------------|----------------|
| `servicenow.incident.create`   | POST   | `/api/now/table/incident`                 | mutates-state  |
| `servicenow.incident.get`      | GET    | `/api/now/table/incident/{sys_id}`        | read-only      |
| `servicenow.incident.update`   | PATCH  | `/api/now/table/incident/{sys_id}`        | mutates-state  |

## Setup

### 1. ServiceNow instance

You need a ServiceNow instance with the Table API enabled (enabled by
default on all instances). Note your instance URL, e.g.
`https://mycompany.service-now.com`.

### 2. API credentials

Create a ServiceNow API user or use an existing integration user with
the following roles:

- `itil` (or a custom role with read/write access to the `incident` table)
- Table API access enabled

Generate an API key or use Basic-to-Bearer auth through your ServiceNow
OAuth configuration.

### 3. Configure mdk

```bash
# Set the instance URL
export SERVICENOW_INSTANCE_URL=https://mycompany.service-now.com

# Store the API key via mdk auth
mdk auth login servicenow
```

Or set both in your project `.env`:

```
SERVICENOW_INSTANCE_URL=https://mycompany.service-now.com
SERVICENOW_API_KEY=your-api-key-here
```

### 4. Reference in agent.yaml

```yaml
tools:
  - servicenow.incident.create@1.0.0
  - servicenow.incident.get@1.0.0
  - servicenow.incident.update@1.0.0
```

## Required permissions

The Table API user needs:

- **Read**: `GET` on `/api/now/table/incident` (for `incident.get`)
- **Write**: `POST` on `/api/now/table/incident` (for `incident.create`)
- **Update**: `PATCH` on `/api/now/table/incident` (for `incident.update`)

## Environment variables

| Variable                  | Required | Description                                |
|---------------------------|----------|--------------------------------------------|
| `SERVICENOW_API_KEY`      | Yes      | Bearer token for the ServiceNow Table API. |
| `SERVICENOW_INSTANCE_URL` | Yes      | Base URL of the ServiceNow instance.       |
