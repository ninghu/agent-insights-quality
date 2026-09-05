import asyncio
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from agent_insights_quality.contracts import Environment
from agent_insights_quality.errors import QualityError
from agent_insights_quality.providers import AzureHttpTransport, AzureLogsReader, AzureSol, HttpRequest
from agent_insights_quality.providers.transport import ARM_SCOPE, FOUNDRY_SCOPE
from agent_insights_quality.registry import AzureRegistryBlob


@pytest.fixture
def environment():
    return Environment(
        "daily", "synthetic", "project", "https://example.invalid/api/projects/project",
        "/synthetic/telemetry", "storage", "registry", "swedencentral", "SwedenCentral",
    )


@pytest.fixture
def credentials(monkeypatch):
    class AzureError(Exception):
        pass

    class Credential:
        def __init__(self, *, fail=False):
            self.fail = fail
            self.scopes = []
            self.closed = False

        def get_token(self, scope):
            self.scopes.append(scope)
            if self.fail:
                raise AzureError("Synthetic private CLI diagnostic")
            return SimpleNamespace(token="synthetic-cli-token")

        async def close(self):
            self.closed = True

    cli = Credential()
    constructed = []

    def cli_credential(*args, **kwargs):
        assert not args and not kwargs
        constructed.append(cli)
        return cli

    def forbidden(*args, **kwargs):
        pytest.fail("The local runtime must not select an environment/managed/default credential")

    identity = ModuleType("azure.identity")
    identity.AzureCliCredential = cli_credential
    identity.DefaultAzureCredential = forbidden
    identity.EnvironmentCredential = forbidden
    identity.ManagedIdentityCredential = forbidden
    aio_identity = ModuleType("azure.identity.aio")
    aio_identity.AzureCliCredential = cli_credential
    aio_identity.DefaultAzureCredential = forbidden
    exceptions = ModuleType("azure.core.exceptions")
    exceptions.AzureError = AzureError
    monkeypatch.setitem(sys.modules, "azure.identity", identity)
    monkeypatch.setitem(sys.modules, "azure.identity.aio", aio_identity)
    monkeypatch.setitem(sys.modules, "azure.core.exceptions", exceptions)
    monkeypatch.setenv("AZURE_CLIENT_ID", "synthetic-environment-client")
    monkeypatch.setenv("AZURE_TENANT_ID", "synthetic-environment-tenant")
    return SimpleNamespace(cli=cli, constructed=constructed, Credential=Credential)


@pytest.fixture
def http(monkeypatch):
    requests = []

    class Reply:
        status = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"synthetic":"response"}'

    class Opener:
        def open(self, request, **kwargs):
            requests.append(request)
            return Reply()

    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Opener())
    return requests


@pytest.mark.parametrize("injected", [False, True])
def test_http_uses_cli_default_or_exact_injected_credential(environment, credentials, http, injected):
    credential = credentials.Credential() if injected else credentials.cli
    transport = AzureHttpTransport(credential if injected else None)
    assert not credentials.constructed and not credential.scopes
    for scope in (FOUNDRY_SCOPE, ARM_SCOPE):
        request = HttpRequest("GET", environment.project_endpoint, scope)
        reply = asyncio.run(transport.send(request))
        assert reply.status == 200
    assert credentials.constructed == ([] if injected else [credential])
    assert credential.scopes == [FOUNDRY_SCOPE, ARM_SCOPE]
    assert len(http) == 2


def test_cli_http_auth_failure_has_no_fallback_or_request(environment, credentials, http):
    credentials.cli.fail = True
    transport = AzureHttpTransport()
    with pytest.raises(QualityError, match="azure_authentication_failed") as error:
        asyncio.run(transport.send(HttpRequest("GET", environment.project_endpoint, FOUNDRY_SCOPE)))
    assert error.value.request_accepted is False
    assert error.value.__cause__ is None and error.value.__suppress_context__
    assert credentials.constructed == [credentials.cli]
    assert not http


