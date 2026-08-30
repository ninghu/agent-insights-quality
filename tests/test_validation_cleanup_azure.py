from __future__ import annotations

import json
from types import SimpleNamespace

from agent_insights_quality.validation_cleanup import CleanupPlanItem
from agent_insights_quality.validation_cleanup_azure import (
    AzureValidationCleanupBackend,
)


def _intent(kind: str, discovery_key: str) -> CleanupPlanItem:
    return CleanupPlanItem(
        kind=kind,
        deterministic_name="synthetic-agent/issue-001",
        provider_id="sha256:" + ("a" * 64),
        resolved_provider_id=None,
        intent_reference="sha256:" + ("a" * 64),
        runtime_kind="hosted_code",
        discovery_key=discovery_key,
        parent_id=None,
        authority_id="issue-001",
        state="ambiguous_create",
        cleanup_method="explicit",
        shared_manifest_allowed=False,
    )


def test_partial_agent_version_intent_resolves_without_runtime_topology() -> None:
    backend = object.__new__(AzureValidationCleanupBackend)
    backend._profile = SimpleNamespace(name="validation-cycle")
    backend._client = SimpleNamespace(
        _request=lambda *_args, **_kwargs: {
            "_status": 200,
            "data": [
                {
                    "version": "7",
                    "metadata": {
                        "aiq_profile": "validation-cycle",
                        "aiq_logical_version": "issue-001",
                    },
                }
            ],
        }
    )
    resolved = backend.resolve_intent(
        _intent(
            "provider_agent_version",
            "synthetic-agent|issue-001|provider_agent_version",
        )
    )
    assert resolved is not None
    assert resolved.resolved_provider_id == "synthetic-agent/versions/7"
    assert resolved.deterministic_name == "synthetic-agent/7"


def test_other_cycle_acr_tag_keeps_shared_manifest() -> None:
    digest = "sha256:" + ("b" * 64)
    backend = object.__new__(AzureValidationCleanupBackend)
    backend._profile = SimpleNamespace(
        container_registry_name="synthetic-registry"
    )
    backend._resources = [
        {
            "kind": "acr_manifest",
            "provider_id": digest,
            "resolved_provider_id": None,
            "discovery_key": f"support@{digest}",
            "deterministic_name": "support",
        },
        {
            "kind": "acr_tag",
            "parent_id": digest,
            "deterministic_name": "support:validation-current",
        },
    ]
    backend._run = lambda *_args, **_kwargs: SimpleNamespace(
        returncode=0,
        stdout=json.dumps(
            {"tags": ["validation-current", "validation-other-cycle"]}
        ),
    )
    assert backend.manifest_is_shared(digest) is True
