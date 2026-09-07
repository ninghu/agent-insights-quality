targetScope = 'resourceGroup'

param assessmentModelVersion string = '2026-07-09'
@minValue(1)
@maxValue(1000)
param assessmentCapacity int = 1000

var accounts = [
  'aiq-staging-swedencentral'
  'aiq-daily-swedencentral'
]

module assessmentModels 'modules/assessment-model.bicep' = [for accountName in accounts: {
  name: 'assessment-${accountName}'
  params: {
    accountName: accountName
    modelVersion: assessmentModelVersion
    capacity: assessmentCapacity
  }
}]
