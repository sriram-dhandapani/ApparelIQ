// ApparelIQ Phase 2 infrastructure (tasks 0-2).
// Private ADLS Gen2 for Unity Catalog, VNet-injected Azure Databricks (Premium), and a private Azure SQL ERP/WMS.
// Nothing here is publicly reachable:
//   - ADLS: firewall default Deny, no IP rules, no trusted-service bypass. Access only via private endpoints
//     (Databricks classic compute) and resource instance rules (Databricks access connector, Fabric workspace).
//   - Azure SQL: public network access Disabled. Access only via private endpoint (Databricks, Fabric VNet gateway).

@description('Azure region for all resources.')
param location string = 'centralus'

@description('Short prefix used in resource names.')
param prefix string = 'appareliq'

@description('Entra object ID of the SQL Entra admin (user or group).')
param sqlAdminObjectId string

@description('Login (UPN or group name) of the SQL Entra admin.')
param sqlAdminLogin string

@allowed(['User', 'Group', 'Application'])
param sqlAdminPrincipalType string = 'User'

@description('Fabric workspace ID allowed through the storage firewall (resource instance rule). Empty until the workspace exists.')
param fabricWorkspaceId string = ''

@description('Object id of the Fabric workspace identity service principal; granted Storage Blob Data Reader for trusted workspace access. Empty to skip.')
param fabricWorkspaceIdentityObjectId string = ''

@description('Temporarily Enabled only while seeding from an allow-listed client IP; Disabled otherwise.')
@allowed(['Enabled', 'Disabled'])
param sqlPublicNetworkAccess string = 'Disabled'

@description('Azure SQL database SKU. GP_S_Gen5 = General Purpose serverless.')
param sqlSkuName string = 'GP_S_Gen5'
param sqlSkuCapacity int = 2

@description('Deploy the Databricks workspace in this (hub) region. False when the subscription has no VM capacity here; use databricks-compute-region.bicep instead.')
param deployDatabricksInHub bool = false

param tags object = {
  project: 'ApparelIQ'
  workload: 'uc1-inventory-rebalancing'
  data: 'SYNTHETIC'
}

var sfx = take(uniqueString(resourceGroup().id, prefix), 6)
var tenantId = subscription().tenantId
var storageName = take('st${prefix}${sfx}', 24)
var dbxName = 'dbw-${prefix}-${sfx}'
var sqlServerName = 'sql-${prefix}-${sfx}'
var sqlDbName = 'sqldb-${prefix}-erpwms'
var blobDataContributor = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var blobDataReader = '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'

// ---------------- Network ----------------
resource nsgDbx 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: 'nsg-${prefix}-dbx'
  location: location
  tags: tags
}

resource pipNat 'Microsoft.Network/publicIPAddresses@2024-05-01' = {
  name: 'pip-${prefix}-nat'
  location: location
  tags: tags
  sku: { name: 'Standard' }
  properties: { publicIPAllocationMethod: 'Static' }
}