@pytest.mark.parametrize("injected", [False, True])
def test_logs_default_and_injected_credentials_reach_the_query_client(credentials, monkeypatch, injected):
    clients = []
    queries = []
    credential = credentials.Credential() if injected else credentials.cli

    class LogsQueryClient:
        def __init__(self, selected):
            clients.append(selected)

        def query_resource(self, *args, **kwargs):
            queries.append((args, kwargs))
            return SimpleNamespace(status="Success", tables=[])

    query = ModuleType("azure.monitor.query")
    query.LogsQueryClient = LogsQueryClient
    monkeypatch.setitem(sys.modules, "azure.monitor.query", query)
    reader = AzureLogsReader("/synthetic/telemetry", credential=credential if injected else None)
    assert not clients and not credentials.constructed
    for _ in range(2):
        result = asyncio.run(reader.query(
            "synthetic query", start="2026-09-01T00:00:00Z", end="2026-09-02T00:00:00Z",
        ))
        assert result.complete
    assert clients == [credential]
    assert credentials.constructed == ([] if injected else [credential])
    assert len(queries) == 2


def test_injected_logs_client_does_not_construct_a_credential(credentials, monkeypatch):
    class Client:
        def query_resource(self, *args, **kwargs):
            return SimpleNamespace(status="Success", tables=[])

    query = ModuleType("azure.monitor.query")
    query.LogsQueryClient = lambda *args: pytest.fail("Injected query client was replaced")
    monkeypatch.setitem(sys.modules, "azure.monitor.query", query)
    reader = AzureLogsReader("/synthetic/telemetry", client=Client())
    assert asyncio.run(reader.query(
        "synthetic query", start="2026-09-01T00:00:00Z", end="2026-09-02T00:00:00Z",
    )).complete
    assert not credentials.constructed


def test_cli_logs_auth_failure_does_not_switch_identity(credentials, monkeypatch):
    credentials.cli.fail = True

    class LogsQueryClient:
        def __init__(self, credential):
            self.credential = credential

        def query_resource(self, *args, **kwargs):
            self.credential.get_token("synthetic-logs-scope")

    query = ModuleType("azure.monitor.query")
    query.LogsQueryClient = LogsQueryClient
    monkeypatch.setitem(sys.modules, "azure.monitor.query", query)
    with pytest.raises(QualityError, match="telemetry_query_failed") as error:
        asyncio.run(AzureLogsReader("/synthetic/telemetry").query(
            "synthetic query", start="2026-09-01T00:00:00Z", end="2026-09-02T00:00:00Z",
        ))
    assert error.value.__cause__ is None and error.value.__suppress_context__
    assert credentials.constructed == [credentials.cli]


def test_registry_already_uses_only_the_async_cli_credential(environment, credentials, monkeypatch):
    clients = []
    closed = []
    blob = ModuleType("azure.storage.blob.aio")

    class BlobClient:
        def __init__(self, **kwargs):
            clients.append(kwargs)

        async def close(self):
            closed.append(True)

    blob.BlobClient = BlobClient
    monkeypatch.setitem(sys.modules, "azure.storage.blob.aio", blob)
    registry = AzureRegistryBlob(environment)
    assert not credentials.constructed and not clients
    assert registry._get_client() is registry._get_client()
    assert credentials.constructed == [credentials.cli]
    assert clients[0]["credential"] is credentials.cli
    asyncio.run(registry.close())
    assert closed == [True] and credentials.cli.closed


def test_sol_uses_the_configured_deployment_alias_instead_of_a_hardcoded_model(environment, credentials):
    from agent_insights_quality.providers import HttpResponse

    requests = []

    class Transport:
        async def send(self, request):
            requests.append(request)
            return HttpResponse(200, body=json.dumps({
                "status": "completed",
                "output": [{"type": "message", "content": [
                    {"type": "output_text", "text": '{"synthetic":true}'},
                ]}],
            }).encode())

    result = asyncio.run(AzureSol(
        environment, transport=Transport(), deployment="synthetic-assessment-alias",
    ).complete_json(
        instructions="Synthetic assessment", payload={},
        schema={"type": "object", "properties": {"synthetic": {"type": "boolean"}},
                "required": ["synthetic"], "additionalProperties": False},
    ))
    assert result == {"synthetic": True}
    assert json.loads(requests[0].body)["model"] == "synthetic-assessment-alias"
    assert not credentials.constructed
