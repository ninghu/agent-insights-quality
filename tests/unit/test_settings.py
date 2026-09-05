from dataclasses import FrozenInstanceError
import json
from pathlib import Path

import pytest

from agent_insights_quality.errors import QualityError
from agent_insights_quality.settings import (
    AssessmentSettings,
    RuntimeSettings,
    load_assessment_settings,
    load_settings,
    production_runtime_root,
)


def test_code_defaults_are_offline_and_bounded(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Default settings must not read any file")

    monkeypatch.setattr(Path, "open", forbidden)
    settings = load_settings()
    assert settings.daily_lanes == 5
    assert settings.staging_workers == 8
    assert settings.attempts == 10
    assert settings.readiness_attempts == 6
    assert settings.max_unscorable_units == 2
    assert settings.retry_limit == 3
    assert settings.poll_timeout_seconds == 600
    assert settings.insights_poll_timeout_seconds == 1200
    assert settings.daily_assessment_max_payload_bytes == 4_000_000
    assert load_assessment_settings() == AssessmentSettings(
        deployment_name="sol-assessment", model="gpt-5.6-sol",
        model_version="2026-07-09", credential="azure_cli",
    )
    with pytest.raises(FrozenInstanceError):
        settings.daily_lanes = 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"daily_lanes": True}, {"daily_lanes": 0}, {"daily_lanes": 6},
        {"staging_workers": 9}, {"staging_workers": "8"}, {"attempts": 11},
        {"readiness_attempts": 5}, {"query_workers": None}, {"deployment_workers": 9},
        {"assessment_workers": 0}, {"hydration_seconds": -1},
        {"hydration_seconds": 601}, {"poll_interval_seconds": 0},
        {"poll_timeout_seconds": 3601}, {"retry_limit": -1}, {"retry_limit": 9},
        {"insights_poll_timeout_seconds": 3601}, {"insights_poll_timeout_seconds": True},
        {"insights_poll_timeout_seconds": 3}, {"insights_poll_timeout_seconds": 0},
        {"daily_assessment_max_payload_bytes": 1023},
        {"daily_assessment_max_payload_bytes": 8_000_001},
        {"daily_assessment_max_payload_bytes": True},
        {"retry_limit": 2.0}, {"retry_backoff_seconds": 10, "retry_max_backoff_seconds": 2},
        {"poll_timeout_seconds": 3}, {"log_max_bytes": 100}, {"log_backup_count": 0},
    ],
)
def test_invalid_runtime_settings(overrides):
    with pytest.raises(QualityError, match="^settings_invalid$"):
        RuntimeSettings(**overrides)


def test_explicit_small_json_settings_and_snapshot(tmp_path):
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps({"daily_lanes": 2, "retry_limit": 1}), encoding="utf-8")
    settings = load_settings(path)
    assert settings.to_dict()["daily_lanes"] == 2
    assert settings.retry_limit == 1
    assert settings.staging_workers == 8


@pytest.mark.parametrize("content", ['[]', '{', '{"daily_lanes":1,"daily_lanes":2}',
                                     '{"root":"escape"}', '{"sender":"graph"}', " " * 16385])
def test_reject_unknown_duplicate_corrupt_and_oversized_settings(tmp_path, content):
    path = tmp_path / "runtime.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(QualityError, match="^settings_read_failed$"):
        load_settings(path)


def test_explicit_missing_file_is_not_defaults(tmp_path):
    with pytest.raises(QualityError, match="^settings_read_failed$"):
        load_settings(tmp_path / "missing.json")
    with pytest.raises(QualityError, match="^settings_read_failed$"):
        load_assessment_settings(tmp_path / "missing-assessment.json")


def test_private_assessment_configuration_is_explicit(tmp_path):
    path = tmp_path / "assessment.json"
    path.write_text(json.dumps({
        "deployment_name": "synthetic-sol", "model": "gpt-5.6-sol",
        "model_version": "2026-07-09", "credential": "azure_cli",
    }), encoding="utf-8")
    assert load_assessment_settings(path).deployment_name == "synthetic-sol"


@pytest.mark.parametrize(
    "overrides",
    [
        {"deployment_name": None}, {"deployment_name": "https://synthetic.invalid"},
        {"model": []}, {"model_version": False}, {"model_version": "latest"},
        {"model_version": "2026-99-99"},
        {"credential": "synthetic-secret"}, {"credential": "graph"}, {"credential": None},
    ],
)
def test_invalid_assessment_settings(overrides):
    with pytest.raises(QualityError, match="^assessment_settings_invalid$"):
        AssessmentSettings(**overrides)


def test_runtime_root_is_home_only_and_has_no_side_effects(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("AIQ_RUNTIME_ROOT", str(tmp_path / "wrong"))
    assert production_runtime_root() == tmp_path / ".aiq-runtime" / "agent-insights-quality"
    assert not (tmp_path / ".aiq-runtime").exists()
