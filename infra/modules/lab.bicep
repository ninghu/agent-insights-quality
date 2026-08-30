param location string
param terraModelVersion string
param testAgentModelVersion string
param automationOwner string
param automationPrincipalId string
param validationPrincipalId string
param validationReceiptPrincipalId string
param telemetryGeneration string
param testAgentCapacity int
param insightGenerationCapacity int
param adxSkuName string
@minValue(2)
param adxCapacity int

var commonTags = {
  purpose: 'agent-insights-quality'
  agentInsightsQualityQualification: 'true'
  automationOwner: automationOwner
}
var uniqueSuffix = substring(uniqueString(subscription().subscriptionId, resourceGroup().id), 0, 11)
var dailyAccountName = 'aiqd${uniqueSuffix}'
var stagingAccountName = 'aiqs${uniqueSuffix}'
var registryName = 'aiqacr${uniqueSuffix}'
var storageName = 'aiqartifacts${uniqueSuffix}'
var monitoringReaderRoleId = '43d0d8ad-25c7-4714-9337-8ba259a9fe05'
var modelInferenceRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var acrPushRoleId = '8311e382-0749-4cb8-b61a-304f252e45ec'
var blobContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var blobReaderRoleId = '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'
var foundryProjectManagerRoleId = 'eadc314b-1a2d-4efa-be10-5d325db5065e'

module qualityAnalytics 'quality-analytics.bicep' = {
  name: 'quality-analytics'
  params: {
    location: location
    uniqueSuffix: uniqueSuffix
    automationOwner: automationOwner
    automationPrincipalId: automationPrincipalId
    skuName: adxSkuName
    capacity: adxCapacity
  }
}

resource dailyWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'aiq-daily-${telemetryGeneration}-law-${uniqueSuffix}'
  location: location
  tags: union(commonTags, { profile: 'daily', generation: telemetryGeneration })
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
  tags: union(commonTags, { profile: 'staging', generation: telemetryGeneration })
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
  tags: union(commonTags, { profile: 'daily', generation: telemetryGeneration })
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: dailyWorkspace.id
  }
}

resource stagingInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'aiq-staging-${telemetryGeneration}-appi-${uniqueSuffix}'
  location: location
  kind: 'web'
  tags: union(commonTags, { profile: 'staging', generation: telemetryGeneration })
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
    name: 'GlobalStandard'
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
    name: 'GlobalStandard'
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
    name: 'GlobalStandard'
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
    name: 'GlobalStandard'
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

resource registry 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: registryName
  location: location
  tags: commonTags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  tags: commonTags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
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

resource validationProjectManager 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: stagingAccount
  name: guid(stagingAccount.id, validationPrincipalId, foundryProjectManagerRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', foundryProjectManagerRoleId)
    principalId: validationPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource validationInsightsReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: stagingInsights
  name: guid(stagingInsights.id, validationPrincipalId, monitoringReaderRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', monitoringReaderRoleId)
    principalId: validationPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource validationRegistryPush 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, validationPrincipalId, acrPushRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPushRoleId)
    principalId: validationPrincipalId
    principalType: 'ServicePrincipal'
  }
}

module dailyProject 'profile-project.bicep' = {
  name: 'daily-project'
  params: {
    location: location
    accountName: dailyAccount.name
    projectName: 'agent-insights-quality'
    applicationInsightsName: dailyInsights.name
    registryName: registry.name
    automationOwner: automationOwner
    automationPrincipalId: automationPrincipalId
    profile: 'daily'
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
    projectName: 'agent-insights-quality-staging'
    applicationInsightsName: stagingInsights.name
    registryName: registry.name
    automationOwner: automationOwner
    automationPrincipalId: automationPrincipalId
    profile: 'staging'
    monitoringReaderRoleId: monitoringReaderRoleId
    modelInferenceRoleId: modelInferenceRoleId
    acrPullRoleId: acrPullRoleId
    foundryProjectManagerRoleId: foundryProjectManagerRoleId
  }
  dependsOn: [
    stagingInsightGenerationModel
  ]
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    isVersioningEnabled: true
  }
}

