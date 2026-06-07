# Microsoft Graph Connector

Enterprise connector for the Microsoft Graph REST API v1.0 (user
provisioning, password reset, license assignment). Part of the Action
Fabric (ADR 052 Phase 1).

## Skills

| Skill name                    | Method | Endpoint                                                                   | Side-effects   |
|-------------------------------|--------|----------------------------------------------------------------------------|----------------|
| `msgraph.user.create`         | POST   | `/v1.0/users`                                                              | mutates-state  |
| `msgraph.user.resetpassword`  | POST   | `/v1.0/users/{user_id}/authentication/methods/{method_id}/resetPassword`   | mutates-state  |
| `msgraph.license.assign`      | POST   | `/v1.0/users/{user_id}/assignLicense`                                      | mutates-state  |

## Setup

### 1. Azure AD app registration

Register an application in Azure Active Directory:

1. Go to **Azure Portal > Azure Active Directory > App registrations**
2. Click **New registration**
3. Name it (e.g. `movate-graph-connector`)
4. Set supported account type to **Single tenant**
5. Click **Register**

### 2. API permissions

Add the following Microsoft Graph **Application** permissions:

- `User.ReadWrite.All` (create users, update profiles)
- `UserAuthenticationMethod.ReadWrite.All` (reset passwords)
- `Directory.ReadWrite.All` (assign licenses)

Then click **Grant admin consent** for your tenant.

### 3. Client credentials

Generate a client secret or certificate:

1. Go to **Certificates & secrets > New client secret**
2. Note the secret value (shown only once)

Use the client credentials flow to obtain an access token:

```bash
curl -X POST "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token" \
  -d "client_id={app_id}" \
  -d "client_secret={secret}" \
  -d "scope=https://graph.microsoft.com/.default" \
  -d "grant_type=client_credentials"
```

### 4. Configure mdk

```bash
# Set the tenant ID
export MSGRAPH_TENANT_ID=your-tenant-id

# Store the access token via mdk auth
mdk auth login msgraph
```

Or set both in your project `.env`:

```
MSGRAPH_TENANT_ID=your-azure-ad-tenant-id
MSGRAPH_ACCESS_TOKEN=your-access-token-here
```

### 5. Reference in agent.yaml

```yaml
tools:
  - msgraph.user.create@1.0.0
  - msgraph.user.resetpassword@1.0.0
  - msgraph.license.assign@1.0.0
```

## Required Graph API permissions

| Skill                        | Permission                              | Type        |
|------------------------------|-----------------------------------------|-------------|
| `msgraph.user.create`        | `User.ReadWrite.All`                    | Application |
| `msgraph.user.resetpassword` | `UserAuthenticationMethod.ReadWrite.All` | Application |
| `msgraph.license.assign`     | `User.ReadWrite.All`, `Directory.ReadWrite.All` | Application |

## Environment variables

| Variable              | Required | Description                                    |
|-----------------------|----------|------------------------------------------------|
| `MSGRAPH_ACCESS_TOKEN`| Yes      | Bearer token for the Microsoft Graph API.      |
| `MSGRAPH_TENANT_ID`   | Yes      | Azure AD tenant ID (for multi-tenant routing). |
