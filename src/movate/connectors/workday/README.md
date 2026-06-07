# Workday Connector

Enterprise connector for the Workday HCM REST API (worker + time-off management).
Part of the Action Fabric (ADR 052 Phase 1).

## Skills

| Skill name                  | Method | Endpoint                                                         | Side-effects   |
|-----------------------------|--------|------------------------------------------------------------------|----------------|
| `workday.worker.get`        | GET    | `/ccx/api/v1/{tenant}/workers/{worker_id}`                       | read-only      |
| `workday.worker.create`     | POST   | `/ccx/api/v1/{tenant}/workers`                                   | mutates-state  |
| `workday.timeoff.balance`   | GET    | `/ccx/api/v1/{tenant}/workers/{worker_id}/timeOffBalance`        | read-only      |

## Setup

### 1. Workday Integration System User (ISU)

Create an Integration System User in your Workday tenant:

1. Navigate to **Create Integration System User** task in Workday
2. Assign a username (e.g. `mdk_integration`) and password
3. Do **not** require a password change at next sign-in
4. Add the ISU to the **Integration System Security Group**

### 2. API Client Registration

Register an API client for OAuth 2.0 access:

1. Go to **Register API Client for Integrations** in Workday
2. Set the **Client Name** (e.g. `mdk-connector`)
3. Select **Bearer Token** as the grant type
4. Note the **Client ID** and **Client Secret**
5. Generate a bearer token using the OAuth 2.0 token endpoint:
   `POST https://<host>/ccx/oauth2/<tenant>/token`

### 3. Security Policy Configuration

Grant the ISU access to the required Workday domains:

- **Worker Data**: Get and Put access for worker lookup and onboarding
- **Time Off**: Get access for time-off balance queries
- **Staffing**: Put access for worker creation

Activate the security policy changes after configuration.

### 4. Configure mdk

```bash
# Set the base URL (your Workday REST API endpoint)
export WORKDAY_BASE_URL=https://wd3-impl-services1.workday.com

# Store the access token via mdk auth
mdk auth login workday
```

Or set both in your project `.env`:

```
WORKDAY_BASE_URL=https://wd3-impl-services1.workday.com
WORKDAY_ACCESS_TOKEN=your-bearer-token-here
```

### 5. Reference in agent.yaml

```yaml
tools:
  - workday.worker.get@1.0.0
  - workday.worker.create@1.0.0
  - workday.timeoff.balance@1.0.0
```

## Required permissions

The Integration System User needs:

- **Read**: Worker Data domain (for `worker.get` and `timeoff.balance`)
- **Write**: Staffing domain (for `worker.create`)
- **Read**: Time Off domain (for `timeoff.balance`)

## Environment variables

| Variable               | Required | Description                                    |
|------------------------|----------|------------------------------------------------|
| `WORKDAY_ACCESS_TOKEN` | Yes      | Bearer token for the Workday REST API.         |
| `WORKDAY_BASE_URL`     | Yes      | Base URL of the Workday REST API endpoint.     |
