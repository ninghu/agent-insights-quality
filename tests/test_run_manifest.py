from __future__ import annotations

from copy import deepcopy

import pytest

from agent_insights_quality.run_manifest import (
    OFFICIAL_DELIVERY,
    TEST_EMAIL_ONLY_DELIVERY,
    validate_manifest,
)
from agent_insights_quality.util import ContractError, content_hash


def _manifest(
    *,
    run_id: str = "aiq-20260828",
    profile: str = "daily",
    delivery_mode: str = OFFICIAL_DELIVERY,
) -> dict:
    value = {
        "schema_version": "3.0.0",
        "run_id": run_id,
        "profile": profile,
        "delivery_mode": delivery_mode,
        "report_date": "2026-08-28",
        "insight_lookback_hours": 0.1,
        "telemetry_resource_set": "g29",
        "catalog_hashes": {},
        "agents": [{}, {}, {}, {}, {}],
        "manifest_hash": "",
    }
    value["manifest_hash"] = content_hash(
        {key: item for key, item in value.items() if key != "manifest_hash"}
    )
    return value


def test_manifest_accepts_official_and_isolated_test_delivery() -> None:
    validate_manifest(_manifest())
    validate_manifest(
        _manifest(
            run_id="aiq-20260828-r01",
            delivery_mode=TEST_EMAIL_ONLY_DELIVERY,
        )
    )


@pytest.mark.parametrize(
    ("run_id", "profile"),
    [
        ("aiq-20260828", "daily"),
        ("aiq-20260828-r01", "staging"),
    ],
)
def test_test_delivery_requires_daily_rerun_identity(
    run_id: str,
    profile: str,
) -> None:
    manifest = _manifest(
        run_id=run_id,
        profile=profile,
        delivery_mode=TEST_EMAIL_ONLY_DELIVERY,
    )
    with pytest.raises(ContractError, match="daily nonzero rerun"):
        validate_manifest(manifest)


def test_manifest_rejects_superseded_schema() -> None:
    manifest = deepcopy(_manifest())
    manifest["schema_version"] = "2.0.0"
    manifest["manifest_hash"] = content_hash(
        {key: item for key, item in manifest.items() if key != "manifest_hash"}
    )
    with pytest.raises(ContractError, match="Run manifest is invalid"):
        validate_manifest(manifest)
