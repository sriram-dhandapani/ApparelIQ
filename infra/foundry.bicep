// ApparelIQ Phase 2 step 8: Foundry IQ (AI-4 policy retrieval over the DS-18 SharePoint corpus).
// - Azure AI Search (Standard, semantic ranker) hosts the Foundry IQ knowledge source + knowledge base.
// - Microsoft Foundry account (AIServices) + project, with gpt-4.1 (agent + KB query planning) and
//   text-embedding-3-small (ingestion vectorizer).
// Keyless everywhere: local auth disabled on both services; managed identities + RBAC only.
// SharePoint Online is not a private-link target, so the indexer's source connection is public by design;
// Search and Foundry stay public-endpoint with Entra-only auth (no keys) for the demo.

@description('Azure region for Foundry and AI Search (must support agentic retrieval).')
param location string = 'centralus'

@description('Short prefix used in resource names.')
param prefix string = 'appareliq'

@allowed(['basic', 'standard'])
param searchSku string = 'standard'

param chatModel string = 'gpt-4.1'
param chatModelVersion string = '2025-04-14'
param chatCapacity int = 50
param embeddingModel string = 'text-embedding-3-small'
param embeddingModelVersion string = '1'
param embeddingCapacity int = 50

@description('Object id of the operator (user) who runs scripts/setup_foundry.py.')
param operatorObjectId string

@allowed(['User', 'Group'])
param operatorPrincipalType string = 'User'

param tags object = {
  project: 'ApparelIQ'
  workload: 'uc1-inventory-rebalancing'
  data: 'SYNTHETIC'
}

var sfx = take(uniqueString(resourceGroup().id, prefix), 6)
var searchName = 'srch-${prefix}-${sfx}'
var foundryName = 'aif-${prefix}-${sfx}'
var projectName = 'proj-${prefix}'

var roles = {
  cognitiveServicesUser: 'a97b65f3-24c7-4388-baec-2e87135dc908'
  cognitiveServicesOpenAIUser: '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
  azureAIUser: '53ca6127-db72-4b80-b1b0-d745d6d5456d'
  searchIndexDataReader: '1407120a-92aa-4202-b7e9-c0e197c71c8f'
  searchIndexDataContributor: '8ebe5a00-799e-43f5-93ac-243d3dce84a7'
  searchServiceContributor: '7ca78c08-252a-4471-8644-bb5ff32d4ba0'
}

resource search 'Microsoft.Search/searchServices@2025-05-01' = {
  name: searchName
  location: location
  tags: tags
  sku: { name: searchSku }
  identity: { type: 'SystemAssigned' }
  properties: {
    replicaCount: 1
    partitionCount: 1
    hostingMode: 'Default'
    semanticSearch: 'standard'
    disableLocalAuth: true
    publicNetworkAccess: 'enabled'
  }
}

resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: foundryName
  location: location
  tags: tags
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    customSubDomainName: foundryName
    allowProjectManagement: true
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: foundry
  name: projectName
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: {
    displayName: 'ApparelIQ Marketplace Rebalancing'
    description: 'UC1 Marketplace Rebalancing Agent (synthetic Kinetiq data).'
  }
}

resource chat 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundry
  name: chatModel
  sku: { name: 'GlobalStandard', capacity: chatCapacity }
  properties: {
    model: { format: 'OpenAI', name: chatModel, version: chatModelVersion }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
  dependsOn: [project]
}

resource embedding 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundry
  name: embeddingModel
  sku: { name: 'GlobalStandard', capacity: embeddingCapacity }
  properties: {
    model: { format: 'OpenAI', name: embeddingModel, version: embeddingModelVersion }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
  dependsOn: [chat]
}

// Project -> Search connection so Foundry IQ / agents can use the knowledge base with the project identity.
resource searchConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-06-01' = {
  parent: project
  name: searchName
  properties: {
    category: 'CognitiveSearch'
    target: 'https://${searchName}.search.windows.net'
    authType: 'AAD'
    isSharedToAll: true
    metadata: {
      ApiType: 'Azure'
      ResourceId: search.id
      location: location
    }
  }
  dependsOn: [embedding]
}

// Search MI calls the embedding model during ingestion and the chat model for KB query planning.
resource searchToFoundry 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: foundry
  name: guid(foundry.id, search.id, roles.cognitiveServicesUser)
  properties: {
    principalId: search.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.cognitiveServicesUser)
  }
}

// Foundry project MI reads the knowledge base (agent tool calls).
resource projectToSearch 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: search
  name: guid(search.id, project.id, roles.searchIndexDataReader)
  properties: {
    principalId: project.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.searchIndexDataReader)
  }
}

// Operator: manage knowledge sources/bases, load and query data, and use the Foundry project.
var operatorSearchRoles = [roles.searchServiceContributor, roles.searchIndexDataContributor]
resource operatorSearch 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for r in operatorSearchRoles: {
  scope: search
  name: guid(search.id, operatorObjectId, r)
  properties: {
    principalId: operatorObjectId
    principalType: operatorPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', r)
  }
}]

var operatorFoundryRoles = [roles.azureAIUser, roles.cognitiveServicesOpenAIUser]
resource operatorFoundry 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for r in operatorFoundryRoles: {
  scope: foundry
  name: guid(foundry.id, operatorObjectId, r)
  properties: {
    principalId: operatorObjectId
    principalType: operatorPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', r)
  }
}]

output searchName string = search.name
output searchEndpoint string = 'https://${search.name}.search.windows.net'
output searchPrincipalId string = search.identity.principalId
output foundryName string = foundry.name
output foundryEndpoint string = 'https://${foundry.name}.services.ai.azure.com'
output openAIEndpoint string = 'https://${foundry.name}.openai.azure.com'
output projectName string = project.name
output projectEndpoint string = 'https://${foundry.name}.services.ai.azure.com/api/projects/${project.name}'
output projectPrincipalId string = project.identity.principalId
output chatDeployment string = chat.name
output embeddingDeployment string = embedding.name
