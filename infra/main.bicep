targetScope = 'subscription'

param resourceGroupName string = 'agent-insights-quality-rg'
param location string = 'westus2'
param terraModelVersion string
param testAgentModelVersion string
param automationOwner string = 'ninghu'
param automationPrincipalId string
param telemetryGeneration string
@minValue(1)
@maxValue(5000)
param testAgentCapacity int = 5000
@minValue(1)
@maxValue(1000)
param insightGenerationCapacity int = 100
param adxSkuName string = 'Standard_E2ads_v5'
@minValue(2)
param adxCapacity int = 2

resource resourceGroup 'Microsoft.Resources/resourceGroups@2024-11-01' = {
  name: resourceGroupName
  location: location
  tags: {
    purpose: 'agent-insights-quality'
    agentInsightsQualityQualification: 'true'
    automationOwner: automationOwner
  }
}

module lab 'modules/lab.bicep' = {
  name: 'agent-insights-quality-lab'
  scope: resourceGroup
  params: {
    location: location
    terraModelVersion: terraModelVersion
    testAgentModelVersion: testAgentModelVersion
    automationOwner: automationOwner
    automationPrincipalId: automationPrincipalId
    telemetryGeneration: telemetryGeneration
    testAgentCapacity: testAgentCapacity
    insightGenerationCapacity: insightGenerationCapacity
    adxSkuName: adxSkuName
    adxCapacity: adxCapacity
  }
}
