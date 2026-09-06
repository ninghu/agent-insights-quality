import asyncio
import json
import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest

from agent_insights_quality.contracts import Environment
from agent_insights_quality.errors import QualityError
from agent_insights_quality.providers import AzureHttpTransport, AzureLogsReader, AzureSol, HttpRequest, HttpResponse
from agent_insights_quality.providers.transport import ARM_SCOPE, AZURE_DEVOPS_SCOPE, FOUNDRY_SCOPE, JsonClient
from agent_insights_quality.registry import AzureRegistryBlob


@pytest.fixture
def environment():
    return Environment(
        "daily", "synthetic", "project", "https://example.invalid/api/projects/project",
        "/synthetic/telemetry", "storage", "registry", "swedencentral", "SwedenCentral",
    )


@pytest.fixture
def credentials(monkeypatch):
    clock = SimpleNamespace(now=0)

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
            return SimpleNamespace(
                token=f"synthetic-cli-token-{len(self.scopes)}", expires_on=clock.now + 3600,
            )

        async def close(self):
            self.closed = True

    cli = Credential()
    constructed = []
    options = []
    providers = []

    def cli_credential(*args, **kwargs):
        assert not args and kwargs in ({}, {"process_timeout": 60})
        options.append(kwargs)
        constructed.append(cli)
        return cli

    def token_provider(credential, scope):
        providers.append((credential, scope))
        token = None

        def bearer():
            # Mock the SDK policy boundary, not a second production token cache.
            nonlocal token
            if token is None or token.expires_on - clock.now < 300:
                token = credential.get_token(scope)
            return token.token

        return bearer

    def forbidden(*args, **kwargs):
        pytest.fail("The local runtime must not select an environment/managed/default credential")

    identity = ModuleType("azure.identity")
    identity.AzureCliCredential = cli_credential
    identity.get_bearer_token_provider = token_provider
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
    return SimpleNamespace(
        cli=cli, constructed=constructed, Credential=Credential, options=options,
        providers=providers, clock=clock,
    )


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
    assert credentials.options == ([] if injected else [{"process_timeout": 60}])
    assert credentials.providers == [(credential, FOUNDRY_SCOPE), (credential, ARM_SCOPE)]
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


@pytest.mark.parametrize("method,outcome", [
    ("POST", 201), ("POST", 202), ("POST", 429), ("GET", 429), ("POST", "no_response"),
])
def test_cancelled_http_send_drains_actual_thread_without_retry(environment, monkeypatch, method, outcome):
    release, finished = threading.Event(), threading.Event()
    calls, backoffs = [], []
    transport = AzureHttpTransport()

    async def exercise():
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()
        def blocking(request):
            calls.append(request)
            loop.call_soon_threadsafe(entered.set)
            try:
                assert release.wait(timeout=5), "HTTP worker was not released"
                if outcome == "no_response":
                    raise OSError("Synthetic lost response")
                return HttpResponse(outcome, {}, b"{}")
            finally:
                finished.set()
        monkeypatch.setattr(transport, "_send", blocking)
        async def backoff(delay):
            backoffs.append(delay)
            await asyncio.sleep(0)
        client = JsonClient(environment.project_endpoint, transport, sleep=backoff)
        task = asyncio.create_task(client.request(method, "/sessions"))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done() and not finished.is_set()
                assert len(calls) == 1
        finally:
            release.set()
            task.cancel()
            settled = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(settled[0], asyncio.CancelledError)
        assert finished.is_set() and len(calls) == 1 and not backoffs

    asyncio.run(exercise())


@pytest.mark.parametrize("injected", [False, True])
def test_http_reuses_each_scopes_sdk_token_provider(environment, credentials, http, injected):
    credential = credentials.Credential() if injected else credentials.cli
    transport = AzureHttpTransport(credential if injected else None)
    for scope in (FOUNDRY_SCOPE, ARM_SCOPE, AZURE_DEVOPS_SCOPE) * 3:
        asyncio.run(transport.send(HttpRequest("GET", environment.project_endpoint, scope)))
    assert credential.scopes == [FOUNDRY_SCOPE, ARM_SCOPE, AZURE_DEVOPS_SCOPE]
    assert credentials.providers == [(credential, scope) for scope in credential.scopes]
    assert len(http) == 9
    assert http[0].get_header("Authorization") == http[3].get_header("Authorization")
    assert http[0].get_header("Authorization") != http[1].get_header("Authorization")


def test_http_serializes_concurrent_first_use_and_refresh(environment, credentials, http):
    transport = AzureHttpTransport()
    scopes = [FOUNDRY_SCOPE, ARM_SCOPE] * 4
    all_started = threading.Event()
    serial = threading.Lock()
    counter = threading.Lock()
    callers = 0

    class ObservedLock:
        def __enter__(self):
            nonlocal callers
            with counter:
                callers += 1
                if callers == len(scopes):
                    all_started.set()
            serial.acquire()

        def __exit__(self, *args):
            serial.release()

    transport._credential_lock = ObservedLock()

    async def requests():
        return await asyncio.gather(*(
            transport.send(HttpRequest("GET", environment.project_endpoint, scope))
            for scope in scopes
        ))

    original = credentials.cli.get_token
    active = threading.Lock()

    def get_token(scope):
        assert active.acquire(blocking=False), "Concurrent credential refresh"
        try:
            assert all_started.wait(timeout=5), "Requests did not contend for the credential"
            return original(scope)
        finally:
            active.release()

    credentials.cli.get_token = get_token
    assert all(result.status == 200 for result in asyncio.run(requests()))
    assert credentials.constructed == [credentials.cli]
    assert sorted(credentials.cli.scopes) == sorted(set(scopes))
    assert len(credentials.providers) == 2
    credentials.clock.now = 3601
    assert all(result.status == 200 for result in asyncio.run(requests()))
    assert len(credentials.cli.scopes) == 4
    assert len(credentials.providers) == 2
    assert len(http) == 16


