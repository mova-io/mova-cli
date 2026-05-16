// Azure Storage Account + File Share — shared agents volume for movate.
//
// Mounts at /home/movate/agents on both the API and worker pods so
// wizard-created agents are visible across replicas and pods. The share
// is consumed by the Container Apps Environment via a
// ``Microsoft.App/managedEnvironments/storages`` binding declared in
// main.bicep (requires the storage account key via listKeys()).
//
// Disabled by default (useAzureFiles=false in main.bicep). Enable for
// staging and prod where multi-replica / multi-pod consistency matters.
// Dev can stay on pod-local /app/agents since it runs a single replica.

@description('Storage account name. Must be 3-24 chars, lowercase alphanumeric. Globally unique.')
param name string

@description('Azure region.')
param location string

@description('Azure Files share name.')
param shareName string = 'movate-agents'

@description('Share quota in GiB. 100 GiB comfortably fits hundreds of agent bundles.')
param shareQuotaGiB int = 100

@description('Common tags.')
param tags object = {}

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: name
  location: location
  tags: tags
  sku: {
    // LRS replication is sufficient — agent bundles are source-controlled
    // and can be redeployed. ZRS/GRS adds cost with no material benefit
    // for this use-case.
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    // Disable shared-key auth for blobs/queues but keep it for Files —
    // ACA's managed-environment storage binding uses a shared key
    // (the alternative, MI-based NFS, requires Premium tier).
    // ``allowSharedKeyAccess: true`` is the default; explicit here for
    // auditability.
    allowSharedKeyAccess: true
  }
}

// The file service child resource must be named 'default'.
resource fileService 'Microsoft.Storage/storageAccounts/fileServices@2023-01-01' = {
  parent: storageAccount
  name: 'default'
}

resource fileShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-01-01' = {
  parent: fileService
  name: shareName
  properties: {
    shareQuota: shareQuotaGiB
    // Transaction-optimized is the default tier for standard accounts.
    // Hot/Cool tiers apply to Blob, not Files.
    enabledProtocols: 'SMB'
  }
}

@description('Storage account name — pass to listKeys() in main.bicep to get the account key.')
output storageAccountName string = storageAccount.name

@description('Storage account resource id — for constructing the listKeys() call.')
output storageAccountId string = storageAccount.id

@description('File share name.')
output shareName string = shareName
