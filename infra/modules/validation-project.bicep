targetScope = 'resourceGroup'

param location string
param accountName string
param projectName string
param applicationInsightsName string
param registryName string
param validationOperatorPrincipalId string
param ownershipNonce string
param cycleId string
param validationOperatorProjectManagerName string
param appInsightsReaderName string
param modelInferenceUserName string
param registryPullName string

var monitoringReaderRoleId = '43d0d8ad-25c7-4714-9337-8ba259a9fe05'
var modelInferenceRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var foundryProjectManagerRoleId = 'eadc314b-1a2d-4efa-be10-5d325db5065e'

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
    purpose: 'test-agent-validation'
    ownershipNonce: ownershipNonce
    cycleId: cycleId
  }
  properties: {}
}

resource validationOperatorProjectManager 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: project
  name: validationOperatorProjectManagerName
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', foundryProjectManagerRoleId)
    principalId: validationOperatorPrincipalId
    principalType: 'User'
  }
}

module projectRbac 'validation-project-rbac.bicep' = {
  name: 'validation-project-rbac-${uniqueString(project.id, ownershipNonce)}'
  params: {
    accountName: accountName
    applicationInsightsName: applicationInsightsName
    registryName: registryName
    projectPrincipalId: project.identity.principalId
    monitoringReaderRoleId: monitoringReaderRoleId
    modelInferenceRoleId: modelInferenceRoleId
    acrPullRoleId: acrPullRoleId
    appInsightsReaderName: appInsightsReaderName
    modelInferenceUserName: modelInferenceUserName
    registryPullName: registryPullName
  }
}

resource registryConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-04-01-preview' = {
  parent: project
  name: 'container-registry-validation'
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
      ownershipNonce: ownershipNonce
      cycleId: cycleId
    }
  }
  dependsOn: [
    projectRbac
  ]
}

resource insightsConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-06-01' = {
  parent: project
  name: 'application-insights-validation'
  properties: {
    category: 'AppInsights'
    target: applicationInsights.id
    authType: 'ApiKey'
    credentials: {
      key: applicationInsights.properties.ConnectionString
    }
    isSharedToAll: true
    metadata: {
      ApiType: 'Azure'
      ResourceId: applicationInsights.id
    }
  }
  dependsOn: [
    projectRbac
    registryConnection
  ]
}

output projectId string = project.id
output projectPrincipalId string = project.identity.principalId
output projectEndpoint string = 'https://${accountName}.services.ai.azure.com/api/projects/${projectName}'
output connectionIds array = [
  registryConnection.id
  insightsConnection.id
]
output roleAssignmentIds array = [
  validationOperatorProjectManager.id
  ...projectRbac.outputs.roleAssignmentIds
]
