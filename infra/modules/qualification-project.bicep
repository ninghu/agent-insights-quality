param location string = 'westus2'
param accountName string
param projectName string
param applicationInsightsName string
param registryName string
param reportDate string
param expiresOn string
param automationOwner string
param catalogVersion string

var monitoringReaderRoleId = '43d0d8ad-25c7-4714-9337-8ba259a9fe05'
var modelInferenceRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName
}

resource applicationInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: applicationInsightsName
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' existing = {
  name: registryName
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: account
  name: projectName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  tags: {
    purpose: 'agent-insights-quality'
    agentInsightsQualityQualification: 'true'
    reportDate: reportDate
    expiresOn: expiresOn
    automationOwner: automationOwner
    catalogVersion: catalogVersion
  }
  properties: {}
}

resource appInsightsReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: applicationInsights
  name: guid(applicationInsights.id, project.id, monitoringReaderRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', monitoringReaderRoleId)
    principalId: project.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource modelInferenceUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: account
  name: guid(account.id, project.id, modelInferenceRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', modelInferenceRoleId)
    principalId: project.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource registryPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, project.id, acrPullRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: project.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource containerRegistryConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-04-01-preview' = {
  parent: project
  name: 'container-registry'
  properties: {
    category: 'ContainerRegistry'
    target: registry.properties.loginServer
    authType: 'ManagedIdentity'
    credentials: {
      clientId: project.identity.principalId
      resourceId: registry.id
    }
    isSharedToAll: false
    metadata: {
      ResourceId: registry.id
    }
  }
  dependsOn: [
    registryPull
  ]
}

resource appInsightsConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-06-01' = {
  parent: project
  name: 'application-insights'
  properties: {
    category: 'AppInsights'
    target: applicationInsights.id
    authType: 'ApiKey'
    credentials: {
      key: applicationInsights.properties.ConnectionString
    }
    metadata: {
      ApiType: 'Azure'
      ResourceId: applicationInsights.id
      purpose: 'agent-insights-quality'
      owner_reference: automationOwner
      expires_on: expiresOn
    }
  }
  dependsOn: [
    appInsightsReader
    modelInferenceUser
  ]
}