// Explicit egress for no-public-IP Databricks clusters (default outbound access is not relied on).
resource nat 'Microsoft.Network/natGateways@2024-05-01' = {
  name: 'ng-${prefix}'
  location: location
  tags: tags
  sku: { name: 'Standard' }
  properties: { publicIpAddresses: [ { id: pipNat.id } ], idleTimeoutInMinutes: 10 }
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: 'vnet-${prefix}'
  location: location
  tags: tags
  properties: {
    addressSpace: { addressPrefixes: [ '10.40.0.0/16' ] }
    subnets: [
      {
        name: 'snet-dbx-host'
        properties: {
          addressPrefix: '10.40.0.0/24'
          networkSecurityGroup: { id: nsgDbx.id }
          natGateway: { id: nat.id }
          defaultOutboundAccess: false
          delegations: [ { name: 'dbx', properties: { serviceName: 'Microsoft.Databricks/workspaces' } } ]
        }
      }
      {
        name: 'snet-dbx-container'
        properties: {
          addressPrefix: '10.40.1.0/24'
          networkSecurityGroup: { id: nsgDbx.id }
          natGateway: { id: nat.id }
          defaultOutboundAccess: false
          delegations: [ { name: 'dbx', properties: { serviceName: 'Microsoft.Databricks/workspaces' } } ]
        }
      }
      {
        name: 'snet-pe'
        properties: {
          addressPrefix: '10.40.2.0/24'
          defaultOutboundAccess: false
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
      {
        // Fabric / Power Platform virtual network data gateway (Azure SQL mirroring).
        name: 'snet-fabric-gw'
        properties: {
          addressPrefix: '10.40.3.0/27'
          defaultOutboundAccess: false
          delegations: [ { name: 'pp', properties: { serviceName: 'Microsoft.PowerPlatform/vnetaccesslinks' } } ]
        }
      }
      {
        // Step 9 coworker agent: Flex Consumption VNet integration (reaches Azure SQL privately; NAT for Graph/Teams/Fabric).
        // Also declared in infra/agent.bicep; keep both in sync so re-deploying this file does not drop it.
        name: 'snet-func'
        properties: {
          addressPrefix: '10.40.4.0/24'
          natGateway: { id: nat.id }
          defaultOutboundAccess: false
          delegations: [ { name: 'flex', properties: { serviceName: 'Microsoft.App/environments' } } ]
        }
      }
    ]
  }
}

var peSubnetId = '${vnet.id}/subnets/snet-pe'

var dnsZones = [
  'privatelink.dfs.${environment().suffixes.storage}'
  'privatelink.blob.${environment().suffixes.storage}'
  'privatelink${environment().suffixes.sqlServerHostname}'
]

resource zones 'Microsoft.Network/privateDnsZones@2024-06-01' = [for z in dnsZones: {
  name: z
  location: 'global'
  tags: tags
}]

resource zoneLinks 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = [for (z, i) in dnsZones: {
  parent: zones[i]
  name: 'link-${prefix}'
  location: 'global'
  properties: { virtualNetwork: { id: vnet.id }, registrationEnabled: false }
}]

// ---------------- Databricks ----------------
resource accessConnector 'Microsoft.Databricks/accessConnectors@2024-05-01' = {
  name: 'dbac-${prefix}-${sfx}'
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: {}
}

resource dbx 'Microsoft.Databricks/workspaces@2024-05-01' = if (deployDatabricksInHub) {
  name: dbxName
  location: location
  tags: tags
  sku: { name: 'premium' }
  properties: {
    managedResourceGroupId: subscriptionResourceId('Microsoft.Resources/resourceGroups', 'rg-${prefix}-dbx-managed')
    publicNetworkAccess: 'Enabled'           // front end (UI/API) only; compute has no public IPs
    requiredNsgRules: 'AllRules'
    parameters: {
      customVirtualNetworkId: { value: vnet.id }
      customPublicSubnetName: { value: 'snet-dbx-host' }
      customPrivateSubnetName: { value: 'snet-dbx-container' }
      enableNoPublicIp: { value: true }
    }
  }
}

// ---------------- ADLS Gen2 (Unity Catalog storage) ----------------
var baseRules = [ { tenantId: tenantId, resourceId: accessConnector.id } ]
var fabricRule = empty(fabricWorkspaceId) ? [] : [ {
  tenantId: tenantId
  resourceId: '/subscriptions/00000000-0000-0000-0000-000000000000/resourcegroups/Fabric/providers/Microsoft.Fabric/workspaces/${fabricWorkspaceId}'
} ]

resource st 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  tags: tags
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    isHnsEnabled: true
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: 'Enabled'           // required for resource instance rules; default action is Deny
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'None'
      ipRules: []
      virtualNetworkRules: []
      resourceAccessRules: concat(baseRules, fabricRule)
    }
  }
}

