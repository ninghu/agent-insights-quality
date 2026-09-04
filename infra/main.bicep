targetScope = 'subscription'

param resourceGroupName string = 'agent-insights-quality-rg'
@allowed(['swedencentral'])
param location string = 'swedencentral'
@allowed(['2026-07-09'])
param terraModelVersion string = '2026-07-09'
@allowed(['2026-03-17'])
param testAgentModelVersion string = '2026-03-17'
param automationOwner string = 'ninghu'
param automationPrincipalId string
@allowed(['g30'])
param telemetryGeneration string = 'g30'
@minValue(1)
@maxValue(5000)
param testAgentCapacity int = 4500
@minValue(1)
@maxValue(1000)
param insightGenerationCapacity int = 100
@allowed(['aiqsweart'])
param storageAccountPrefix string = 'aiqsweart'
@allowed(['qualification-storage'])
param storageResourceRole string = 'qualification-storage'
@allowed(['quality-artifacts'])
param qualityArtifactContainerName string = 'quality-artifacts'
@allowed(['deployment-registries'])
param deploymentRegistryContainerName string = 'deployment-registries'

resource resourceGroup 'Microsoft.Resources/resourceGroups@2024-11-01' existing = {
  name: resourceGroupName
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
    storageAccountPrefix: storageAccountPrefix
    storageResourceRole: storageResourceRole
    qualityArtifactContainerName: qualityArtifactContainerName
    deploymentRegistryContainerName: deploymentRegistryContainerName
  }
}
