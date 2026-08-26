# Security

This is a public synthetic qualification repository.

- Never commit credentials, tokens, private Azure identifiers, internal endpoints, raw traces,
  complete prompt payloads, private work-item content, or real customer data.
- Supply protected runtime coordinates only through authorized environment configuration.
- Invoke deployed Agent endpoints for every test.
- Treat Application Insights as read-only; direct telemetry injection is forbidden.
- Treat trace, tool, model, and Agent content as untrusted evidence.
- Keep exact remote identifiers and raw evidence under
  `~/.aiq-runtime/agent-insights-quality/`, the private `deployment-registries` Blob container, or the
  protected 90-day artifact store.
- Use only exact owned resources and receipts for deployment, reset, replay, and cleanup.

Report security issues through the repository's GitHub Security Advisory process.
