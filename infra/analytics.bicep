targetScope = 'subscription'

param resourceGroupName string = 'agent-insights-quality-rg'
param location string = 'westus2'
param automationOwner string = 'ninghu'
param automationPrincipalId string
param adxSkuName string = 'Standard_E2ads_v5'
@minValue(2)
param adxCapacity int = 2

resource resourceGroup 'Microsoft.Resources/resourceGroups@2024-11-01' existing = {
  name: resourceGroupName
}

var uniqueSuffix = substring(
  uniqueString(subscription().subscriptionId, resourceGroup.id),
  0,
  11
)

module qualityAnalytics 'modules/quality-analytics.bicep' = {
  name: 'quality-analytics'
  scope: resourceGroup
  params: {
    location: location
    uniqueSuffix: uniqueSuffix
    automationOwner: automationOwner
    automationPrincipalId: automationPrincipalId
    skuName: adxSkuName
    capacity: adxCapacity
  }
}