def test_http_refresh_follows_sdk_expiry_margin(environment, credentials, http):
    transport = AzureHttpTransport()
    request = HttpRequest("GET", environment.project_endpoint, FOUNDRY_SCOPE)
    for now in (0, 3299, 3301, 7002):
        credentials.clock.now = now
        asyncio.run(transport.send(request))
    assert credentials.cli.scopes == [FOUNDRY_SCOPE] * 3
    assert len(credentials.providers) == 1
    tokens = [wire.get_header("Authorization") for wire in http]
    assert tokens[0] == tokens[1]
    assert tokens[1] != tokens[2] != tokens[3]


@pytest.mark.parametrize("now", [3301, 3601])
def test_http_refresh_failure_never_sends_a_stale_token(environment, credentials, http, now):
    transport = AzureHttpTransport()
    request = HttpRequest("GET", environment.project_endpoint, FOUNDRY_SCOPE)
    asyncio.run(transport.send(request))
    credentials.clock.now = now
    credentials.cli.fail = True
    with pytest.raises(QualityError, match="azure_authentication_failed") as error:
        asyncio.run(transport.send(request))
    assert error.value.request_accepted is False
    assert error.value.__cause__ is None and error.value.__suppress_context__
    assert len(http) == 1
    credentials.cli.fail = False
    asyncio.run(transport.send(request))
    assert len(http) == 2 and len(credentials.providers) == 1
    assert http[0].get_header("Authorization") != http[1].get_header("Authorization")


def test_http_scope_rejection_precedes_credentials(environment, credentials, http):
    with pytest.raises(QualityError, match="provider_scope_invalid"):
        asyncio.run(AzureHttpTransport().send(
            HttpRequest("GET", environment.project_endpoint, "https://unapproved.invalid/.default"),
        ))
    assert not credentials.constructed and not credentials.providers and not http


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
    assert credentials.options == ([] if injected else [{"process_timeout": 60}])
    assert len(queries) == 2
    assert all(options["headers"] == {"Cache-Control": "no-cache"} for _, options in queries)


def test_injected_logs_client_does_not_construct_a_credential(credentials, monkeypatch):
    class Client:
        def query_resource(self, *args, **kwargs):
            assert kwargs["headers"] == {"Cache-Control": "no-cache"}
            return SimpleNamespace(status="Success", tables=[])

    query = ModuleType("azure.monitor.query")
    query.LogsQueryClient = lambda *args: pytest.fail("Injected query client was replaced")
    monkeypatch.setitem(sys.modules, "azure.monitor.query", query)
    reader = AzureLogsReader("/synthetic/telemetry", client=Client())
    assert asyncio.run(reader.query(
        "synthetic query", start="2026-09-01T00:00:00Z", end="2026-09-02T00:00:00Z",
    )).complete
    assert not credentials.constructed


@pytest.mark.parametrize("initial_roots", [3, 5])
@pytest.mark.parametrize("honor_bypass,expected_roots", [(True, 10), (False, None)])
def test_discovery_and_hydration_refresh_within_the_original_response_cache_lifetime(
    credentials, monkeypatch, initial_roots, honor_bypass, expected_roots,
):
    from copy import deepcopy
    import test_telemetry as telemetry

    query_module = ModuleType("azure.monitor.query")
    query_module.LogsQueryClient = lambda *_: pytest.fail("Injected client must be retained")
    monkeypatch.setitem(sys.modules, "azure.monitor.query", query_module)
    class CachedClient:
        elapsed = 0
        roots = initial_roots
        def __init__(self):
            self.cache, self.calls = {}, []

        def query_resource(self, resource, query, **kwargs):
            self.calls.append((resource, query, deepcopy(kwargs)))
            fresh = honor_bypass and kwargs["headers"] == {"Cache-Control": "no-cache"}
            if query in self.cache and self.elapsed - self.cache[query][0] < 120 and not fresh:
                return deepcopy(self.cache[query][1])
            rows = [telemetry.span(f"response-{index}") for index in range(self.roots)]
            table = SimpleNamespace(
                name="PrimaryResult", columns=list(rows[0]), rows=[list(row.values()) for row in rows],
            )
            result = SimpleNamespace(status="Success", tables=[table])
            self.cache[query] = (self.elapsed, deepcopy(result))
            return result

    client = CachedClient()
    reader = AzureLogsReader("/synthetic/telemetry", client=client)
    receipts = tuple(telemetry.invocation(index) for index in range(10))
    first = asyncio.run(telemetry.collect_snapshot(reader, telemetry.DEPLOYMENT, receipts))
    client.elapsed, client.roots = 5, 10
    second = asyncio.run(telemetry.collect_snapshot(reader, telemetry.DEPLOYMENT, receipts))
    assert len(first.attributable_responses) == initial_roots
    assert len(second.attributable_responses) == (expected_roots or initial_roots)
    assert len(client.calls) == 4
    assert client.calls[0] == client.calls[2]
    assert client.calls[1] == client.calls[3]
    assert "operation_Id in" not in client.calls[0][1]
    assert "operation_Id in" in client.calls[1][1]
    assert all(call[2]["headers"] == {"Cache-Control": "no-cache"} for call in client.calls)
    assert first.query_complete and second.query_complete
    assert second.window_start == first.window_start and second.window_end == first.window_end


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
