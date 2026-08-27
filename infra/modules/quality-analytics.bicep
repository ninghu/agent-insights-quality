param location string
param uniqueSuffix string
param automationOwner string
param automationPrincipalId string
param skuName string
@minValue(2)
param capacity int

var commonTags = {
  purpose: 'agent-insights-quality'
  agentInsightsQualityQualification: 'true'
  automationOwner: automationOwner
  component: 'quality-analytics'
}

resource cluster 'Microsoft.Kusto/clusters@2025-02-14' = {
  name: 'aiqadx${uniqueSuffix}'
  location: location
  tags: commonTags
  sku: {
    name: skuName
    tier: 'Standard'
    capacity: capacity
  }
  properties: {
    enableAutoStop: false
    enableDiskEncryption: true
    enableDoubleEncryption: false
    enablePurge: false
    enableStreamingIngest: false
    engineType: 'V2'
    publicIPType: 'IPv4'
    publicNetworkAccess: 'Enabled'
    restrictOutboundNetworkAccess: 'Disabled'
    trustedExternalTenants: []
  }
}

resource database 'Microsoft.Kusto/clusters/databases@2025-02-14' = {
  parent: cluster
  name: 'AgentInsightsQuality'
  location: location
  kind: 'ReadWrite'
  properties: {
    hotCachePeriod: 'P90D'
    softDeletePeriod: 'P730D'
  }
}

resource schema 'Microsoft.Kusto/clusters/databases/scripts@2025-02-14' = {
  parent: database
  name: 'quality-analytics-schema'
  properties: {
    continueOnErrors: false
    forceUpdateTag: 'quality-analytics-v6'
    #disable-next-line use-secure-value-for-secure-inputs
    scriptContent: loadTextContent('../quality-analytics.kql')
  }
}

resource automationViewer 'Microsoft.Kusto/clusters/databases/principalAssignments@2025-02-14' = {
  parent: database
  name: guid(database.id, automationPrincipalId, 'Viewer')
  properties: {
    principalId: automationPrincipalId
    principalType: 'User'
    role: 'Viewer'
    tenantId: tenant().tenantId
  }
  dependsOn: [schema]
}

resource automationIngestor 'Microsoft.Kusto/clusters/databases/principalAssignments@2025-02-14' = {
  parent: database
  name: guid(database.id, automationPrincipalId, 'Ingestor')
  properties: {
    principalId: automationPrincipalId
    principalType: 'User'
    role: 'Ingestor'
    tenantId: tenant().tenantId
  }
  dependsOn: [schema]
}
