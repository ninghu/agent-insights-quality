import asyncio
from collections.abc import Mapping
from copy import deepcopy
import json
from pathlib import Path

import pytest

from agent_insights_quality.catalogs import load_catalog
from agent_insights_quality.contracts import Environment, Target
from agent_insights_quality.providers import AzureRuntime, HttpRequest, HttpResponse
from agent_insights_quality.providers.artifacts import source_files
from agent_insights_quality.providers.container_environment import container_environment
from agent_insights_quality.providers.hosted import hosted_environment

ROOT = Path(__file__).resolve().parents[2]


class Transport:
    def __init__(self):
        self.requests: list[HttpRequest] = []

    async def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if len(self.requests) == 1:
            assert request.method == "GET"
            return HttpResponse(404)
        assert request.method == "POST"
        return HttpResponse(201, body=b'{"version":"42","status":"active"}')


def environment(profile: str) -> Environment:
    return Environment(
        profile, "synthetic", "project", "https://example.invalid/api/projects/project",
        "/synthetic/telemetry", "storage", "registry", "swedencentral", "SwedenCentral",
    )


def test_container_projection_omits_only_platform_reserved_names():
    variables = {
        **hosted_environment(environment("staging"), "InstrumentationKey=synthetic"),
        "FOUNDRY_AGENT_NAME": "synthetic-override", "FOUNDRY_AGENT_VERSION": "synthetic-version",
        "AGENT_SYNTHETIC": "reserved", "CUSTOM_SETTING": "permitted",
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": "synthetic-model",
    }
    original = deepcopy(variables)
    assert container_environment(variables) == {
        "ENABLE_SENSITIVE_DATA": "true", "CUSTOM_SETTING": "permitted",
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": "synthetic-model",
    }
    assert variables == original


@pytest.mark.parametrize("profile", ["staging", "daily"])
@pytest.mark.parametrize("version", [
    target.unit_id.logical_version for target in load_catalog(ROOT).for_agent("support-ticket-agent")
])
def test_real_support_deployment_post_uses_platform_environment_and_exact_source(profile, version):
    target = load_catalog(ROOT).target("support-ticket-agent/" + version)
    resolved = environment(profile)
    variables = hosted_environment(resolved, "InstrumentationKey=synthetic")
    before = source_files(target, container=True)
    calls = []
    image = "exampleregistry.azurecr.io/synthetic@sha256:" + "a" * 64

    class Images:
        async def ensure_image(
            self, selected: Target, source_revision: str, context: Mapping[str, bytes],
        ) -> str:
            calls.append((selected, source_revision, dict(context)))
            return image

    transport = Transport()
    cloud = AzureRuntime(resolved, transport=transport, images=Images(), hosted_environment=variables)
    saved = []
    deployed = asyncio.run(cloud.ensure_deployment(target, "synthetic-source", None, saved.append))
    body = json.loads(transport.requests[-1].body)
    assert body["definition"] == {
        "kind": "hosted",
        "protocol_versions": [{"protocol": "responses", "version": "1.0.0"}],
        "cpu": "1", "memory": "2Gi",
        "environment_variables": {
            "AZURE_AI_MODEL_DEPLOYMENT_NAME": "gpt-5.4-mini", "ENABLE_SENSITIVE_DATA": "true",
        },
        "container_configuration": {"image": image},
    }
    assert body["name"] == target.runtime_name(profile)
    assert deployed.provider_version == "42" and saved[-1] == deployed
    assert calls == [(target, "synthetic-source", before)]
    assert source_files(target, container=True) == before
    assert before["v0/requirements.txt"] == (target.baseline_root / "requirements.txt").read_bytes()
    assert variables == hosted_environment(resolved, "InstrumentationKey=synthetic")


@pytest.mark.parametrize("agent", ["finance-agent", "travel-agent"])
def test_hosted_code_post_keeps_the_existing_environment(agent):
    target = load_catalog(ROOT).target(agent + "/v0")
    resolved = environment("staging")
    variables = hosted_environment(resolved, "InstrumentationKey=synthetic")
    transport = Transport()
    cloud = AzureRuntime(resolved, transport=transport, hosted_environment=variables)
    asyncio.run(cloud.ensure_deployment(target, "synthetic-source", None, lambda value: None))
    wire = transport.requests[-1]
    boundary = wire.headers["Content-Type"].split("boundary=")[1].encode()
    metadata = json.loads(wire.body.split(b"--" + boundary)[1].split(b"\r\n\r\n", 1)[1])
    assert metadata["definition"]["environment_variables"] == {
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": "gpt-5.4-mini", **variables,
    }
    assert "code_configuration" in metadata["definition"]
    assert "container_configuration" not in metadata["definition"]


@pytest.mark.parametrize("agent", ["weather-agent", "healthcare-agent"])
def test_prompt_definition_post_is_unchanged(agent):
    target = load_catalog(ROOT).target(agent + "/v0")
    resolved = environment("staging")
    transport = Transport()
    cloud = AzureRuntime(
        resolved, transport=transport,
        hosted_environment=hosted_environment(resolved, "InstrumentationKey=synthetic"),
    )
    asyncio.run(cloud.ensure_deployment(target, "synthetic-source", None, lambda value: None))
    body = json.loads(transport.requests[-1].body)
    original = json.loads((target.version_root / "definition.json").read_text(encoding="utf-8"))
    assert body["definition"] == original["definition"]
    assert "environment_variables" not in body["definition"]