resource blobSvc 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: st
  name: 'default'
}

resource ucContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobSvc
  name: 'unity'
  properties: { publicAccess: 'None' }
}

resource acStorageRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: st
  name: guid(st.id, accessConnector.id, blobDataContributor)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobDataContributor)
    principalId: accessConnector.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource fabricStorageRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(fabricWorkspaceIdentityObjectId)) {
  scope: st
  name: guid(st.id, fabricWorkspaceIdentityObjectId, blobDataReader)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobDataReader)
    principalId: fabricWorkspaceIdentityObjectId
    principalType: 'ServicePrincipal'
  }
}

resource peStorage 'Microsoft.Network/privateEndpoints@2024-05-01' = [for (g, i) in [ 'dfs', 'blob' ]: {
  name: 'pe-${storageName}-${g}'
  location: location
  tags: tags
  properties: {
    subnet: { id: peSubnetId }
    privateLinkServiceConnections: [ { name: g, properties: { privateLinkServiceId: st.id, groupIds: [ g ] } } ]
  }
}]

resource peStorageDns 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = [for i in range(0, 2): {
  parent: peStorage[i]
  name: 'default'
  properties: { privateDnsZoneConfigs: [ { name: 'z', properties: { privateDnsZoneId: zones[i].id } } ] }
}]

// ---------------- Azure SQL (ERP / WMS stand-in) ----------------
resource sql 'Microsoft.Sql/servers@2023-08-01' = {
  name: sqlServerName
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }      // required (primary identity) for Fabric mirroring
  properties: {
    minimalTlsVersion: '1.2'
    publicNetworkAccess: sqlPublicNetworkAccess
    administrators: {
      administratorType: 'ActiveDirectory'
      principalType: sqlAdminPrincipalType
      login: sqlAdminLogin
      sid: sqlAdminObjectId
      tenantId: tenantId
      azureADOnlyAuthentication: true
    }
  }
}

resource sqlDb 'Microsoft.Sql/servers/databases@2023-08-01' = {
  parent: sql
  name: sqlDbName
  location: location
  tags: tags
  sku: { name: sqlSkuName, tier: startsWith(sqlSkuName, 'GP') ? 'GeneralPurpose' : 'Standard', family: 'Gen5', capacity: sqlSkuCapacity }
  properties: {
    autoPauseDelay: startsWith(sqlSkuName, 'GP_S') ? 60 : null
    minCapacity: startsWith(sqlSkuName, 'GP_S') ? json('0.5') : null
    zoneRedundant: false
    requestedBackupStorageRedundancy: 'Local'
  }
}

resource peSql 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: 'pe-${sqlServerName}'
  location: location
  tags: tags
  properties: {
    subnet: { id: peSubnetId }
    privateLinkServiceConnections: [ { name: 'sql', properties: { privateLinkServiceId: sql.id, groupIds: [ 'sqlServer' ] } } ]
  }
}

resource peSqlDns 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: peSql
  name: 'default'
  properties: { privateDnsZoneConfigs: [ { name: 'z', properties: { privateDnsZoneId: zones[2].id } } ] }
}

output storageAccountName string = st.name
output storageDfsEndpoint string = st.properties.primaryEndpoints.dfs
output accessConnectorId string = accessConnector.id
output databricksWorkspaceName string = deployDatabricksInHub ? dbx.name : ''
output databricksWorkspaceUrl string = deployDatabricksInHub ? dbx!.properties.workspaceUrl : ''
output databricksWorkspaceResourceId string = deployDatabricksInHub ? dbx.id : ''
output sqlServerFqdn string = sql.properties.fullyQualifiedDomainName
output sqlServerResourceId string = sql.id
output sqlDatabaseName string = sqlDb.name
output vnetName string = vnet.name
output fabricGatewaySubnet string = 'snet-fabric-gw'
