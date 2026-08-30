param accountName string
param applicationInsightsName string
param registryName string
param projectPrincipalId string
param monitoringReaderRoleId string
param modelInferenceRoleId string
param acrPullRoleId string
param appInsightsReaderName string
param modelInferenceUserName string
param registryPullName string

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName
}

resource applicationInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: applicationInsightsName
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' existing = {
  name: registryName
}

resource appInsightsReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: applicationInsights
  name: appInsightsReaderName
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', monitoringReaderRoleId)
    principalId: projectPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource modelInferenceUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: account
  name: modelInferenceUserName
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', modelInferenceRoleId)
    principalId: projectPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource registryPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: registryPullName
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: projectPrincipalId
    principalType: 'ServicePrincipal'
  }
}

output roleAssignmentIds array = [
  appInsightsReader.id
  modelInferenceUser.id
  registryPull.id
]
