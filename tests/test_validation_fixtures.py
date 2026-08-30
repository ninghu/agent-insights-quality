from __future__ import annotations

import json

from agent_insights_quality.util import ROOT
from agent_insights_quality.catalogs import load_catalogs
from agent_insights_quality.validation_manifest import authority_specs
from agent_insights_quality.validation_evidence import validate_evidence
from agent_insights_quality.validation_blob import BlobRecord
from agent_insights_quality.validation_issuer import (
    build_validation_receipt,
    current_issuer_code_digest,
    validate_receipt,
)
from agent_insights_quality.validation_lifecycle import validate_lifecycle
from agent_insights_quality.validation_policy import load_trusted_policy


def test_sanitized_shadow_fixtures_cover_r01_and_reviewed_r02() -> None:
    root = ROOT / "tests" / "fixtures" / "test_agent_validation"
    agents, issues = load_catalogs()
    current = {
        item.authority_id: item.execution_digest
        for item in authority_specs(agents, issues)
    }
    for run in ("r01", "r02"):
        evidence = json.loads((root / run / "evidence.json").read_text())
        lifecycle = json.loads(
            (root / run / "clean-lifecycle.json").read_text()
        )
        receipt = json.loads(
            (root / run / "shadow-receipt.json").read_text()
        )
        durations = json.loads((root / run / "durations.json").read_text())
        validate_evidence(evidence)
        validate_lifecycle(lifecycle)
        validate_receipt(receipt)
        assert len(evidence["authorities"]) == 41
        assert all(item["pass"] for item in evidence["authorities"])
        assert lifecycle["state"] == "CLEAN"
        assert lifecycle["cleanup"]["exact_clean"] is True
        assert lifecycle["project"]["state"] == "deleted"
        assert lifecycle["project"]["provider_id"] in lifecycle["cleanup"][
            "verified_absent_ids"
        ]
        assert receipt["mode"] == "shadow"
        assert receipt["authorizes_merge"] is False
        assert (
            receipt["trusted_policy"]["default_branch_trust_anchor_present"]
            is False
        )
        assert durations["percentiles_calculated"] is False
        assert receipt["issuer"]["issuer_code_digest"] == (
            current_issuer_code_digest()
        )
        assert [
            item["authority_evidence_digest"]
            for item in receipt["authorities"]
        ] == [
            item["authority_evidence_digest"]
            for item in evidence["authorities"]
        ]
        assert lifecycle["evidence_reference"]["digest"] == evidence[
            "evidence_digest"
        ]
        rebuilt = build_validation_receipt(
            mode="shadow",
            evidence=evidence,
            clean_snapshot=BlobRecord(
                "test-agent-validation-snapshots",
                f"clean/{lifecycle['cycle_id']}.json",
                lifecycle,
                receipt["clean_snapshot"]["etag"],
                receipt["clean_snapshot"]["version_id"],
            ),
            issuer=receipt["issuer"],
            trusted_policy_manifest=load_trusted_policy()[0],
            policy_commit_sha=receipt["trusted_policy"]["commit_sha"],
            policy_content_digest=receipt["trusted_policy"]["content_digest"],
            review=receipt["review"],
            targeted_verification=receipt["required_ci"][
                "targeted_verification"
            ],
            continuous_integration=receipt["required_ci"][
                "continuous_integration"
            ],
            issued_at=receipt["issued_at"],
        )
        validate_receipt(rebuilt)
        assert {
            item["authority_id"]: item["execution_digest"]
            for item in evidence["authorities"]
        } == current
    assert json.loads(
        (root / "r01" / "shadow-receipt.json").read_text()
    )["review"]["status"] == "skipped"
    assert json.loads(
        (root / "r02" / "shadow-receipt.json").read_text()
    )["review"]["status"] == "success"


def test_shadow_fixtures_contain_no_raw_payloads_or_private_identifiers() -> None:
    root = ROOT / "tests" / "fixtures" / "test_agent_validation"
    forbidden_keys = {"input", "output", "prompt", "raw_trace", "response_body"}
    for path in root.rglob("*.json"):
        value = json.loads(path.read_text(encoding="utf-8"))
        text = json.dumps(value, sort_keys=True).casefold()
        assert "/subscriptions/" not in text
        assert "https://" not in text
        assert "@microsoft.com" not in text

        def keys(item):
            if isinstance(item, dict):
                return set(item).union(*(keys(child) for child in item.values()))
            if isinstance(item, list):
                return set().union(*(keys(child) for child in item))
            return set()

        assert forbidden_keys.isdisjoint(keys(value))
    for run in ("r01", "r02"):
        lifecycle = json.loads(
            (root / run / "clean-lifecycle.json").read_text()
        )
        for agent in lifecycle["runtime_topology"]["agents"]:
            assert agent["provider_agent_id"].startswith("sha256:")
            assert agent["provider_agent_version_id"].startswith("sha256:")
            assert agent["telemetry_identity_id"].startswith("sha256:")
            if agent["runtime_kind"] == "prompt":
                assert agent["runtime_principal_id"] is None
                assert agent["hosted_identity_id"] is None
            else:
                assert agent["runtime_principal_id"].startswith("sha256:")
                assert agent["hosted_identity_id"].startswith("sha256:")
                assert agent["hosted_blueprint_id"].startswith("sha256:")
                assert agent["hosted_deployment_id"].startswith("sha256:")
        assert {
            kind: sum(
                item["runtime_kind"] == kind
                for item in lifecycle["runtime_topology"]["agents"]
            )
            for kind in ("prompt", "hosted_code", "hosted_custom_container")
        } == {
            "prompt": 14,
            "hosted_code": 18,
            "hosted_custom_container": 9,
        }
