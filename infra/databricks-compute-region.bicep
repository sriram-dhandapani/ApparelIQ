// Databricks compute region override.
// The subscription has no VM SKUs available in centralus (all SKUs report NotAvailableForSubscription), so classic
// Databricks clusters cannot start there. This template deploys a VNet-injected Premium workspace in a region with
// capacity (default northcentralus; see docs for the preflight check). The data plane keeps using the private ADLS and Azure SQL in centralus through the
// EXISTING private endpoints: global VNet peering to vnet-appareliq + links from the existing private DNS zones.
// Nothing is made public; storage and SQL firewall settings are untouched.

param location string = 'northcentralus'
param prefix string = 'appareliq'

@description('Existing hub VNet (centralus) that hosts the storage and SQL private endpoints.')
param hubVnetName string = 'vnet-appareliq'

param addressPrefix string = '10.41.0.0/16'

param tags object = {
  project: 'ApparelIQ'
  workload: 'uc1-inventory-rebalancing'
  data: 'SYNTHETIC'
}

var sfx = take(uniqueString(resourceGroup().id, prefix), 6)
var region = take(replace(location, 'us', ''), 6)
var dbxName = 'dbw-${prefix}-${region}-${sfx}'

var dnsZones = [
  'privatelink.dfs.${environment().suffixes.storage}'
  'privatelink.blob.${environment().suffixes.storage}'
  'privatelink${environment().suffixes.sqlServerHostname}'
]

resource hubVnet 'Microsoft.Network/virtualNetworks@2024-05-01' existing = {
  name: hubVnetName
}

resource nsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: 'nsg-${prefix}-dbx-${location}'
  location: location
  tags: tags
}

resource pip 'Microsoft.Network/publicIPAddresses@2024-05-01' = {
  name: 'pip-${prefix}-nat-${location}'
  location: location
  tags: tags
  sku: { name: 'Standard' }
  properties: { publicIPAllocationMethod: 'Static' }
}

resource nat 'Microsoft.Network/natGateways@2024-05-01' = {
  name: 'ng-${prefix}-${location}'
  location: location
  tags: tags
  sku: { name: 'Standard' }
  properties: { publicIpAddresses: [ { id: pip.id } ], idleTimeoutInMinutes: 10 }
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: 'vnet-${prefix}-${location}'
  location: location
  tags: tags
  properties: {
    addressSpace: { addressPrefixes: [ addressPrefix ] }
    subnets: [
      {
        name: 'snet-dbx-host'
        properties: {
          addressPrefix: cidrSubnet(addressPrefix, 24, 0)
          networkSecurityGroup: { id: nsg.id }
          natGateway: { id: nat.id }
          defaultOutboundAccess: false
          delegations: [ { name: 'dbx', properties: { serviceName: 'Microsoft.Databricks/workspaces' } } ]
        }
      }
      {
        name: 'snet-dbx-container'
        properties: {
          addressPrefix: cidrSubnet(addressPrefix, 24, 1)
          networkSecurityGroup: { id: nsg.id }
          natGateway: { id: nat.id }
          defaultOutboundAccess: false
          delegations: [ { name: 'dbx', properties: { serviceName: 'Microsoft.Databricks/workspaces' } } ]
        }
      }
    ]
  }
}

resource toHub 'Microsoft.Network/virtualNetworks/virtualNetworkPeerings@2024-05-01' = {
  parent: vnet
  name: 'to-${hubVnetName}'
  properties: {
    remoteVirtualNetwork: { id: hubVnet.id }
    allowVirtualNetworkAccess: true
    allowForwardedTraffic: false
  }
}

resource fromHub 'Microsoft.Network/virtualNetworks/virtualNetworkPeerings@2024-05-01' = {
  parent: hubVnet
  name: 'to-${vnet.name}'
  properties: {
    remoteVirtualNetwork: { id: vnet.id }
    allowVirtualNetworkAccess: true
    allowForwardedTraffic: false
  }
}

resource zones 'Microsoft.Network/privateDnsZones@2024-06-01' existing = [for z in dnsZones: {
  name: z
}]

resource zoneLinks 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = [for (z, i) in dnsZones: {
  parent: zones[i]
  name: 'link-${prefix}-${location}'
  location: 'global'
  properties: { virtualNetwork: { id: vnet.id }, registrationEnabled: false }
}]

resource dbx 'Microsoft.Databricks/workspaces@2024-05-01' = {
  name: dbxName
  location: location
  tags: tags
  sku: { name: 'premium' }
  properties: {
    managedResourceGroupId: subscriptionResourceId('Microsoft.Resources/resourceGroups', 'rg-${prefix}-dbx-${location}-managed')
    publicNetworkAccess: 'Enabled'           // front end (UI/API) only; compute has no public IPs
    requiredNsgRules: 'AllRules'
    parameters: {
      customVirtualNetworkId: { value: vnet.id }
      customPublicSubnetName: { value: 'snet-dbx-host' }
      customPrivateSubnetName: { value: 'snet-dbx-container' }
      enableNoPublicIp: { value: true }
    }
  }
  dependsOn: [ toHub, fromHub, zoneLinks ]
}

output databricksWorkspaceName string = dbx.name
output databricksWorkspaceUrl string = dbx.properties.workspaceUrl
output databricksWorkspaceResourceId string = dbx.id
output vnetName string = vnet.name

