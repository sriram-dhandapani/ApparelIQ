// ApparelIQ Phase 2 step 9: semi-autonomous, collaborative coworker agent runtime (SYNTHETIC demo data only).
// - One user-assigned managed identity is BOTH the Function App identity and the Azure Bot identity (no secrets):
//   Graph app roles (Chat.Create, TeamsAppInstallation.ReadWriteSelfForChat.All, User.Read.All) are granted by
//   scripts/setup_agent.py; Fabric workspace Viewer and the SQL contained user are granted there too.
// - Function App (Flex Consumption, Python 3.11) with VNet integration (snet-func) so it reaches Azure SQL over the
//   private endpoint. Its storage account is private (blob + queue private endpoints) and keyless.
// - Azure Bot (F0, UserAssignedMSI) with the Teams channel; the messaging endpoint is the Function's /api/messages.
// - Key Vault (RBAC) holds the Function key that the Fabric User Data Function (Activator action) reads through its
//   Key Vault generic connection; Fabric has no direct webhook action.

@description('Azure region (must support Flex Consumption).')
param location string = 'centralus'

@description('Short prefix used in resource names.')
param prefix string = 'appareliq'

@description('Object id of the operator who runs scripts/setup_agent.py and owns the Fabric UDF item.')
param operatorObjectId string

@description('Existing Foundry account (infra/foundry.bicep) and AI Search service names.')
param foundryName string
param searchName string

@description('Application settings that are environment specific (from config/deployment.local.json; never in source).')
@secure()
param appSettings object

param funcSubnetPrefix string = '10.40.4.0/24'
param maximumInstanceCount int = 40

param tags object = {
  project: 'ApparelIQ'
  workload: 'uc1-inventory-rebalancing'
  data: 'SYNTHETIC'
}

var sfx = take(uniqueString(resourceGroup().id, prefix), 6)
var identityName = 'id-${prefix}-agent'
var storageName = take('st${prefix}ag${sfx}', 24)
var funcName = 'func-${prefix}-agent-${sfx}'
var planName = 'asp-${prefix}-agent'
var botName = 'bot-${prefix}-agent-${sfx}'
var kvName = take('kv-${prefix}-${sfx}', 24)
var deployContainer = 'app-package'

var roles = {
  storageBlobDataOwner: 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
  storageQueueDataContributor: '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
  storageTableDataContributor: '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
  keyVaultSecretsOfficer: 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'
  azureAIUser: '53ca6127-db72-4b80-b1b0-d745d6d5456d'
  cognitiveServicesOpenAIUser: '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
  searchIndexDataReader: '1407120a-92aa-4202-b7e9-c0e197c71c8f'
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' existing = { name: 'vnet-${prefix}' }
resource nat 'Microsoft.Network/natGateways@2024-05-01' existing = { name: 'ng-${prefix}' }
resource blobZone 'Microsoft.Network/privateDnsZones@2024-06-01' existing = { name: 'privatelink.blob.${environment().suffixes.storage}' }
resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = { name: foundryName }
resource search 'Microsoft.Search/searchServices@2025-05-01' existing = { name: searchName }

// Same definition as infra/main.bicep (keep in sync).
resource funcSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' = {
  parent: vnet
  name: 'snet-func'
  properties: {
    addressPrefix: funcSubnetPrefix
    natGateway: { id: nat.id }
    defaultOutboundAccess: false
    delegations: [ { name: 'flex', properties: { serviceName: 'Microsoft.App/environments' } } ]
  }
}

resource queueZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.queue.${environment().suffixes.storage}'
  location: 'global'
  tags: tags
}

resource queueZoneLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: queueZone
  name: 'link-${prefix}'
  location: 'global'
  properties: { virtualNetwork: { id: vnet.id }, registrationEnabled: false }
}

resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
  tags: tags
}

resource law 'Microsoft.OperationalInsights/workspaces@2025-02-01' = {
  name: 'log-${prefix}-agent'
  location: location
  tags: tags
  properties: { retentionInDays: 30, sku: { name: 'PerGB2018' } }
}

resource appi 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${prefix}-agent'
  location: location
  tags: tags
  kind: 'web'
  properties: { Application_Type: 'web', WorkspaceResourceId: law.id }
}

resource st 'Microsoft.Storage/storageAccounts@2024-01-01' = {
  name: storageName
  location: location
  tags: tags
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    accessTier: 'Hot'
    allowSharedKeyAccess: false
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    publicNetworkAccess: 'Disabled'
    networkAcls: { defaultAction: 'Deny', bypass: 'AzureServices' }
  }
}

resource blobSvc 'Microsoft.Storage/storageAccounts/blobServices@2024-01-01' = {
  parent: st
  name: 'default'
}

resource pkg 'Microsoft.Storage/storageAccounts/blobServices/containers@2024-01-01' = {
  parent: blobSvc
  name: deployContainer
  properties: { publicAccess: 'None' }
}

var peTargets = [
  { group: 'blob', zone: blobZone.id }
  { group: 'queue', zone: queueZone.id }
]

