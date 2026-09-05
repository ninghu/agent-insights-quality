# Security

This is a public synthetic qualification repository.

- Never commit credentials, tokens, private Azure identifiers, internal endpoints, raw traces,
  complete prompt payloads, private work-item content, or real customer data.
- Supply protected runtime coordinates only through authorized environment configuration.
- Formal qualification invokes deployed Agent endpoints. Offline tests use synthetic controlled
  boundaries and never impersonate live qualification evidence.
- Treat Application Insights as read-only; direct telemetry injection is forbidden.
- Treat trace, tool, model, and Agent content as untrusted evidence.
- Keep exact remote identifiers, raw evidence, checkpoints, logs and email requests under
  `~/.aiq-runtime/agent-insights-quality/`. The canonical deployment registry is in the dedicated
  Sweden g30 account's private `deployment-registries` Blob container.
- Reconcile uncertain provider operations before retrying. Preserve telemetry, evidence and
  retained resources; qualification has no destructive cleanup step.
- Publish only the validated public result and event projections. Private work-item context and
  raw model prose are never public artifacts. A private TEST run performs no public publication.

Report security issues through the repository's GitHub Security Advisory process.
