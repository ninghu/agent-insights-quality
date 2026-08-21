param location string = 'westus2'
param accountName string
param projectName string
param applicationInsightsName string
param reportDate string
param expiresOn string
param automationOwner string
param catalogVersion string

var monitoringReaderRoleId = '43d0d8ad-25c7-4714-9337-8ba259a9fe05'
var modelInferenceRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName
}

resource applicationInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: applicationInsightsName
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

resource appInsightsConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-06-01' = {
  parent: project
  name: 'application-insights'
  properties: {
    category: 'AppInsights'
    target: applicationInsights.id
    authType: 'AAD'
    metadata: {
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
