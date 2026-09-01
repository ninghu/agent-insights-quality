param location string
param terraModelVersion string
param testAgentModelVersion string
param automationOwner string
param automationPrincipalId string
param telemetryGeneration string
param testAgentCapacity int
param insightGenerationCapacity int
param approvedRecordContainerName string

var commonTags = {
  purpose: 'agent-insights-quality'
  agentInsightsQualityQualification: 'true'
  automationOwner: automationOwner
  environment: 'swedencentral'
  location: 'swedencentral'
  generation: telemetryGeneration
}
var uniqueSuffix = substring(uniqueString(subscription().subscriptionId, resourceGroup().id), 0, 11)
var dailyAccountName = 'aiq-daily-swedencentral'
var stagingAccountName = 'aiq-staging-swedencentral'
var registryName = 'aiqacr${uniqueSuffix}'
var storageName = 'aiqartifacts${uniqueSuffix}'
var monitoringReaderRoleId = '43d0d8ad-25c7-4714-9337-8ba259a9fe05'
var modelInferenceRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var acrPushRoleId = '8311e382-0749-4cb8-b61a-304f252e45ec'
var acrDeleteRoleId = 'c2f4ef07-c644-48eb-af81-4b1b4947fb11'
var blobContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var foundryProjectManagerRoleId = 'eadc314b-1a2d-4efa-be10-5d325db5065e'

resource dailyWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'aiq-daily-${telemetryGeneration}-law-${uniqueSuffix}'
  location: location
  tags: union(commonTags, { profile: 'daily' })
  properties: {
    retentionInDays: 90
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
    }
  }
}

resource stagingWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'aiq-staging-${telemetryGeneration}-law-${uniqueSuffix}'
  location: location
  tags: union(commonTags, { profile: 'staging' })
  properties: {
    retentionInDays: 90
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
    }
  }
}

resource dailyInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'aiq-daily-${telemetryGeneration}-appi-${uniqueSuffix}'
  location: location
  kind: 'web'
  tags: union(commonTags, { profile: 'daily' })
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: dailyWorkspace.id
  }
}

resource stagingInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'aiq-staging-${telemetryGeneration}-appi-${uniqueSuffix}'
  location: location
  kind: 'web'
  tags: union(commonTags, { profile: 'staging' })
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: stagingWorkspace.id
  }
}

resource dailyAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: dailyAccountName
  location: location
  kind: 'AIServices'
  tags: union(commonTags, { profile: 'daily' })
  identity: {
    type: 'SystemAssigned'
  }
  sku: {
    name: 'S0'
  }
  properties: {
    customSubDomainName: dailyAccountName
    publicNetworkAccess: 'Enabled'
    allowProjectManagement: true
  }
}

resource dailyTestAgentsModel 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: dailyAccount
  name: 'gpt-5.4-mini'
  sku: {
    name: 'DataZoneStandard'
    capacity: testAgentCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-5.4-mini'
      version: testAgentModelVersion
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}

resource dailyInsightGenerationModel 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: dailyAccount
  name: 'terra-insight-generation'
  sku: {
    name: 'DataZoneStandard'
    capacity: insightGenerationCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-5.6-terra'
      version: terraModelVersion
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
  dependsOn: [
    dailyTestAgentsModel
  ]
}

resource stagingAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: stagingAccountName
  location: location
  kind: 'AIServices'
  tags: union(commonTags, { profile: 'staging' })
  identity: {
    type: 'SystemAssigned'
  }
  sku: {
    name: 'S0'
  }
  properties: {
    customSubDomainName: stagingAccountName
    publicNetworkAccess: 'Enabled'
    allowProjectManagement: true
  }
}

resource stagingTestAgentsModel 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: stagingAccount
  name: 'gpt-5.4-mini'
  sku: {
    name: 'DataZoneStandard'
    capacity: testAgentCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-5.4-mini'
      version: testAgentModelVersion
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}

resource stagingInsightGenerationModel 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: stagingAccount
  name: 'terra-insight-generation'
  sku: {
    name: 'DataZoneStandard'
    capacity: insightGenerationCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-5.6-terra'
      version: terraModelVersion
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
  dependsOn: [
    stagingTestAgentsModel
  ]
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' existing = {
  name: registryName
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageName
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' existing = {
  parent: storage
  name: 'default'
}

resource approvedValidationRecords 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: approvedRecordContainerName
  properties: {
    publicAccess: 'None'
    immutableStorageWithVersioning: {
      enabled: true
    }
  }
}

resource automationArtifactContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, automationPrincipalId, blobContributorRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobContributorRoleId)
    principalId: automationPrincipalId
    principalType: 'User'
  }
}

resource automationRegistryPush 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, automationPrincipalId, acrPushRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPushRoleId)
    principalId: automationPrincipalId
    principalType: 'User'
  }
}

resource automationRegistryDelete 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, automationPrincipalId, acrDeleteRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrDeleteRoleId)
    principalId: automationPrincipalId
    principalType: 'User'
  }
}

resource localValidationProjectManager 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: stagingAccount
  name: guid(stagingAccount.id, automationPrincipalId, foundryProjectManagerRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', foundryProjectManagerRoleId)
    principalId: automationPrincipalId
    principalType: 'User'
  }
}

resource localValidationInsightsReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: stagingInsights
  name: guid(stagingInsights.id, automationPrincipalId, monitoringReaderRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', monitoringReaderRoleId)
    principalId: automationPrincipalId
    principalType: 'User'
  }
}

module dailyProject 'profile-project.bicep' = {
  name: 'daily-project'
  params: {
    location: location
    accountName: dailyAccount.name
    projectName: dailyAccountName
    applicationInsightsName: dailyInsights.name
    registryName: registry.name
    automationOwner: automationOwner
    automationPrincipalId: automationPrincipalId
    profile: 'daily'
    environment: 'swedencentral'
    telemetryGeneration: telemetryGeneration
    monitoringReaderRoleId: monitoringReaderRoleId
    modelInferenceRoleId: modelInferenceRoleId
    acrPullRoleId: acrPullRoleId
    foundryProjectManagerRoleId: foundryProjectManagerRoleId
  }
  dependsOn: [
    dailyInsightGenerationModel
  ]
}

module stagingProject 'profile-project.bicep' = {
  name: 'staging-project'
  params: {
    location: location
    accountName: stagingAccount.name
    projectName: stagingAccountName
    applicationInsightsName: stagingInsights.name
    registryName: registry.name
    automationOwner: automationOwner
    automationPrincipalId: automationPrincipalId
    profile: 'staging'
    environment: 'swedencentral'
    telemetryGeneration: telemetryGeneration
    monitoringReaderRoleId: monitoringReaderRoleId
    modelInferenceRoleId: modelInferenceRoleId
    acrPullRoleId: acrPullRoleId
    foundryProjectManagerRoleId: foundryProjectManagerRoleId
  }
  dependsOn: [
    stagingInsightGenerationModel
  ]
}
