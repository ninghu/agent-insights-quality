# Agent Catalog

<!-- Generated from catalogs/AGENT_CATALOG.yaml; do not edit. -->

| Agent | Owner | Type | Framework | Model | Terminal evidence | Semantic assertions | Trace operations | Validation | Execution digest | Issue count |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | ---: |
| `weather-agent` | Billy Hu | `prompt` | `foundry_prompt` | `gpt-5.4-mini` | `direct_prompt` | `required_per_request` | `uniform` | `baseline 6/10` | `sha256:bd23232c4e2dfe60a5c64f02c6fb9ce8d2e0f5257658ce8aae6e8b134385848e` | 6 |
| `healthcare-agent` | Ilya Matiach | `prompt` | `foundry_prompt` | `gpt-5.4-mini` | `direct_prompt` | `required_per_request` | `uniform` | `baseline 6/10` | `sha256:4f0585af819b1f4246a2cead4657e4a7bf8c16366557a3bff4742ac5a9f3519c` | 6 |
| `finance-agent` | Han Che | `hosted_code` | `microsoft_agent_framework` | `gpt-5.4-mini` | `standard_assistant_message` | `required` | `uniform` | `baseline 6/10` | `sha256:e8682c2f931e02ff832e3cd5ff96d7b96a4f5d8419903a9677fdb7064755f260` | 8 |
| `travel-agent` | Sean Gayler | `hosted_code` | `langgraph` | `gpt-5.4-mini` | `standard_assistant_message` | `required` | `uniform` | `baseline 6/10` | `sha256:622b6e132a0c87a8bc2a804302bf770d20ed86b29005d52a4a62bc8181cd1fcf` | 8 |
| `support-ticket-agent` | Nishal Dsilva | `hosted_custom_container` | `custom_responses` | `gpt-5.4-mini` | `explicit_span_attributes` | `required` | `required_per_request` | `baseline 6/10` | `sha256:7309dcab2000536ad3f9819c607a990d5952ddf8c62d5840c9d6283d197b9a81` | 8 |
