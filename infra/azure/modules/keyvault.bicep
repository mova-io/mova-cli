// Key Vault — holds Postgres admin password + provider API keys.
// Container Apps reference these via Key Vault references with
// system-assigned managed identity (no static credentials anywhere).

@description('Name of the vault (3-24 chars, globally unique).')
@minLength(3)
@maxLength(24)
param name string

@description('Azure region.')
param location string

@description('Tenant id (subscription tenant). Pass az account show --query tenantId.')
param tenantId string = subscription().tenantId

@description('Common tags.')
param tags object = {}

@description('Public network access. v1.0 has no VNet so this stays Enabled with KV firewall.')
@allowed(['Enabled', 'Disabled'])
param publicNetworkAccess string = 'Enabled'

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: tenantId
    // RBAC mode (NOT access policies) — modern, integrates with
    // Azure RBAC inheritance. ACA managed identity gets the
    // "Key Vault Secrets User" role assigned in main.bicep.
    enableRbacAuthorization: true
    // Soft-delete is mandatory in 2024+. 90 days is the default;
    // shorten only if the operator has a specific reason.
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    // Don't allow purge for prod safety. Operators who need to
    // recreate a deleted vault must wait out soft-delete.
    enablePurgeProtection: true
    publicNetworkAccess: publicNetworkAccess
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
  }
}

output vaultId string = vault.id
output vaultName string = vault.name
output vaultUri string = vault.properties.vaultUri
