FROM python:3.12.11-slim

ARG ISSUE_PATH=v0/implementation.yaml
ARG PIP_INSTALL_ARGS=""

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY v0/requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir ${PIP_INSTALL_ARGS} -r /app/requirements.txt
COPY v0/source /app/source
COPY ${ISSUE_PATH} /app/issue.yaml

RUN useradd --create-home --uid 10001 agentuser
USER agentuser
EXPOSE 8088
CMD ["python", "-m", "source.app"]