resource pe 'Microsoft.Network/privateEndpoints@2024-05-01' = [for t in peTargets: {
  name: 'pe-${storageName}-${t.group}'
  location: location
  tags: tags
  properties: {
    subnet: { id: '${vnet.id}/subnets/snet-pe' }
    privateLinkServiceConnections: [ {
      name: t.group
      properties: { privateLinkServiceId: st.id, groupIds: [ t.group ] }
    } ]
  }
}]

resource peDns 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = [for (t, i) in peTargets: {
  parent: pe[i]
  name: 'default'
  properties: { privateDnsZoneConfigs: [ { name: t.group, properties: { privateDnsZoneId: t.zone } } ] }
}]

var storageRoles = [roles.storageBlobDataOwner, roles.storageQueueDataContributor, roles.storageTableDataContributor]
resource uamiStorage 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for r in storageRoles: {
  scope: st
  name: guid(st.id, uami.id, r)
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', r)
  }
}]

resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: planName
  location: location
  tags: tags
  kind: 'functionapp'
  sku: { tier: 'FlexConsumption', name: 'FC1' }
  properties: { reserved: true }
}

var baseSettings = {
  AzureWebJobsStorage__accountName: st.name
  AzureWebJobsStorage__credential: 'managedidentity'
  AzureWebJobsStorage__clientId: uami.properties.clientId
  APPLICATIONINSIGHTS_CONNECTION_STRING: appi.properties.ConnectionString
  AZURE_CLIENT_ID: uami.properties.clientId
  MicrosoftAppType: 'UserAssignedMSI'
  MicrosoftAppId: uami.properties.clientId
  MicrosoftAppTenantId: tenant().tenantId
  AGENT_QUEUE_NAME: 'agent-events'
}
var mergedSettings = union(baseSettings, appSettings)

resource func 'Microsoft.Web/sites@2024-04-01' = {
  name: funcName
  location: location
  tags: tags
  kind: 'functionapp,linux'
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${uami.id}': {} } }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    virtualNetworkSubnetId: funcSubnet.id
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${st.properties.primaryEndpoints.blob}${deployContainer}'
          authentication: { type: 'UserAssignedIdentity', userAssignedIdentityResourceId: uami.id }
        }
      }
      scaleAndConcurrency: { maximumInstanceCount: maximumInstanceCount, instanceMemoryMB: 2048 }
      runtime: { name: 'python', version: '3.11' }
    }
    siteConfig: { minTlsVersion: '1.2' }
  }
  dependsOn: [uamiStorage, peDns, queueZoneLink]
}

resource funcSettings 'Microsoft.Web/sites/config@2024-04-01' = {
  parent: func
  name: 'appsettings'
  properties: mergedSettings
}

resource bot 'Microsoft.BotService/botServices@2022-09-15' = {
  name: botName
  location: 'global'
  tags: tags
  kind: 'azurebot'
  sku: { name: 'F0' }
  properties: {
    displayName: 'Marketplace Rebalancing Agent'
    description: 'Kinetiq UC1 coworker agent (SYNTHETIC demo data).'
    endpoint: 'https://${func.properties.defaultHostName}/api/messages'
    msaAppId: uami.properties.clientId
    msaAppType: 'UserAssignedMSI'
    msaAppMSIResourceId: uami.id
    msaAppTenantId: tenant().tenantId
    disableLocalAuth: false
  }
}

resource teams 'Microsoft.BotService/botServices/channels@2022-09-15' = {
  parent: bot
  name: 'MsTeamsChannel'
  location: 'global'
  properties: {
    channelName: 'MsTeamsChannel'
    properties: { isEnabled: true, enableCalling: false }
  }
}

resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: kvName
  location: location
  tags: tags
  properties: {
    tenantId: tenant().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    // Management-group policy (MCAPSGovDeployPolicies / KeyVault_PublicNetwork_Modify) forces this to Disabled, so the
    // Fabric UDF cannot read it. The Activator relay therefore uses the OneLake outbox (D-26) and holds no secret;
    // this vault is currently unused and kept only as the place for any future agent secret.
    publicNetworkAccess: 'Disabled'
  }
}

// Operator can manage secrets if one is ever needed (none are stored today).
resource operatorKv 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: kv
  name: guid(kv.id, operatorObjectId, roles.keyVaultSecretsOfficer)
  properties: {
    principalId: operatorObjectId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsOfficer)
  }
}

// Agent identity: Foundry agents + models, Foundry IQ knowledge base retrieval.
var foundryRoles = [roles.azureAIUser, roles.cognitiveServicesOpenAIUser]
resource uamiFoundry 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for r in foundryRoles: {
  scope: foundry
  name: guid(foundry.id, uami.id, r)
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', r)
  }
}]

resource uamiSearch 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: search
  name: guid(search.id, uami.id, roles.searchIndexDataReader)
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.searchIndexDataReader)
  }
}

output identityName string = uami.name
output identityClientId string = uami.properties.clientId
output identityPrincipalId string = uami.properties.principalId
output identityResourceId string = uami.id
output functionAppName string = func.name
output functionHost string = func.properties.defaultHostName
output botName string = bot.name
output keyVaultName string = kv.name
output keyVaultUri string = kv.properties.vaultUri
output storageName string = st.name