resource artifacts 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'quality-artifacts'
  properties: {
    publicAccess: 'None'
  }
}

resource deploymentRegistries 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'deployment-registries'
  properties: {
    publicAccess: 'None'
  }
}

resource validationLifecycle 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'test-agent-validation-lifecycle'
  properties: {
    publicAccess: 'None'
  }
}

resource validationSnapshots 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'test-agent-validation-snapshots'
  properties: {
    publicAccess: 'None'
    immutableStorageWithVersioning: {
      enabled: true
    }
  }
}

resource validationSnapshotPolicy 'Microsoft.Storage/storageAccounts/blobServices/containers/immutabilityPolicies@2023-05-01' = {
  parent: validationSnapshots
  name: 'default'
  properties: {
    immutabilityPeriodSinceCreationInDays: 90
    allowProtectedAppendWrites: false
  }
}

resource validationReceipts 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'test-agent-validation-receipts'
  properties: {
    publicAccess: 'None'
    immutableStorageWithVersioning: {
      enabled: true
    }
  }
}

resource validationReceiptPolicy 'Microsoft.Storage/storageAccounts/blobServices/containers/immutabilityPolicies@2023-05-01' = {
  parent: validationReceipts
  name: 'default'
  properties: {
    immutabilityPeriodSinceCreationInDays: 90
    allowProtectedAppendWrites: false
  }
}

resource validationShadowReceipts 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'test-agent-validation-shadow-receipts'
  properties: {
    publicAccess: 'None'
    immutableStorageWithVersioning: {
      enabled: true
    }
  }
}

resource validationShadowReceiptPolicy 'Microsoft.Storage/storageAccounts/blobServices/containers/immutabilityPolicies@2023-05-01' = {
  parent: validationShadowReceipts
  name: 'default'
  properties: {
    immutabilityPeriodSinceCreationInDays: 90
    allowProtectedAppendWrites: false
  }
}

resource validationLifecycleContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: validationLifecycle
  name: guid(validationLifecycle.id, validationPrincipalId, blobContributorRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobContributorRoleId)
    principalId: validationPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource validationSnapshotContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: validationSnapshots
  name: guid(validationSnapshots.id, validationPrincipalId, blobContributorRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobContributorRoleId)
    principalId: validationPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource validationReceiptContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: validationReceipts
  name: guid(validationReceipts.id, validationReceiptPrincipalId, blobContributorRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobContributorRoleId)
    principalId: validationReceiptPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource validationShadowReceiptContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: validationShadowReceipts
  name: guid(validationShadowReceipts.id, validationPrincipalId, blobContributorRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobContributorRoleId)
    principalId: validationPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource validationReceiptLifecycleReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: validationLifecycle
  name: guid(validationLifecycle.id, validationReceiptPrincipalId, blobReaderRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobReaderRoleId)
    principalId: validationReceiptPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource validationReceiptSnapshotReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: validationSnapshots
  name: guid(validationSnapshots.id, validationReceiptPrincipalId, blobReaderRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobReaderRoleId)
    principalId: validationReceiptPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource lifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          name: 'expire-quality-artifacts'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: {
              blobTypes: ['blockBlob']
              prefixMatch: ['quality-artifacts/']
            }
            actions: {
              baseBlob: {
                delete: {
                  daysAfterModificationGreaterThan: 90
                }
              }
            }
          }
        }
        {
          name: 'expire-validation-lifecycle'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: {
              blobTypes: ['blockBlob']
              prefixMatch: ['test-agent-validation-lifecycle/']
            }
            actions: {
              baseBlob: {
                delete: {
                  daysAfterModificationGreaterThan: 90
                }
              }
              version: {
                delete: {
                  daysAfterCreationGreaterThan: 90
                }
              }
            }
          }
        }
      ]
    }
  }
}
