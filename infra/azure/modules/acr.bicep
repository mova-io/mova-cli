// Azure Container Registry — holds the movate runtime image.
// Pull access goes to the ACA managed identity via role assignment
// (created in main.bicep so this module stays focused on the registry).

@description('Name of the ACR (lowercase alphanumeric, 5-50 chars).')
@minLength(5)
@maxLength(50)
param name string

@description('Azure region for the registry.')
param location string

@description('SKU. Basic for dev/staging, Standard for prod.')
@allowed(['Basic', 'Standard', 'Premium'])
param sku string = 'Basic'

@description('Whether to enable the admin user. ONLY for dev — prod uses managed identity.')
param adminUserEnabled bool = false

@description('Common tags.')
param tags object = {}

resource registry 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: sku
  }
  properties: {
    adminUserEnabled: adminUserEnabled
    // Public access stays on (no VNet in v1.0 per docs/v1.0-azure-design §6).
    // Pulls are gated by RBAC, not network.
    publicNetworkAccess: 'Enabled'
  }
}

output loginServer string = registry.properties.loginServer
output registryId string = registry.id
output registryName string = registry.name
