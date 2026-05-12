// Azure Communication Services (ACS) — outbound SMS notifications.
//
// Provisions the ACS resource (the "service" namespace). The phone
// number itself is provisioned out-of-band — Bicep can't reliably
// declare it because toll-free SMS numbers must be search-purchased
// via the ACS REST API. Operators run:
//
//   az communication phonenumber list-available --service <name> \
//       --country-code US --phone-number-type tollFree \
//       --assignment-type application --capabilities sms=outbound
//
//   az communication phonenumber purchase --service <name> --search-id <id>
//
// (or use the Azure Portal's "Phone numbers → Get" flow). Then:
//   1. Copy the resulting E.164 number into the bicepparam file
//      (``acsFromNumber`` param on main.bicep) — non-secret, lives
//      alongside ``image`` and other deploy-time inputs.
//   2. Copy the connection string from the resource's Keys blade
//      into Key Vault as ``acs-connection-string`` — operator does
//      this once, same pattern as ``openai-api-key`` etc.
//
// See docs/v1.0-azure-design.md §10 for the vendor decision and
// docs/azure-bootstrap.md for the runbook.

@description('Name of the ACS resource (4-63 chars).')
@minLength(4)
@maxLength(63)
param name string

@description('''
Data location for the ACS resource. ACS resources are GLOBAL (no
``location`` field), but each one MUST pick a single data location
that scopes where customer data lives. ``United States`` is the
correct default for US-customer movate deployments. Change ONLY if
the customer base shifts.
''')
@allowed([
  'Africa'
  'Asia Pacific'
  'Australia'
  'Brazil'
  'Canada'
  'Europe'
  'France'
  'Germany'
  'India'
  'Japan'
  'Korea'
  'Norway'
  'Switzerland'
  'UAE'
  'United Kingdom'
  'United States'
])
param dataLocation string = 'United States'

@description('Common tags.')
param tags object = {}

resource acs 'Microsoft.Communication/communicationServices@2023-04-01' = {
  name: name
  // ACS is a global service — `location` MUST be the literal 'global'
  // string. `dataLocation` (below) is the per-resource data-residency
  // pin and is the field operators actually care about.
  location: 'global'
  tags: tags
  properties: {
    dataLocation: dataLocation
  }
}

@description('ACS resource id (for role assignments / cross-ref).')
output resourceId string = acs.id

@description('ACS resource name (operators reference this in `az communication ...` commands).')
output resourceName string = acs.name

@description('''
Hostname operators use when constructing the connection string by
hand. The actual connection string is sensitive (contains the access
key) and MUST be copied from the resource's Keys blade into Key
Vault — Bicep deliberately does NOT surface ``listKeys(acs.id, ...).primaryConnectionString``
as an output because that would write the secret to the deployment
output history.
''')
output hostName string = acs.properties.hostName
