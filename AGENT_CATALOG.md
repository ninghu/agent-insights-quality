# Agent Catalog

<!-- Generated from catalogs/AGENT_CATALOG.yaml; do not edit. -->

| Agent | Owner | Type | Framework | Model | Terminal evidence | Semantic assertions | Trace operations | Output messages | Validation | Execution digest | Issue count |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | ---: |
| `weather-agent` | Billy Hu | `prompt` | `foundry_prompt` | `gpt-5.4-mini` | `direct_prompt` | `required_per_request` | `uniform` | `present` | `baseline 5/5` | `sha256:e3e48daada11192baa28764ce1e8741222afa17d04c6225f832c8f96a180bd19` | 6 |
| `healthcare-agent` | Ilya Matiach | `prompt` | `foundry_prompt` | `gpt-5.4-mini` | `direct_prompt` | `required_per_request` | `uniform` | `present` | `baseline 5/5` | `sha256:9086beb26e439c752d128d949f4e0723d490f22d30a02ae222cfac6965b0569d` | 6 |
| `finance-agent` | Han Che | `hosted_code` | `microsoft_agent_framework` | `gpt-5.4-mini` | `standard_assistant_message` | `required` | `uniform` | `present` | `baseline 5/5` | `sha256:e9112f17a7d23ade48743d836a1b4a68dc9b5ad65947fa43d611ffebcae6abf0` | 8 |
| `travel-agent` | Sean Gayler | `hosted_code` | `langgraph` | `gpt-5.4-mini` | `standard_assistant_message` | `required` | `uniform` | `present` | `baseline 5/5` | `sha256:3fa72749e748b778e6023cdd3c34a34f414fce8a05e5954c92688afa750e3d02` | 8 |
| `support-ticket-agent` | Nishal Dsilva | `hosted_custom_container` | `custom_responses` | `gpt-5.4-mini` | `explicit_span_attributes` | `required` | `required_per_request` | `present` | `baseline 5/5` | `sha256:58c53a8ab9e66497fa704cb946e91e20fc9e37bb30bd3d200fd3a332e3049329` | 8 |
