# Salesforce Connector

Enterprise connector for the Salesforce REST API (account, case, contact management).
Part of the Action Fabric (ADR 052 Phase 1).

## Skills

| Skill name                     | Method | Endpoint                                           | Side-effects   |
|--------------------------------|--------|----------------------------------------------------|----------------|
| `salesforce.account.get`       | GET    | `/services/data/v59.0/sobjects/Account/{id}`       | read-only      |
| `salesforce.case.create`       | POST   | `/services/data/v59.0/sobjects/Case`               | mutates-state  |
| `salesforce.contact.search`    | GET    | `/services/data/v59.0/query/?q={soql}`             | read-only      |

## Setup

### 1. Connected App

Create a Connected App in your Salesforce org:

1. Navigate to **Setup > App Manager > New Connected App**
2. Enable **OAuth Settings**
3. Set the **Callback URL** (e.g. `https://localhost/callback` for server-to-server)
4. Select the following **OAuth Scopes**:
   - `api` (Access and manage your data)
   - `refresh_token` (Perform requests at any time)
5. Note the **Consumer Key** (Client ID) and **Consumer Secret**

### 2. OAuth 2.0 Token

Obtain a bearer token using one of these OAuth 2.0 flows:

**Username-Password flow (server-to-server):**
```bash
curl -X POST https://login.salesforce.com/services/oauth2/token \
  -d "grant_type=password" \
  -d "client_id=YOUR_CONSUMER_KEY" \
  -d "client_secret=YOUR_CONSUMER_SECRET" \
  -d "username=YOUR_USERNAME" \
  -d "password=YOUR_PASSWORD_AND_SECURITY_TOKEN"
```

**JWT Bearer flow (recommended for production):**
Use a certificate-based JWT assertion for unattended authentication.
See the [Salesforce OAuth 2.0 JWT Bearer Flow](https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oauth_jwt_flow.htm) docs.

### 3. Configure mdk

```bash
# Set the instance URL (your Salesforce org URL)
export SALESFORCE_INSTANCE_URL=https://mycompany.my.salesforce.com

# Store the access token via mdk auth
mdk auth login salesforce
```

Or set both in your project `.env`:

```
SALESFORCE_INSTANCE_URL=https://mycompany.my.salesforce.com
SALESFORCE_ACCESS_TOKEN=your-access-token-here
```

### 4. Reference in agent.yaml

```yaml
tools:
  - salesforce.account.get@1.0.0
  - salesforce.case.create@1.0.0
  - salesforce.contact.search@1.0.0
```

## Required permissions

The Connected App user needs:

- **Read**: Account, Contact, Case objects (for `account.get` and `contact.search`)
- **Write**: Case object (for `case.create`)
- **API Enabled** permission on the user profile

## Environment variables

| Variable                   | Required | Description                                     |
|----------------------------|----------|-------------------------------------------------|
| `SALESFORCE_ACCESS_TOKEN`  | Yes      | Bearer token for the Salesforce REST API.       |
| `SALESFORCE_INSTANCE_URL`  | Yes      | Instance URL of the Salesforce org.             |
