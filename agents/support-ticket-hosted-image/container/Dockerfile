FROM python:3.13-slim

LABEL org.opencontainers.image.source="https://github.com/ninghu/agent-insights-quality"
LABEL org.opencontainers.image.title="Agent Insights Quality synthetic ticket agent"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --requirement requirements.txt \
    && useradd --create-home --uid 10001 agent
COPY --chown=agent:agent main.py logic.py model_runtime.py scenario_runtime.py agent.yaml ./

USER agent
EXPOSE 8088
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8088/readiness', timeout=2).read()"]

CMD ["python", "main.py"]
