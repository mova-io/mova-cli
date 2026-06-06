// Postgres Flexible Server — backs the movate jobs / runs / api_keys
// tables. Public IP + firewall in v1.0 (no VNet — see docs/v1.0-azure-design §6).

@description('Server name (3-63 chars, lowercase, hyphens, must end alphanumeric).')
@minLength(3)
@maxLength(63)
param name string

@description('Azure region.')
param location string

@description('Postgres major version. Match what the asyncpg driver supports + LTS.')
@allowed(['14', '15', '16', '17'])
param postgresVersion string = '16'

@description('SKU tier — Burstable for dev/staging, GeneralPurpose for prod.')
@allowed(['Burstable', 'GeneralPurpose', 'MemoryOptimized'])
param skuTier string = 'Burstable'

@description('SKU name. e.g. Standard_B1ms (1 vCore, 2 GB) for Burstable, Standard_D2ds_v5 (2 vCore, 8 GB) for GP.')
param skuName string = 'Standard_B1ms'

@description('Storage in GB. Min 32; expand later non-disruptively.')
@minValue(32)
@maxValue(16384)
param storageSizeGB int = 32

@description('Backup retention in days.')
@minValue(7)
@maxValue(35)
param backupRetentionDays int = 7

@description('Database admin username.')
param adminUsername string = 'movateadmin'

@description('Admin password — pulled from Key Vault by main.bicep, never in source.')
@secure()
param adminPassword string

@description('Default database name to create.')
param databaseName string = 'movate'

@description('Also create a `langfuse` database on this server (for self-hosted Langfuse). Off by default so non-Langfuse deploys stay lean.')
param createLangfuseDatabase bool = false

@description('Also create `temporal` + `temporal_visibility` databases on this server (for the self-hosted Temporal server, ADR 078). Off by default so non-Temporal deploys stay lean.')
param createTemporalDatabases bool = false

@description('Allow-list the pgvector extension (Azure blocks extensions until named in the azure.extensions server parameter). Required for the KB vector store — see docs/adr/009-pgvector-kb-storage.md.')
param enablePgvector bool = true

@description('Common tags.')
param tags object = {}

resource server 'Microsoft.DBforPostgreSQL/flexibleServers@2023-12-01-preview' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: skuName
    tier: skuTier
  }
  properties: {
    version: postgresVersion
    administratorLogin: adminUsername
    administratorLoginPassword: adminPassword
    storage: {
      storageSizeGB: storageSizeGB
      // Auto-grow ON: never page an operator at 3am because the disk
      // filled up. Costs slightly more than a fixed disk that's right-sized,
      // but page-prevention is worth it.
      autoGrow: 'Enabled'
    }
    backup: {
      backupRetentionDays: backupRetentionDays
      geoRedundantBackup: 'Disabled' // v1.0 single-region; revisit at v1.1
    }
    network: {
      // Public access; firewall rules below restrict to Azure Services.
      publicNetworkAccess: 'Enabled'
    }
    highAvailability: {
      // Burstable doesn't support HA; GP does. Operators flip this when
      // they upgrade SKU.
      mode: 'Disabled'
    }
    authConfig: {
      // Password auth for now. Microsoft Entra ID auth is a v1.1 win
      // (eliminates the admin password entirely).
      passwordAuth: 'Enabled'
      activeDirectoryAuth: 'Disabled'
    }
  }

  resource db 'databases@2023-12-01-preview' = {
    name: databaseName
    properties: {
      charset: 'UTF8'
      collation: 'en_US.utf8'
    }
  }

  // Dedicated database for self-hosted Langfuse (its Prisma schema lives
  // here, isolated from the movate app tables). Created only when
  // createLangfuseDatabase=true.
  resource langfuseDb 'databases@2023-12-01-preview' = if (createLangfuseDatabase) {
    name: 'langfuse'
    properties: {
      charset: 'UTF8'
      collation: 'en_US.utf8'
    }
  }

  // Dedicated databases for the self-hosted Temporal server (ADR 078 D3): the
  // default (history) store + the standard SQL visibility store, isolated from
  // the movate app tables. temporalio/auto-setup runs its (idempotent) schema
  // setup against these on boot. Created only when createTemporalDatabases=true.
  resource temporalDb 'databases@2023-12-01-preview' = if (createTemporalDatabases) {
    name: 'temporal'
    properties: {
      charset: 'UTF8'
      collation: 'en_US.utf8'
    }
  }

  resource temporalVisibilityDb 'databases@2023-12-01-preview' = if (createTemporalDatabases) {
    name: 'temporal_visibility'
    properties: {
      charset: 'UTF8'
      collation: 'en_US.utf8'
    }
  }

  // Allow Azure-internal traffic (which includes ACA outbound IPs).
  // For v1.0 this is the simplest secure-enough story; fine-grained
  // ACA-IP-pinning lands when there's a security review.
  resource fwAzureInternal 'firewallRules@2023-12-01-preview' = {
    name: 'AllowAllAzureServices'
    properties: {
      startIpAddress: '0.0.0.0'
      endIpAddress: '0.0.0.0'
    }
  }

  // Allow-list the pgvector extension. Azure Postgres Flexible Server
  // refuses `CREATE EXTENSION vector` until the extension is named in the
  // `azure.extensions` server parameter; the runtime creates the extension
  // at boot (PostgresProvider._ensure_pgvector). `azure.extensions` is a
  // dynamic parameter, so this takes effect without a server restart.
  // See docs/adr/009-pgvector-kb-storage.md.
  resource extensionsAllowlist 'configurations@2023-12-01-preview' = if (enablePgvector) {
    name: 'azure.extensions'
    properties: {
      value: 'VECTOR'
      source: 'user-override'
    }
  }
}

@description('Server FQDN — used to build MDK_DB_URL.')
output serverFqdn string = server.properties.fullyQualifiedDomainName

@description('Server resource id.')
output serverId string = server.id

@description('Database name (echoed for output composition).')
output databaseName string = databaseName

@description('Admin username (echoed for output composition).')
output adminUsername string = adminUsername
