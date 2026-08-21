param location string = 'westus2'
param accountName string
param projectName string
param applicationInsightsName string
param reportDate string
param expiresOn string
param automationOwner string
param catalogVersion string

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
}
