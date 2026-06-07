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

@description('''
Admin password. Pulled from Key Vault by main.bicep, never in source.

LEAVE EMPTY on redeploys of an EXISTING server: an empty value omits the
`administratorLoginPassword` property entirely, and Azure RETAINS the current
password on update. Set it only to (a) create a new server or (b) intentionally
rotate the password. This prevents the footgun where every redeploy reset the
admin password to a param literal that had drifted from the `pg-admin-password`
Key Vault secret the apps authenticate with — silently breaking PG auth for the
whole stack (api/worker/temporal).
''')
@secure()
param adminPassword string = ''

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
  // ``administratorLoginPassword`` is included ONLY when adminPassword is set
  // (union merges it in). On an existing-server update, omitting it makes Azure
  // RETAIN the current password — so redeploys no longer reset it (the footgun).
  properties: union(
    {
      version: postgresVersion
      administratorLogin: adminUsername
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
    },
    empty(adminPassword) ? {} : {
      administratorLoginPassword: adminPassword
    }
  )

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
  resource extensionsAllowlist 'configurations@2023-12-01-preview' = if (enablePgvector || createTemporalDatabases) {
    name: 'azure.extensions'
    properties: {
      // pgvector (KB, ADR 009) PLUS the extensions Temporal's visibility store
      // needs — btree_gin + pg_trgm back its GIN search-attribute indexes
      // (ADR 078). Azure Postgres blocks CREATE EXTENSION until named here;
      // azure.extensions is a dynamic parameter, so this applies without a
      // server restart. The Temporal auto-setup container retries schema setup
      // on its next start once these are allow-listed.
      value: join(
        concat(
          enablePgvector ? ['VECTOR'] : [],
          createTemporalDatabases ? ['BTREE_GIN', 'PG_TRGM'] : []
        ),
        ','
      )
      source: 'user-override'
    }
  }

  // Temporal's all-in-one server opens a persistence pool per internal service
  // (frontend/history/matching/worker/internal-frontend) × store. Even with the
  // small per-pool caps (SQL_MAX_CONNS=3 in containerapp-temporal.bicep) the
  // startup burst peaks ~40 connections, which — plus the api/worker/temporal-
  // worker stack and Azure's superuser-reserved slots — overruns the Burstable
  // default max_connections=50 ("remaining connection slots are reserved for ...
  // SUPERUSER"). Lift the ceiling when Temporal is deployed (B2s's 4 GB supports
  // it comfortably). NOTE: max_connections is RESTART-REQUIRED — after a deploy
  // that changes it, run `az postgres flexible-server restart` for it to apply.
  resource maxConnections 'configurations@2023-12-01-preview' = if (createTemporalDatabases) {
    name: 'max_connections'
    properties: {
      value: '150'
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
