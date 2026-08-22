targetScope = 'subscription'

@description('Dedicated resource group name supplied at deployment time.')
param resourceGroupName string

@allowed([
  'westus2'
])
param location string = 'westus2'

@description('Globally unique suffix generated outside source control.')
@minLength(4)
@maxLength(12)
param uniqueSuffix string

@description('GPT-5.6 Terra model version selected during reviewed deployment.')
param terraModelVersion string

@description('Reviewed automation owner used for exact cleanup boundaries.')
param automationOwner string

@description('Microsoft Entra object ID of the user that runs reviewed quality automation.')
param automationPrincipalId string

resource resourceGroup 'Microsoft.Resources/resourceGroups@2024-11-01' = {
  name: resourceGroupName
  location: location
  tags: {
    purpose: 'agent-insights-quality'
    agentInsightsQualityQualification: 'true'
  }
}

module persistent 'modules/persistent.bicep' = {
  name: 'agent-insights-quality-persistent'
  scope: resourceGroup
  params: {
    location: location
    uniqueSuffix: uniqueSuffix
    terraModelVersion: terraModelVersion
    automationOwner: automationOwner
    automationPrincipalId: automationPrincipalId
  }
}
