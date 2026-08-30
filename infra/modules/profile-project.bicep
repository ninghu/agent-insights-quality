param location string
param accountName string
param projectName string
param applicationInsightsName string
param registryName string
param automationOwner string
param automationPrincipalId string
@allowed(['daily', 'staging'])
param profile string
param monitoringReaderRoleId string
param modelInferenceRoleId string
param acrPullRoleId string
param foundryProjectManagerRoleId string

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
    automationOwner: automationOwner
    profile: profile
  }
  properties: {}
}

resource automationInsightsReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: applicationInsights
  name: guid(applicationInsights.id, automationPrincipalId, monitoringReaderRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', monitoringReaderRoleId)
    principalId: automationPrincipalId
    principalType: 'User'
  }
}

resource automationProjectManager 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: project
  name: guid(project.id, automationPrincipalId, foundryProjectManagerRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', foundryProjectManagerRoleId)
    principalId: automationPrincipalId
    principalType: 'User'
  }
}

module projectRbac 'project-rbac.bicep' = {
  name: 'project-rbac-${profile}'
  params: {
    accountName: accountName
    applicationInsightsName: applicationInsightsName
    registryName: registryName
    projectPrincipalId: project.identity.principalId
    monitoringReaderRoleId: monitoringReaderRoleId
    modelInferenceRoleId: modelInferenceRoleId
    acrPullRoleId: acrPullRoleId
  }
}

resource registryConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-04-01-preview' = {
  parent: project
  name: 'container-registry-${profile}'
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
  dependsOn: [projectRbac]
}

resource insightsConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-06-01' = {
  parent: project
  name: 'application-insights-${profile}'
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
      profile: profile
    }
  }
  dependsOn: [
    projectRbac
    registryConnection
  ]
}

output projectId string = project.id
output projectPrincipalId string = project.identity.principalId
output connectionIds array = [
  registryConnection.id
  insightsConnection.id
]
output roleAssignmentIds array = [
  automationInsightsReader.id
  automationProjectManager.id
  ...projectRbac.outputs.roleAssignmentIds
]
