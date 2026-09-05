param accountName string
param modelVersion string
@minValue(1)
@maxValue(1000)
param capacity int

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName
}

resource assessmentModel 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: account
  name: 'sol-assessment'
  sku: {
    name: 'DataZoneStandard'
    capacity: capacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-5.6-sol'
      version: modelVersion
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}
