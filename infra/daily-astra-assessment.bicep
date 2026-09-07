targetScope = 'resourceGroup'

@minValue(1)
@maxValue(1000)
param assessmentCapacity int = 1000

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: 'aiq-daily-swedencentral'
}

resource assessmentModel 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: account
  name: 'astra-assessment'
  sku: {
    name: 'GlobalStandard'
    capacity: assessmentCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-6-astra'
      version: '2026-09-03'
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}
